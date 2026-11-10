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
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypedDict

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ContentBlock
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
#: agent ONLY inside the untrusted-data banner, so these five emit no ``structuredContent``
#: at all: a client that renders only the structured channel would otherwise read raw row
#: data with no fence around it. Metadata tools (schemas, stats, diagnostics, config) keep
#: their structured output.
VALUE_BEARING_TOOLS = frozenset(
    {"sample_table_rows", "execute_read_query", "execute_write_query", "fetch_rows", "search_value"}
)

#: The most rows ``sample_table_rows`` returns; a larger ``n`` is clamped to this and the
#: answer says so with ``meta.truncated: true``.
SAMPLE_ROWS_LIMIT = 1000


class Meta(TypedDict):
    """The first key of every tool's answer: what came back, and how long it took.

    null means the field does not apply to the tool, or the tool cannot know it.
    """

    tool: str
    database: str | None
    row_count: int | None
    truncated: bool | None
    elapsed_ms: int


class ListDatabasesResult(TypedDict):
    """list_databases: the configured databases, never a DSN."""

    meta: Meta
    databases: list[dict[str, Any]]


class ListTablesResult(TypedDict):
    """list_tables: one {schema, name, kind} item per table or view."""

    meta: Meta
    tables: list[dict[str, Any]]


class FindColumnsResult(TypedDict):
    """find_columns: one {schema, table, column, type, nullable} item per match."""

    meta: Meta
    columns: list[dict[str, Any]]


class CheckDatabaseResult(TypedDict):
    """check_database: one connectivity result per probed database."""

    meta: Meta
    results: list[dict[str, Any]]


class DoctorResult(TypedDict):
    """doctor: one finding per diagnostic check."""

    meta: Meta
    findings: list[dict[str, Any]]


def _with_meta(
    tool: str,
    database: str | None,
    started: float,
    body: dict,
    *,
    count_key: str | None = None,
    truncated: bool | None = None,
) -> dict:
    """Return ``body`` with a ``meta`` object put first.

    ``row_count`` is the length of ``body[count_key]`` when that key is present, else
    ``None``. ``elapsed_ms`` is whole milliseconds since ``started``, a
    ``time.monotonic()`` reading. All five meta keys are always set: the typed tools'
    answers are validated against :class:`Meta`.
    """
    row_count = len(body[count_key]) if count_key in body else None
    meta: Meta = {
        "tool": tool,
        "database": database,
        "row_count": row_count,
        "truncated": truncated,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return {"meta": meta, **body}


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
        through the SDK's output validation. Every tool answers one object, so there is
        always exactly one text block to wrap, an empty row list included.
        """
        self._reject_unknown_arguments(name, arguments)
        result = await super().call_tool(name, arguments)
        if name in VALUE_BEARING_TOOLS:
            blocks = result[0] if isinstance(result, tuple) else result
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

    Today none of the five declares one: each is annotated with a bare ``dict``, from
    which FastMCP derives no schema (a typed return such as a ``TypedDict`` would get
    one). The loop still covers all five so a future annotation change cannot silently
    re-open the gap.
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
    async def list_databases() -> ListDatabasesResult:
        """List configured databases with their allowed mode and yolo flag (no DSN).

        Returns `meta` and `databases`, one item per configured database with `name`,
        `mode` and `yolo`. Entries with fallback_ports configured also show them, plus
        active_port when a fallback port is currently in use.
        """
        started = time.monotonic()
        databases = await handlers.list_databases()
        return _with_meta(
            "list_databases", None, started, {"databases": databases}, count_key="databases"
        )

    @app.tool()
    async def list_tables(
        database: str, pattern: str | None = None, limit: int | None = None
    ) -> ListTablesResult:
        """List all tables and views in the named database.

        Narrow instead of listing everything: `pattern` keeps only names containing it
        (fuzzy, case-insensitive — "order" matches ORDERS, order_items). `limit` returns
        at most that many rows, so a huge database can't flood the answer.

        Returns `meta` and `tables`, one item per table or view with `schema`, `name` and
        `kind` (table or view). With `limit` set, `meta.truncated` is true when more
        tables matched than were returned.
        """
        started = time.monotonic()
        if limit is None:
            tables = await handlers.list_tables(database, pattern, None)
            return _with_meta(
                "list_tables",
                database,
                started,
                {"tables": tables},
                count_key="tables",
                truncated=False,
            )
        # One row past the limit is asked for: its presence is the truncation signal.
        shown = max(0, limit)
        tables = await handlers.list_tables(database, pattern, shown + 1)
        return _with_meta(
            "list_tables",
            database,
            started,
            {"tables": tables[:shown]},
            count_key="tables",
            truncated=len(tables) > shown,
        )

    @app.tool()
    async def get_table_schema(database: str, table: str, include_indexes: bool = False) -> dict:
        """Get columns, types, and primary/foreign keys for a table.

        Returns `meta`, `table`, `columns` (name, type, nullable, default), `primary_key`
        and `foreign_keys`, plus `indexes` when asked for; a table that does not exist is
        an error.

        Pass include_indexes=true to also get the table's indexes (name, its KEY columns
        in index order — an INCLUDE payload is not listed — whether it is unique, and its
        method: btree, gin, ...). Use it before writing a filter or a JOIN, or when a
        query is slow and you want to know what is actually indexed. Omitted by default,
        so the plain answer stays small.
        """
        started = time.monotonic()
        schema = await handlers.get_table_schema(database, table, include_indexes)
        return _with_meta("get_table_schema", database, started, schema)

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

        Returns `meta`, `database` and `generated_utc`, then `table_count` and `tables`
        (json) or `format` and `ddl` (sql). Schema content is deterministic;
        `meta.elapsed_ms` varies. Returned inline by default; pass output_dir to instead
        WRITE it to `{database}_schema_{UTC}.{json|sql}` and get back `saved_to` plus a
        summary — recommended for large schemas too big to return inline.
        """
        started = time.monotonic()
        schema = await handlers.get_database_schema(database, output_dir, format)
        return _with_meta("get_database_schema", database, started, schema)

    @app.tool()
    async def dump_schema_faithful(database: str, output_dir: str | None = None) -> dict:
        """Produce a byte-faithful schema dump using the database's own tool (pg_dump -s).

        The most complete/runnable schema export (handles sequences, identity, partitioning,
        etc.) but REQUIRES the pg_dump binary on the machine running this server. Returns the
        DDL inline (or writes `{database}_schema_{UTC}.sql` when output_dir is set). If
        pg_dump isn't installed it returns {status: "pg_dump_not_found", message}: offer to
        install pg_dump for the user (see the faithful_schema_export prompt), then retry —
        or use get_database_schema(format="sql") which needs no extra tools.

        Returns `meta`, `status`, `database`, `generated_utc` and `ddl` (or `saved_to`),
        or `meta`, `status` and `message` when pg_dump is missing or fails.
        """
        started = time.monotonic()
        dump = await handlers.dump_schema_faithful(database, output_dir)
        return _with_meta("dump_schema_faithful", database, started, dump)

    @app.tool()
    async def sample_table_rows(database: str, table: str, n: int = 10) -> dict:
        """Fetch up to N rows of a table to learn its data shape.

        Returns `meta` and `rows`, one object per row keyed by column name, in no
        guaranteed order. `n` defaults to 10 and the answer holds at most 1000 rows: a
        larger `n` is clamped to 1000. `meta.truncated` is true only when `n` was over
        1000 and was clamped; it reports that clamp, not whether the table holds more rows.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.

        Values render as on execute_read_query: bytea as `\\x` hex text, a `"char"` column
        (`relkind`, `contype`, `prokind`, ...) as its character (hex when the byte is not
        valid UTF-8), and hex for a `"char"` inside an anonymous `ROW(...)` or an array of
        a domain over `"char"`.
        """
        started = time.monotonic()
        rows = await handlers.sample_table_rows(database, table, min(n, SAMPLE_ROWS_LIMIT))
        return _with_meta(
            "sample_table_rows",
            database,
            started,
            {"rows": rows},
            count_key="rows",
            truncated=n > SAMPLE_ROWS_LIMIT,
        )

    # ---- Discovery / search tools --------------------------------------------
    @app.tool()
    async def find_columns(
        database: str, pattern: str, limit: int | None = None
    ) -> FindColumnsResult:
        """Find columns by name across all tables (fuzzy, case-insensitive substring).

        e.g. pattern "email" matches user_email, EMAIL_ADDRESS. Use this to locate where
        a concept lives before querying. `limit` returns at most that many rows — set it
        when a broad pattern would otherwise match half the database.

        Returns `meta` and `columns`, one item per matching column with `schema`, `table`,
        `column`, `type` and `nullable`. With `limit` set, `meta.truncated` is true when
        more columns matched than were returned.
        """
        started = time.monotonic()
        if limit is None:
            columns = await handlers.find_columns(database, pattern, None)
            return _with_meta(
                "find_columns",
                database,
                started,
                {"columns": columns},
                count_key="columns",
                truncated=False,
            )
        # One row past the limit is asked for: its presence is the truncation signal.
        shown = max(0, limit)
        columns = await handlers.find_columns(database, pattern, shown + 1)
        return _with_meta(
            "find_columns",
            database,
            started,
            {"columns": columns[:shown]},
            count_key="columns",
            truncated=len(columns) > shown,
        )

    @app.tool()
    async def search_value(
        database: str,
        value: str,
        tables: list[str] | None = None,
        limit_per_column: int = 5,
    ) -> dict:
        """Find WHERE a value appears across tables (fuzzy, case-insensitive substring).

        Returns `meta`, `results` and `truncated`: one result per column containing the
        value, with `schema`, `table`, `column`, `matches` (a count) and `samples`.
        For speed on large databases, first narrow with list_tables/find_columns and pass
        a `tables` shortlist; otherwise it scans all non-system tables (bounded, may
        return partial results flagged `truncated`, which `meta.truncated` repeats).

        Matches arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.
        """
        started = time.monotonic()
        found = await handlers.search_value(database, value, tables, limit_per_column)
        return _with_meta(
            "search_value",
            database,
            started,
            found,
            count_key="results",
            truncated=found["truncated"],
        )

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

        Returns `meta`, `columns` and `rows`. There is no row cap: every row comes back in
        one answer, so for a large result use open_query_cursor and fetch_rows instead.
        A `SELECT` with no columns answers one empty object per row.

        Result shape: `columns` lists every column the statement returned, but each row
        is keyed by name, so a repeated name (`SELECT 1 AS a, 2 AS a`) keeps only its last
        value: alias the columns to keep both. Rendering follows the server's column type:
        bytea arrives as `\\x` hex text, a `"char"` column (`relkind`, `contype`, `prokind`,
        ...) as its character (hex when the byte is not valid UTF-8), and a `"char"` inside
        an anonymous `ROW(...)` as hex because the row description says only `record`. An
        array of a domain over `"char"` also reads as hex, because that array has its own
        type.
        """
        started = time.monotonic()
        result = await handlers.execute_read_query(database, sql, params, timeout_ms)
        return _with_meta(
            "execute_read_query", database, started, result, count_key="rows", truncated=False
        )

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
           transaction and is ALWAYS rolled back, returning its would-be
           `rows_affected`, or the `RETURNING` rows. This records a preview grant for
           this exact statement. `RETURNING` values from a dry-run are rolled back and
           are not the values the commit will produce (sequences advance, so generated
           ids differ): never quote a preview id as the real one.
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

        Returns `meta` and `rows_affected`, or `columns` and `rows` for a `RETURNING`
        statement; a dry-run also carries `dry_run` and `rolled_back`.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.

        Result shape: `columns` lists every column `RETURNING` named, but each row is
        keyed by name, so a repeated name (`RETURNING id, id`) keeps only its last value:
        alias the columns to keep both. Rendering follows the server's column type: bytea
        arrives as `\\x` hex text, a `"char"` column (`relkind`, `contype`, `prokind`, ...)
        as its character (hex when the byte is not valid UTF-8), and a `"char"` inside an
        anonymous `ROW(...)` as hex because the row description says only `record`. An array
        of a domain over `"char"` also reads as hex, because that array has its own type. An
        error saying the statement ran but its rows could not be decoded means a commit is to be
        treated as applied: check with a read before running it again.
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
        started = time.monotonic()
        result = await handlers.execute_write_query(
            database,
            sql,
            params,
            user_consent=user_consent,
            dry_run=dry_run,
            skip_dry_run=skip_dry_run,
            timeout_ms=timeout_ms,
            session=session,
        )
        return _with_meta("execute_write_query", database, started, result, count_key="rows")

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

        Returns `meta` and `plan`, the plan's lines in order.
        """
        started = time.monotonic()
        plan = await handlers.explain_query(database, sql, analyze, params)
        return _with_meta("explain_query", database, started, plan)

    @app.tool()
    async def cancel_query(database: str, pid: int) -> dict:
        """Cancel the statement running in server backend `pid` (find pids via show_activity).

        Returns `meta`, `pid` and `cancelled`. Cancels the statement only — the session
        survives. cancelled=false means the pid doesn't exist or you lack permission (same
        database user required).
        """
        started = time.monotonic()
        cancelled = await handlers.cancel_query(database, pid)
        return _with_meta("cancel_query", database, started, cancelled)

    # ---- Cursor tools (large result sets) -------------------------------------
    @app.tool()
    async def open_query_cursor(database: str, sql: str, params: list[Any] | None = None) -> dict:
        """Open a server-side cursor over a read-only query for batched fetching.

        Use for results too large for one execute_read_query response.
        Returns `meta`, `cursor_id` (for fetch_rows/close_cursor) and `database`. The
        cursor pins one read-only connection until closed (auto-reaped after 15 idle
        minutes; max 5 open).
        """
        started = time.monotonic()
        cursor = await handlers.open_query_cursor(database, sql, params)
        return _with_meta("open_query_cursor", database, started, cursor)

    @app.tool()
    async def fetch_rows(cursor_id: str, n: int = 100) -> dict:
        """Fetch the next N rows from an open cursor (see open_query_cursor).

        Returns `meta`, `rows`, `row_count`, `exhausted` and `cursor_closed`;
        `meta.database` is null, since a cursor_id names no database. The cursor closes
        itself once fully drained, and also when the read fails: the error is raised and
        the cursor_id stops working, so re-open the query to read again. Fetch one batch
        at a time: a call made while another is running on the same cursor is refused,
        and the cursor and the running call carry on.

        Rows arrive on the text channel only, inside the untrusted-data banner: this
        tool emits no structuredContent.

        Values render as on execute_read_query: bytea as `\\x` hex text, a `"char"` column
        (`relkind`, `contype`, `prokind`, ...) as its character (hex when the byte is not
        valid UTF-8), and hex for a `"char"` inside an anonymous `ROW(...)` or an array of
        a domain over `"char"`.
        """
        started = time.monotonic()
        batch = await handlers.fetch_rows(cursor_id, n)
        return _with_meta("fetch_rows", None, started, batch, count_key="rows")

    @app.tool()
    async def close_cursor(cursor_id: str) -> dict:
        """Close an open cursor and release its database connection. Idempotent.

        Returns `meta`, `cursor_id`, `closed` and `was_open`.
        """
        started = time.monotonic()
        closed = await handlers.close_cursor(cursor_id)
        return _with_meta("close_cursor", None, started, closed)

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

        Returns `meta`, `object_type`, `name` and `matches`, each with its schema, name,
        kind and definition; no match is an error.
        """
        started = time.monotonic()
        found = await handlers.get_object_definition(database, object_type, name)
        return _with_meta("get_object_definition", database, started, found, count_key="matches")

    # ---- Operational insight tools ----------------------------------------------
    @app.tool()
    async def diff_schemas(database_a: str, database_b: str) -> dict:
        """Compare the schemas of two configured databases (tables, columns, PK/FK).

        Reports tables only in one side, and per-table column/type/default/key
        differences. Use to verify a migrated copy matches its source of truth.

        Returns `meta`, `database_a`, `database_b`, `only_in_a`, `only_in_b`,
        `changed_tables` and `identical`; `meta.database` is null, since two are compared.
        """
        started = time.monotonic()
        diff = await handlers.diff_schemas(database_a, database_b)
        return _with_meta("diff_schemas", None, started, diff)

    @app.tool()
    async def check_sequences(database: str, behind_only: bool = True) -> dict:
        """Find sequences lagging behind their column's max value (stale after migrations).

        A `behind: true` sequence would hand out an id that already exists — fix with
        setval(). Run this after any bulk copy or logical-replication migration. By
        default only the problem sequences are listed, with `total_sequences` reporting
        how many were checked and `skipped` listing the ones that could not be probed —
        so `behind_count: 0` means "all clear" only when `skipped_count` is 0. A skipped
        entry's `reason` is `no_privilege` (grant USAGE on the schema and SELECT on the
        sequence and its table) or `probe_failed` (the owning column is not numeric or the
        sequence could not be read). Pass `behind_only=false` for the full census; on a
        large schema that is one row per sequence (hundreds), so ask for it only when you
        need every sequence's state.

        Returns `meta`, `database`, `sequences`, `behind_count`, `total_sequences`,
        `skipped` and `skipped_count`.
        """
        started = time.monotonic()
        report = await handlers.check_sequences(database, behind_only)
        return _with_meta("check_sequences", database, started, report, count_key="sequences")

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

        Returns `meta`, `database` and `tables`, plus `truncated` when `limit` is set;
        `meta.truncated` repeats it, and is false without `limit`.
        """
        started = time.monotonic()
        stats = await handlers.table_stats(database, limit, min_size_bytes, table)
        return _with_meta(
            "table_stats",
            database,
            started,
            stats,
            count_key="tables",
            truncated=stats.get("truncated", False),
        )

    @app.tool()
    async def show_activity(database: str, include_query: bool = False) -> dict:
        """Show current server activity: pid, state, wait events, query/transaction age.

        Sanitized by default — no user names, client addresses, or query text (pass
        include_query=true for truncated query text). Answers "what is holding
        connections"; pair with cancel_query(pid) to kill a stuck statement.

        Returns `meta`, `database` and `activity`, one item per server backend.
        """
        started = time.monotonic()
        activity = await handlers.show_activity(database, include_query)
        return _with_meta("show_activity", database, started, activity, count_key="activity")

    # ---- Configuration tool --------------------------------------------------
    @app.tool()
    async def set_yolo_mode(database: str, enabled: bool) -> dict:
        """Enable/disable yolo (skip the write-consent prompt) for one database, persisted.

        Returns `meta`, `database`, `yolo` and `persisted`, plus `audit_recorded` and
        `warning` when the audit line could not be written.

        A call cancelled while the write was in progress may still have been applied;
        list_databases shows the stored value. A result with audit_recorded: false means
        the flip persisted but no audit line was written.
        """
        started = time.monotonic()
        flipped = await handlers.set_yolo_mode(database, enabled)
        return _with_meta("set_yolo_mode", database, started, flipped)

    # ---- Diagnostics tool ----------------------------------------------------
    @app.tool()
    async def check_database(database: str | None = None) -> CheckDatabaseResult:
        """Test connectivity for one database (or all) — returns OK or a sanitized cause.

        Returns `meta` and `results`, one item per database with `database` and `status`
        (OK or UNREACHABLE; an unreachable one adds category and detail). `meta.database`
        is null when every database was checked. Reports active_port when the database
        answered on a fallback port — doubles as a live re-probe for tunneled connections.
        """
        started = time.monotonic()
        results = await handlers.check_database(database)
        return _with_meta(
            "check_database", database, started, {"results": results}, count_key="results"
        )

    @app.tool()
    async def doctor(offline: bool = False) -> DoctorResult:
        """Diagnose the whole setup, not just connectivity — use when something is
        misbehaving and check_database alone doesn't explain why.

        Runs every diagnostic: stale running server processes, newer PyPI release,
        connections.json key typos/types, secrets exposure (permissions, git),
        MCP client entries pointing at dead paths, and per-database connectivity
        with a credential-free fallback-port identity probe.

        Returns `meta` and `findings`, each with `check`, `status` (ok|warn|fail|skipped),
        `detail` and `suggested_action`, where suggested_action is machine-actionable:
        reconnect_client, upgrade_package, swap_primary_port, fix_permissions, fix_config,
        repair_client_config, fix_connection, install_doctor_extra, run_setup, report_bug,
        or none — every finding that states a remedy names one. Fix what you can; report
        the rest to the user verbatim. Output is sanitized — never contains DSNs, hosts,
        or credentials. offline=true skips the PyPI lookup.
        """
        started = time.monotonic()
        # Existence is re-evaluated per call (like the CLI): the config file can be
        # deleted after startup, and run_checks() reads None as "no configuration".
        path = handlers.config_path
        findings = await doctor_mod.run_checks(path if path.is_file() else None, offline=offline)
        return _with_meta("doctor", None, started, {"findings": findings}, count_key="findings")

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
