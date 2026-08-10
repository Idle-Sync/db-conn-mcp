"""The MCP server: 23 tools + 2 prompts, plus transport wiring (FastMCP).

Knows nothing about PostgreSQL. It wires the pure/abstract layers together: the
:class:`~db_conn_mcp.handlers.Handlers` service (which uses ``config``, the dialect
registry, ``safety``, and ``diagnostics``) onto FastMCP tools and a prompt.

We use FastMCP — the official high-level SDK API — over the lower-level
``mcp.server.Server`` because it is radically simpler (Rule 1: simplicity first) while
remaining the same SDK.
"""

from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import doctor as doctor_mod
from .config import resolve_path
from .handlers import Handlers

Transport = Literal["stdio", "http"]

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


def build_server(config_path: Path | str | None = None) -> FastMCP:
    """Construct the FastMCP app with all 23 tools and 2 prompts."""
    resolved = resolve_path(str(config_path) if config_path else None)
    handlers = Handlers(resolved)
    app = FastMCP("db-conn-mcp")  # 23 tools + 2 prompts registered below

    # ---- Exploration tools ---------------------------------------------------
    @app.tool()
    async def list_databases() -> list[dict]:
        """List configured databases with their allowed mode and yolo flag (no DSN).

        Entries with fallback_ports configured also show them, plus active_port when a
        fallback port is currently in use.
        """
        return await handlers.list_databases()

    @app.tool()
    async def list_tables(database: str) -> list[dict]:
        """List all tables and views in the named database."""
        return await handlers.list_tables(database)

    @app.tool()
    async def get_table_schema(database: str, table: str) -> dict:
        """Get columns, types, and primary/foreign keys for a table."""
        return await handlers.get_table_schema(database, table)

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
        """Fetch the first N rows of a table to learn its data shape."""
        return await handlers.sample_table_rows(database, table, n)

    # ---- Discovery / search tools --------------------------------------------
    @app.tool()
    async def find_columns(database: str, pattern: str) -> list[dict]:
        """Find columns by name across all tables (fuzzy, case-insensitive substring).

        e.g. pattern "email" matches user_email, EMAIL_ADDRESS. Use this to locate where
        a concept lives before querying.
        """
        return await handlers.find_columns(database, pattern)

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

        ALWAYS pass user-supplied values via `params` with $1/$2/... placeholders in the
        SQL (real driver bind parameters — no quoting/injection pitfalls), not by pasting
        them into the SQL string. Optional timeout_ms cancels an overrunning query.
        """
        return await handlers.execute_read_query(database, sql, params, timeout_ms)

    @app.tool()
    async def execute_write_query(
        database: str,
        sql: str,
        params: list[Any] | None = None,
        user_consent: bool = False,
        dry_run: bool = True,
        skip_dry_run: bool = False,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run an INSERT/UPDATE/DELETE/DDL statement on a write-mode database.

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
        return await handlers.execute_write_query(
            database,
            sql,
            params,
            user_consent=user_consent,
            dry_run=dry_run,
            skip_dry_run=skip_dry_run,
            timeout_ms=timeout_ms,
        )

    @app.tool()
    async def explain_query(database: str, sql: str, analyze: bool = False) -> dict:
        """Show the execution plan for a read-only query (EXPLAIN; optionally ANALYZE).

        Use to confirm index usage or diagnose a slow query. analyze=true actually runs
        the (read-only, validated) query to collect real timings and buffer stats.
        """
        return await handlers.explain_query(database, sql, analyze)

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
    async def check_sequences(database: str) -> dict:
        """Find sequences lagging behind their column's max value (stale after migrations).

        A `behind: true` sequence would hand out an id that already exists — fix with
        setval(). Run this after any bulk copy or logical-replication migration.
        """
        return await handlers.check_sequences(database)

    @app.tool()
    async def table_stats(database: str) -> dict:
        """Approximate row counts and disk/index sizes per table, largest first.

        Uses the database's own statistics — fast, no table scans. Useful for planning
        migration copy order and spotting anomalies.
        """
        return await handlers.table_stats(database)

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
        swap_primary_port, fix_permissions, fix_config, repair_client_config, or
        none. Fix what you can; report the rest to the user verbatim. Output is
        sanitized — never contains DSNs, hosts, or credentials. offline=true skips
        the PyPI lookup.
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

    return app


def run(transport: Transport = "stdio", config_path: str | None = None) -> None:
    """Launch the server over the chosen transport (``stdio`` default, or ``http``/SSE)."""
    app = build_server(config_path)
    # Map our public "http" name to FastMCP's SSE transport (design: http == SSE).
    fastmcp_transport = "sse" if transport == "http" else "stdio"
    app.run(transport=fastmcp_transport)
