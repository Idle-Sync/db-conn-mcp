"""The MCP server: 11 tools + 1 prompt, plus transport wiring (FastMCP).

Knows nothing about PostgreSQL. It wires the pure/abstract layers together: the
:class:`~db_conn_mcp.handlers.Handlers` service (which uses ``config``, the dialect
registry, ``safety``, and ``diagnostics``) onto FastMCP tools and a prompt.

We use FastMCP — the official high-level SDK API — over the lower-level
``mcp.server.Server`` because it is radically simpler (Rule 1: simplicity first) while
remaining the same SDK.
"""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

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


def build_server(config_path: Path | str | None = None) -> FastMCP:
    """Construct the FastMCP app with all 11 tools and the troubleshoot prompt."""
    resolved = resolve_path(str(config_path) if config_path else None)
    handlers = Handlers(resolved)
    app = FastMCP("db-conn-mcp")

    # ---- Exploration tools ---------------------------------------------------
    @app.tool()
    async def list_databases() -> list[dict]:
        """List configured databases with their allowed mode and yolo flag (no DSN)."""
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
    async def get_database_schema(database: str) -> dict:
        """Get the whole database's schema at once: every table with its columns,
        types, nullability, primary key, and foreign keys.

        Deterministic (tables sorted by schema/name, columns by position) so repeated
        calls return identical output. Use this to grab the full structure in one shot
        instead of calling get_table_schema per table.
        """
        return await handlers.get_database_schema(database)

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
    async def execute_read_query(database: str, sql: str) -> dict:
        """Run a read-only SELECT query (enforced as a read-only transaction)."""
        return await handlers.execute_read_query(database, sql)

    @app.tool()
    async def execute_write_query(database: str, sql: str, user_consent: bool = False) -> dict:
        """Run an INSERT/UPDATE/DELETE/DDL statement on a write-mode database.

        SAFETY: Only allowed if the database is mode=write. Unless the database has
        yolo enabled, you MUST first read the target table and its schema, then show
        the user the EXACT SQL you intend to run and ask for explicit permission. Only
        call again with user_consent=true if the user clearly says yes.
        """
        return await handlers.execute_write_query(database, sql, user_consent)

    # ---- Configuration tool --------------------------------------------------
    @app.tool()
    async def set_yolo_mode(database: str, enabled: bool) -> dict:
        """Enable/disable yolo (skip the write-consent prompt) for one database, persisted."""
        return await handlers.set_yolo_mode(database, enabled)

    # ---- Diagnostics tool ----------------------------------------------------
    @app.tool()
    async def check_database(database: str | None = None) -> list[dict]:
        """Test connectivity for one database (or all) — returns OK or a sanitized cause."""
        return await handlers.check_database(database)

    # ---- Prompt --------------------------------------------------------------
    @app.prompt()
    def troubleshoot_connection() -> str:
        """The full database-connection gotchas checklist."""
        return TROUBLESHOOT_CHECKLIST

    return app


def run(transport: Transport = "stdio", config_path: str | None = None) -> None:
    """Launch the server over the chosen transport (``stdio`` default, or ``http``/SSE)."""
    app = build_server(config_path)
    # Map our public "http" name to FastMCP's SSE transport (design: http == SSE).
    fastmcp_transport = "sse" if transport == "http" else "stdio"
    app.run(transport=fastmcp_transport)
