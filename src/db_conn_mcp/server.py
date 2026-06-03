"""The MCP server: 8 tools + 1 prompt, plus transport wiring.

Knows nothing about PostgreSQL. It orchestrates the pure/abstract layers:
``config`` (what DBs exist), ``dialects.registry`` (how to talk to one),
``safety`` (the write gate), and ``diagnostics`` (sanitized errors). Every tool that
opens a connection wraps failures through :func:`diagnostics.explain`.
"""

from typing import Literal

Transport = Literal["stdio", "http"]


# ---- Tool handlers (see design spec §8.1) -------------------------------------
# 1. list_databases(database?)            -> [{name, mode, yolo}]   (never dsn)
# 2. list_tables(database)                -> [{schema, name, kind}]
# 3. get_table_schema(database, table)    -> columns/types/PK/FK
# 4. sample_table_rows(database, table, n=10) -> rows
# 5. execute_read_query(database, sql)    -> {columns, rows}  (read-only txn)
# 6. execute_write_query(database, sql, user_consent=False) -> gated by safety
# 7. set_yolo_mode(database, enabled)     -> persisted via config.set_yolo
# 8. check_database(database?)            -> OK | sanitized diagnostic
#
# ---- Prompt -------------------------------------------------------------------
# troubleshoot_connection -> full connection-gotchas checklist


def build_server():
    """Construct and return the configured MCP ``Server`` with all tools/prompts."""
    raise NotImplementedError


async def run(transport: Transport = "stdio", config_path: str | None = None) -> None:
    """Launch the server over the chosen transport (``stdio`` default, or ``http``)."""
    raise NotImplementedError
