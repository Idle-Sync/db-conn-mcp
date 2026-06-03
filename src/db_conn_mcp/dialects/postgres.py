"""PostgreSQL dialect (``asyncpg``) — the only implementation in v1.

The only module in the project that knows it is talking to PostgreSQL. Read-only is
enforced natively here via session characteristics; introspection uses
``information_schema``; identifiers are safely quoted before interpolation (Rule 9).
"""

from typing import Any

from .base import Dialect

#: DSN schemes routed to this dialect.
SCHEMES = ("postgresql", "postgres")


class PostgresDialect(Dialect):
    """``asyncpg``-backed PostgreSQL dialect."""

    scheme = "postgresql"

    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        # Open via asyncpg; when read_only, run
        # `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` before returning.
        raise NotImplementedError

    async def list_tables(self, conn: Any) -> list[dict]:
        # Query information_schema.tables (tables + views).
        raise NotImplementedError

    async def get_schema(self, conn: Any, table: str) -> dict:
        # Query information_schema.columns + table_constraints/key_column_usage.
        raise NotImplementedError

    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        # SELECT * ... LIMIT n, with the identifier safely quoted (never raw f-string).
        raise NotImplementedError

    async def execute(self, conn: Any, sql: str) -> dict:
        # Run raw SQL; shape result as {columns, rows} or {rows_affected}.
        raise NotImplementedError
