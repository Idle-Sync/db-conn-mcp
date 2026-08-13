"""The MCP server: 23 tools + 2 prompts, plus transport wiring (FastMCP).

Knows nothing about PostgreSQL. It wires the pure/abstract layers together: the
:class:`~db_conn_mcp.handlers.Handlers` service (which uses ``config``, the dialect
registry, ``safety``, and ``diagnostics``) onto FastMCP tools and a prompt.

We use FastMCP — the official high-level SDK API — over the lower-level
``mcp.server.Server`` because it is radically simpler (Rule 1: simplicity first) while
remaining the same SDK.
"""

import difflib
import hmac
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ContentBlock, TextContent
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from . import doctor as doctor_mod
from .config import global_config_path, resolve_path
from .guard import UNTRUSTED_DATA_POLICY, guard_content_blocks
from .handlers import Handlers

Transport = Literal["stdio", "http"]

#: What ``FastMCP.call_tool`` may return: the text blocks alone, or
#: ``(text blocks, structured content)`` — where the structured half is ``None`` for
#: the value-bearing tools below.
ToolResult = Sequence[ContentBlock] | tuple[Sequence[ContentBlock], dict[str, Any] | None]

#: Tools that return raw row VALUES — the most attacker-controllable content this server
#: emits, since anyone who can write a row chooses the text. Their payload must reach the
#: agent ONLY inside the untrusted-data banner, so these four emit no ``structuredContent``
#: at all: a client that renders only the structured channel would otherwise read raw row
#: data with no fence around it. Metadata tools (schemas, stats, diagnostics, config) keep
#: their structured output.
VALUE_BEARING_TOOLS = frozenset(
    {"sample_table_rows", "execute_read_query", "fetch_rows", "search_value"}
)

#: What a value-bearing tool says when its result carries no rows at all. FastMCP turns
#: a list into one text block PER row, so an empty list becomes ZERO content blocks —
#: and with the structured channel dropped, the response would say nothing whatsoever:
#: "this table is empty" and "the call did nothing" would look identical to the agent.
#: An empty JSON array is what the same result minus its rows parses as.
EMPTY_VALUE_PAYLOAD = "[]"


class GuardedFastMCP(FastMCP):
    """FastMCP that rejects unknown arguments and fences every tool's *text* output.

    One seam covers all 23 tools (Rule 1: no per-tool boilerplate). Only the text
    channel is wrapped; for the tools in :data:`VALUE_BEARING_TOOLS` the structured
    channel is dropped entirely, so raw row values cannot reach a client outside the
    banner. Every other tool's ``structuredContent`` passes through **untouched** so it
    stays valid against the tool's output schema, which clients rely on.
    """

    def _reject_unknown_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """Raise if ``arguments`` carries a key the tool's input schema never declared.

        FastMCP silently drops undeclared arguments, so a mistargeted call looked like it
        worked — issue #29: ``check_database(connection="prod")`` probed *every* database
        with no signal that the targeting was ignored. Only parameter **names** appear in
        the message; a value could be a DSN (Rule 6).
        """
        tool = self._tool_manager.get_tool(name)
        if tool is None:  # unknown tool — leave FastMCP's own error untouched
            return
        accepted = sorted(tool.parameters.get("properties", {}))
        unknown = sorted(set(arguments) - set(accepted))
        if not unknown:
            return
        closest = difflib.get_close_matches(unknown[0], accepted, n=1)
        hint = f" (did you mean '{closest[0]}'?)" if closest else ""
        raise ValueError(
            f"unknown parameter(s) for {name}: {', '.join(unknown)}"
            f" - accepted: {', '.join(accepted)}{hint}"
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Reject unknown arguments, run the tool, then wrap its text blocks in the guard.

        A value-bearing tool answers ``(guarded blocks, None)``: its rows are served on
        the fenced text channel only. Their output schemas are un-declared at build time
        (see :func:`_drop_value_bearing_output_schemas`), which is what lets ``None``
        through the SDK's output validation. A result with no blocks at all (a row list
        that came back empty) is given one saying so — see :data:`EMPTY_VALUE_PAYLOAD`.
        """
        self._reject_unknown_arguments(name, arguments)
        result = await super().call_tool(name, arguments)
        if name in VALUE_BEARING_TOOLS:
            blocks = result[0] if isinstance(result, tuple) else result
            if not blocks:
                blocks = [TextContent(type="text", text=EMPTY_VALUE_PAYLOAD)]
            return guard_content_blocks(blocks), None
        if isinstance(result, tuple):
            unstructured, structured = result
            return guard_content_blocks(unstructured), structured
        return guard_content_blocks(result)


#: The full connection-gotchas checklist exposed by the troubleshoot_connection prompt.
TROUBLESHOOT_CHECKLIST = """\
Database connection troubleshooting checklist:

1. Host & port — is the host correct and the port open (Postgres default 5432)?
2. Server up — is the database process actually running and accepting connections?
3. Firewall / security groups — is traffic allowed from your machine to the server?
4. Docker — inside a container, `localhost` is the container itself, NOT the database
   container. Use the service name (compose) or the host gateway.
5. SSL — does the server require SSL? Add an appropriate mode, e.g. `?sslmode=require`.
6. Database name — correct spelling and case? Does the database exist yet?
7. Credentials — correct user/password? Does the role exist and may it log in?
8. Connection limits — is the server's `max_connections` exhausted by idle sessions?
9. DNS / VPN — can the hostname be resolved? Is a required VPN connected?
"""

#: Guidance exposed by the faithful_schema_export prompt — how to choose an export and
#: how to offer installing pg_dump for the faithful path.
FAITHFUL_SCHEMA_GUIDANCE = """\
Exporting a database schema with db-conn-mcp — two options:

1. get_database_schema(database, format="sql") — SELF-CONTAINED (recommended default).
   Builds a runnable DDL script (tables, columns, sequences, PK/FK/UNIQUE/CHECK, indexes,
   trigger functions, triggers) using ONLY the existing connection. Works everywhere, no
   extra software. Covers the common 95%; very exotic objects may be approximate.

2. dump_schema_faithful(database) — FAITHFUL (pg_dump --schema-only).
   Byte-faithful and complete (sequences, identity, partitioning, etc.), produced by
   Postgres' own pg_dump. Requires the pg_dump binary on the machine running this server.

If the user wants the faithful export and dump_schema_faithful returns
status="pg_dump_not_found": OFFER to install pg_dump for them, and only with their
explicit consent run the matching command, then call dump_schema_faithful again:
  - Windows:        winget install PostgreSQL.PostgreSQL
  - macOS:          brew install libpq   (then add its bin to PATH)
  - Debian/Ubuntu:  sudo apt-get install -y postgresql-client
  - RHEL/Fedora:    sudo dnf install -y postgresql
If they don't want to install anything, fall back to get_database_schema(format="sql").
"""


def _drop_value_bearing_output_schemas(app: GuardedFastMCP) -> None:
    """Un-declare the output schema of every :data:`VALUE_BEARING_TOOLS` entry.

    The SDK's low-level handler rejects a result whose ``structuredContent`` is ``None``
    while the tool still advertises an ``outputSchema`` ("Output validation error"), so
    suppressing the structured channel means the schema must go with it — and a client
    then sees the honest surface: these tools promise text only.

    Today only ``sample_table_rows`` actually declares one (FastMCP derives a schema from
    a ``list[dict]`` return annotation but not from a bare ``dict``); the loop covers all
    four so a future annotation change cannot silently re-open the gap.
    """
    for name in VALUE_BEARING_TOOLS:
        tool = app._tool_manager.get_tool(name)
        if tool is None:  # pragma: no cover — every name above is registered below
            continue
        tool.fn_metadata.output_schema = None
        tool.fn_metadata.output_model = None


def build_server(config_path: Path | str | None = None) -> GuardedFastMCP:
    """Construct the guarded FastMCP app with all 23 tools and 2 prompts."""
    resolved = resolve_path(str(config_path) if config_path else None)
    handlers = Handlers(resolved)
    # 23 tools + 2 prompts registered below. `instructions` reaches the client in the
    # initialize response — the standing half of the prompt-injection defence, covering
    # the metadata tools, whose structuredContent a client may read without ever seeing
    # the per-response text guard. The row-value tools close that gap outright: they
    # emit no structuredContent, so their data exists only inside the fence.
    app = GuardedFastMCP("db-conn-mcp", instructions=UNTRUSTED_DATA_POLICY)
    # FastMCP takes no `version`, and the low-level server then advertises the *SDK's*
    # version in the initialize response's serverInfo. Stamp our own instead, so a
    # client (and the live verification engine in `verify.py`) can tell which build of
    # db-conn-mcp it is actually talking to.
    app._mcp_server.version = __version__

    # ---- Exploration tools ---------------------------------------------------
    @app.tool()
    async def list_databases() -> list[dict]:
        """List configured databases with their allowed mode and yolo flag (no DSN).

        Entries with fallback_ports configured also show them, plus active_port when a
        fallback port is currently in use.
        """
        return await handlers.list_databases()

    @app.tool()
    async def list_tables(
        database: str, pattern: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """List all tables and views in the named database.

        Narrow instead of listing everything: `pattern` keeps only names containing it
        (fuzzy, case-insensitive — "order" matches ORDERS, order_items). `limit` returns
        at most that many rows, so a huge database can't flood the answer.
        """
        return await handlers.list_tables(database, pattern, limit)

    @app.tool()
    async def get_table_schema(database: str, table: str, include_indexes: bool = False) -> dict:
        """Get columns, types, and primary/foreign keys for a table.

        Pass include_indexes=true to also get the table's indexes (name, its KEY columns
        in index order — an INCLUDE payload is not listed — whether it is unique, and its
        method: btree, gin, ...). Use it before writing a filter or a JOIN, or when a
        query is slow and you want to know what is actually indexed. Omitted by default,
        so the plain answer stays small.
        """
        return await handlers.get_table_schema(database, table, include_indexes)

    @app.tool()
    async def get_database_schema(
        database: str, output_dir: str | None = None, format: Literal["json", "sql"] = "json"
    ) -> dict:
        """Get the whole database's schema at once, as JSON or as a runnable SQL script.

        format="json" (default): every table with columns, types, nullability, primary key,
        and foreign keys. format="sql": a SELF-CONTAINED DDL script that recreates the
        schema — tables, columns, sequences, PK/FK/UNIQUE/CHECK, indexes, trigger functions,
        and triggers — so running it rebuilds the structure. No extra tools needed; covers
        the common cases (for a byte-faithful dump use dump_schema_faithful).

        Output is deterministic. Returned inline by default; pass output_dir to instead WRITE
        it to `{database}_schema_{UTC}.{json|sql}` and get back the path plus a summary —
        recommended for large schemas too big to return inline.
        """
        return await handlers.get_database_schema(database, output_dir, format)

    @app.tool()
    async def dump_schema_faithful(database: str, output_dir: str | None = None) -> dict:
        """Produce a byte-faithful schema dump using the database's own tool (pg_dump -s).

        The most complete/runnable schema export (handles sequences, identity, partitioning,
        etc.) but REQUIRES the pg_dump binary on the machine running this server. Returns the
        DDL inline (or writes `{database}_schema_{UTC}.sql` when output_dir is set). If
        pg_dump isn't installed it returns {status: "pg_dump_not_found", message}: offer to
        install pg_dump for the user (see the faithful_schema_export prompt), then retry —
        or use get_database_schema(format="sql") which needs no extra tools.
        """
        return await handlers.dump_schema_faithful(database, output_dir)

    @app.tool()
    async def sample_table_rows(database: str, table: str, n: int = 10) -> list[dict]:
        """Fetch the first N rows of a table to learn its data shape.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.
        """
        return await handlers.sample_table_rows(database, table, n)

    # ---- Discovery / search tools --------------------------------------------
    @app.tool()
    async def find_columns(database: str, pattern: str, limit: int | None = None) -> list[dict]:
        """Find columns by name across all tables (fuzzy, case-insensitive substring).

        e.g. pattern "email" matches user_email, EMAIL_ADDRESS. Use this to locate where
        a concept lives before querying. `limit` returns at most that many rows — set it
        when a broad pattern would otherwise match half the database.
        """
        return await handlers.find_columns(database, pattern, limit)

    @app.tool()
    async def search_value(
        database: str,
        value: str,
        tables: list[str] | None = None,
        limit_per_column: int = 5,
    ) -> dict:
        """Find WHERE a value appears across tables (fuzzy, case-insensitive substring).

        Returns the tables/columns containing the value, with match counts and samples.
        For speed on large databases, first narrow with list_tables/find_columns and pass
        a `tables` shortlist; otherwise it scans all non-system tables (bounded, may
        return partial results flagged `truncated`).

        Matches arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.
        """
        return await handlers.search_value(database, value, tables, limit_per_column)

    # ---- Execution tools -----------------------------------------------------
    @app.tool()
    async def execute_read_query(
        database: str,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run a read-only SELECT query (enforced as a read-only transaction).

        Know the schema before you query: if the exact table and column names are not
        already in this conversation, call get_table_schema (or list_tables /
        find_columns) first instead of guessing — a wrong name costs a failed
        round-trip. Don't re-fetch a schema you already have.

        ALWAYS pass user-supplied values via `params` with $1/$2/... placeholders in the
        SQL (real driver bind parameters — no quoting/injection pitfalls), not by pasting
        them into the SQL string. Optional timeout_ms cancels an overrunning query.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.
        """
        return await handlers.execute_read_query(database, sql, params, timeout_ms)

    @app.tool()
    async def execute_write_query(
        database: str,
        sql: str,
        ctx: Context,
        params: list[Any] | None = None,
        user_consent: bool = False,
        dry_run: bool = True,
        skip_dry_run: bool = False,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run an INSERT/UPDATE/DELETE/DDL statement on a write-mode database.

        Know the schema before you write: if the exact table and column names are not
        already in this conversation, call get_table_schema first instead of guessing —
        a wrong name wastes a whole dry-run round-trip. Don't re-fetch a schema you
        already have.

        SAFETY — the server enforces this flow; you cannot skip it silently:
        1. Call with dry_run=true (the default): the statement executes inside a
           transaction and is ALWAYS rolled back, returning the rows_affected it
           WOULD have had. This records a preview grant for this exact statement.
        2. Show the user the exact SQL and the dry-run result; ask for permission
           (not needed if the database has yolo enabled).
        3. Call again with dry_run=false (and user_consent=true unless yolo) to
           commit. Commits without a prior dry-run of the identical statement are
           REJECTED. The preview grant expires 10 minutes after the dry-run and is
           consumed by the commit ATTEMPT it authorizes — including a commit that
           FAILS, which may still have applied server-side, so you MUST dry-run
           again before retrying. Running the same statement twice means previewing
           it twice. Pass skip_dry_run=true ONLY if the user explicitly asked to
           skip the preview.

        Pass values via `params` ($1/$2/... placeholders), not pasted into the SQL.
        """
        # Scope the dry-run grant to THIS MCP session (issue #41): under --transport http
        # one Handlers instance is shared by every client, so a grant must not be
        # satisfiable by a different session. FastMCP injects `ctx` and keeps it out of
        # the tool's input schema. During a real request `ctx.session` is always set;
        # outside one (direct/unit calls) it raises — that is the single local session.
        try:
            session: object | None = ctx.session
        except ValueError:
            session = None
        return await handlers.execute_write_query(
            database,
            sql,
            params,
            user_consent=user_consent,
            dry_run=dry_run,
            skip_dry_run=skip_dry_run,
            timeout_ms=timeout_ms,
            session=session,
        )

    @app.tool()
    async def explain_query(
        database: str, sql: str, analyze: bool = False, params: list[Any] | None = None
    ) -> dict:
        """Show the execution plan for a read-only query (EXPLAIN; optionally ANALYZE).

        Use to confirm index usage or diagnose a slow query. analyze=true actually runs
        the (read-only, validated) query to collect real timings and buffer stats.

        EXPLAIN the query you will ACTUALLY run: a plan for bound parameters can differ
        from the plan for the same SQL with the values pasted in. Pass `params` with the
        $1/$2/... placeholders exactly as you would to execute_read_query (real driver
        binds), instead of rewriting the query with literals just to explain it.
        `params` composes with analyze=true.
        """
        return await handlers.explain_query(database, sql, analyze, params)

    @app.tool()
    async def cancel_query(database: str, pid: int) -> dict:
        """Cancel the statement running in server backend `pid` (find pids via show_activity).

        Cancels the statement only — the session survives. cancelled=false means the pid
        doesn't exist or you lack permission (same database user required).
        """
        return await handlers.cancel_query(database, pid)

    # ---- Cursor tools (large result sets) -------------------------------------
    @app.tool()
    async def open_query_cursor(database: str, sql: str, params: list[Any] | None = None) -> dict:
        """Open a server-side cursor over a read-only query for batched fetching.

        Use for results too large for one execute_read_query response. Returns a
        cursor_id for fetch_rows/close_cursor. The cursor pins one read-only
        connection until closed (auto-reaped after 15 idle minutes; max 5 open).
        """
        return await handlers.open_query_cursor(database, sql, params)

    @app.tool()
    async def fetch_rows(cursor_id: str, n: int = 100) -> dict:
        """Fetch the next N rows from an open cursor (see open_query_cursor).

        Returns {rows, row_count, exhausted, cursor_closed}; the cursor closes itself
        once fully drained.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.
        """
        return await handlers.fetch_rows(cursor_id, n)

    @app.tool()
    async def close_cursor(cursor_id: str) -> dict:
        """Close an open cursor and release its database connection. Idempotent."""
        return await handlers.close_cursor(cursor_id)

    # ---- Object-definition tool -----------------------------------------------
    @app.tool()
    async def get_object_definition(
        database: str,
        object_type: Literal["view", "function", "trigger", "sequence", "index"],
        name: str,
    ) -> dict:
        """Get the faithful SQL definition of a non-table object by name.

        Covers views (and materialized views), functions/procedures, triggers,
        sequences, and indexes — produced by the database's own pg_get_*def functions.
        `name` may be schema-qualified ("public.my_view"); all matches are returned
        (same name in several schemas, overloaded functions).
        """
        return await handlers.get_object_definition(database, object_type, name)

    # ---- Operational insight tools ----------------------------------------------
    @app.tool()
    async def diff_schemas(database_a: str, database_b: str) -> dict:
        """Compare the schemas of two configured databases (tables, columns, PK/FK).

        Reports tables only in one side, and per-table column/type/default/key
        differences. Use to verify a migrated copy matches its source of truth.
        """
        return await handlers.diff_schemas(database_a, database_b)

    @app.tool()
    async def check_sequences(database: str, behind_only: bool = True) -> dict:
        """Find sequences lagging behind their column's max value (stale after migrations).

        A `behind: true` sequence would hand out an id that already exists — fix with
        setval(). Run this after any bulk copy or logical-replication migration. By
        default only the problem sequences are listed, with `total_sequences` reporting
        how many were checked — so `behind_count: 0` means "all clear". Pass
        `behind_only=false` for the full census; on a large schema that is one row per
        sequence (hundreds), so ask for it only when you need every sequence's state.
        """
        return await handlers.check_sequences(database, behind_only)

    @app.tool()
    async def table_stats(
        database: str,
        limit: int | None = None,
        min_size_bytes: int | None = None,
        table: str | None = None,
    ) -> dict:
        """Approximate row counts and disk/index sizes per table, largest first.

        Uses the database's own statistics — fast, no table scans. Useful for planning
        migration copy order and spotting anomalies. One row per table, so the full
        answer may be large on a big database — ask the narrower question instead of
        filtering afterwards: `table` for one table (optionally schema-qualified),
        `min_size_bytes` to skip anything smaller, `limit` for the top N by total size.
        With `limit` set the result carries `truncated: true` when more tables matched
        than were returned.
        """
        return await handlers.table_stats(database, limit, min_size_bytes, table)

    @app.tool()
    async def show_activity(database: str, include_query: bool = False) -> dict:
        """Show current server activity: pid, state, wait events, query/transaction age.

        Sanitized by default — no user names, client addresses, or query text (pass
        include_query=true for truncated query text). Answers "what is holding
        connections"; pair with cancel_query(pid) to kill a stuck statement.
        """
        return await handlers.show_activity(database, include_query)

    # ---- Configuration tool --------------------------------------------------
    @app.tool()
    async def set_yolo_mode(database: str, enabled: bool) -> dict:
        """Enable/disable yolo (skip the write-consent prompt) for one database, persisted."""
        return await handlers.set_yolo_mode(database, enabled)

    # ---- Diagnostics tool ----------------------------------------------------
    @app.tool()
    async def check_database(database: str | None = None) -> list[dict]:
        """Test connectivity for one database (or all) — returns OK or a sanitized cause.

        Reports active_port when the database answered on a fallback port — doubles as a
        live re-probe for tunneled connections.
        """
        return await handlers.check_database(database)

    @app.tool()
    async def doctor(offline: bool = False) -> list[dict]:
        """Diagnose the whole setup, not just connectivity — use when something is
        misbehaving and check_database alone doesn't explain why.

        Runs every diagnostic: stale running server processes, newer PyPI release,
        connections.json key typos/types, secrets exposure (permissions, git),
        MCP client entries pointing at dead paths, and per-database connectivity
        with a credential-free fallback-port identity probe. Returns findings as
        {check, status: ok|warn|fail|skipped, detail, suggested_action} where
        suggested_action is machine-actionable: reconnect_client, upgrade_package,
        swap_primary_port, fix_permissions, fix_config, repair_client_config,
        fix_connection, install_doctor_extra, run_setup, report_bug, or none —
        every finding that states a remedy names one. Fix what you can; report the
        rest to the user verbatim. Output is sanitized — never contains DSNs, hosts,
        or credentials. offline=true skips the PyPI lookup.
        """
        # Existence is re-evaluated per call (like the CLI): the config file can be
        # deleted after startup, and run_checks() reads None as "no configuration".
        path = handlers.config_path
        return await doctor_mod.run_checks(path if path.is_file() else None, offline=offline)

    # ---- Prompts -------------------------------------------------------------
    @app.prompt()
    def troubleshoot_connection() -> str:
        """The full database-connection gotchas checklist."""
        return TROUBLESHOOT_CHECKLIST

    @app.prompt()
    def faithful_schema_export() -> str:
        """How to export a schema: self-contained SQL vs. faithful pg_dump (+ installing it)."""
        return FAITHFUL_SCHEMA_GUIDANCE

    _drop_value_bearing_output_schemas(app)
    return app


# --------------------------------------------------------------------------------
# HTTP/SSE transport authentication (issue #42)
# --------------------------------------------------------------------------------
#
# FastMCP's built-in "sse" transport serves EVERY tool — writes included — with no
# credential at all, on 127.0.0.1:8000, so any local process could open ``/sse`` and
# drive writes. We instead build FastMCP's Starlette SSE app ourselves, wrap it in a
# bearer-token + loopback-Host guard, and serve it over uvicorn bound to loopback.
# The stdio transport is a single local pipe and is left completely unauthenticated.

#: The request header the HTTP/SSE transport authenticates on (``<header>: Bearer <token>``).
HTTP_TOKEN_HEADER = "Authorization"

#: File (beside connections.json, owner-only) that persists the HTTP transport token.
HTTP_TOKEN_FILENAME = "http-token"


def _http_token_path() -> Path:
    """Where the HTTP/SSE bearer token is persisted (user-only file, 0600).

    Lives in the user config dir beside ``connections.json`` — a SEPARATE secret from
    the GUI's ``gui-token``, so neither transport's credential ever grants the other.
    """
    return global_config_path().parent / HTTP_TOKEN_FILENAME


def _http_token() -> str:
    """Read the persisted HTTP token, or mint and persist a fresh one.

    The token is a ``secrets.token_urlsafe(32)`` value written owner-only (0600). The
    mode is passed to the *creation* call rather than chmod'ed afterwards, so the secret
    is never briefly world-readable at the umask default (mirrors the GUI's
    ``_write_token``). On Windows the bits are advisory — NTFS ignores them — but the
    file sits under the user's profile directory, which is not world-readable there.
    """
    path = _http_token_path()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    path.chmod(0o600)
    return token


def _bearer_token(header_value: str) -> str:
    """Extract ``<token>`` from an ``Authorization: Bearer <token>`` header value.

    Returns ``""`` for a missing header or any non-Bearer scheme, which then fails the
    constant-time comparison like any other mismatch.
    """
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def _host_is_loopback(host_header: str) -> bool:
    """Whether a ``Host`` header names the loopback interface (DNS-rebinding defence).

    Accepts ``127.0.0.1``, ``localhost`` and IPv6 ``::1`` with or without a port, and
    rejects everything else — mirroring the GUI's Host allowlist so what is served here
    can only be reached as loopback, never through a rebound hostname.
    """
    host = host_header.strip().lower()
    if not host:
        return False
    if host.startswith("["):  # bracketed IPv6, optionally with a port: [::1] / [::1]:8000
        hostname = host[1 : host.index("]")] if "]" in host else host
    elif host.count(":") == 1:  # host:port
        hostname = host.rsplit(":", 1)[0]
    else:  # bare host, or bare IPv6 such as ::1
        hostname = host
    return hostname in {"127.0.0.1", "localhost", "::1"}


class _HttpAuthGuard:
    """Pure-ASGI guard: constant-time bearer-token check, then loopback-Host check.

    Pure ASGI rather than :class:`~starlette.middleware.base.BaseHTTPMiddleware` on
    purpose — the latter buffers the response body, which would break the long-lived
    ``/sse`` event stream. Every HTTP request must carry ``Authorization: Bearer
    <token>`` (else 401) and a loopback ``Host`` (else 403); the token check runs first
    so a probe from a rebound name still learns nothing beyond "unauthorized".
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        # Compared as bytes: compare_digest raises on non-ASCII *str*, and a crash inside
        # the boundary would surface as a 500 with a traceback. surrogateescape keeps any
        # undecodable byte sequence comparable instead of raising.
        self._token = token.encode("utf-8", "surrogateescape")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate one HTTP request, then defer to the wrapped app."""
        if scope["type"] != "http":  # lifespan/websocket: nothing to authenticate here
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        supplied = _bearer_token(headers.get("authorization", ""))
        if not hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), self._token):
            await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
            return
        if not _host_is_loopback(headers.get("host", "")):
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _build_authenticated_sse_app(app: ASGIApp, token: str) -> Starlette:
    """Wrap an SSE ASGI app in the bearer-token + loopback-Host guard.

    ``app`` is FastMCP's ``sse_app()`` (a Starlette serving ``/sse`` and ``/messages``);
    the returned Starlette mounts it behind :class:`_HttpAuthGuard`, so BOTH the GET
    event stream and the POST message route require the token. Raises on an empty token
    — an empty credential compares equal to a request that supplies none, which would
    serve everything to anyone (the boundary refuses to be built unarmed).
    """
    if not token:
        raise ValueError("A non-empty HTTP token is required.")
    return Starlette(
        routes=[Mount("/", app=app)],
        middleware=[Middleware(_HttpAuthGuard, token=token)],
    )


def _print_http_startup(host: str, port: int, token: str) -> None:
    """Print operator guidance for the authenticated HTTP/SSE server, to stderr.

    The token is a local session secret (not a DSN or DB credential), so echoing it to
    the operator's OWN terminal is intended — it is how they configure their MCP client
    (the GUI prints its token the same way). Rule 6 governs DSNs/credentials, not this.
    """
    print(
        f"db-conn-mcp HTTP/SSE server on http://{host}:{port}/sse\n"
        f"Every request must send this header:\n"
        f"    {HTTP_TOKEN_HEADER}: Bearer {token}\n"
        f"Configure your MCP client with that header (token stored at "
        f"{_http_token_path()}). Ctrl-C to stop.",
        file=sys.stderr,
    )


def _run_fastmcp(transport: Transport, config_path: str | None) -> None:
    """Build the MCP app and serve it over the chosen transport (split out so ``run`` is testable).

    ``stdio`` is one local pipe and stays unauthenticated. ``http`` maps to FastMCP's
    SSE app, which we serve OURSELVES behind :class:`_HttpAuthGuard` (issue #42) rather
    than via ``app.run(transport="sse")``, which served every tool — writes included —
    with no credential on 127.0.0.1:8000.
    """
    app = build_server(config_path)
    if transport == "stdio":
        app.run(transport="stdio")
        return
    _run_http(app)


def _run_http(app: GuardedFastMCP) -> None:
    """Serve the SSE app behind the bearer-token guard, over uvicorn on loopback."""
    import uvicorn

    token = _http_token()
    host, port = app.settings.host, app.settings.port
    authenticated = _build_authenticated_sse_app(app.sse_app(), token)
    _print_http_startup(host, port, token)
    # Bound explicitly to 127.0.0.1 (not app.settings.host) so the loopback promise the
    # Host check makes cannot be widened by a stray setting.
    uvicorn.run(authenticated, host="127.0.0.1", port=port, log_level="warning")


def run(transport: Transport = "stdio", config_path: str | None = None, gui: bool = True) -> None:
    """Launch the server over the chosen transport (``stdio`` default, or ``http``/SSE).

    Unless ``gui=False`` (the CLI's ``--no-gui``), also tries to host the local
    dashboard on 127.0.0.1:31415 from a background thread, silently skipping when
    another process already holds the port — the first server started wins it.
    """
    if gui:
        try:
            from .gui.app import start_in_thread

            start_in_thread(config_path)
        except Exception:  # noqa: BLE001 — MCP must start regardless of any GUI failure
            pass
    _run_fastmcp(transport, config_path)
