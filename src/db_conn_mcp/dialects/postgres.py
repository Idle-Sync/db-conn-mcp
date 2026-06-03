"""PostgreSQL dialect (``asyncpg``) — the only implementation in v1.

The only module in the project that knows it is talking to PostgreSQL. Read-only is
enforced natively here via session characteristics; introspection uses
``information_schema``; identifiers are safely quoted before interpolation (Rule 9).

.. note::
   The unit tests exercise query shaping and identifier quoting against a fake
   connection. A full "read-only really blocks a write" assertion requires a live
   server and belongs in a Docker-backed integration test (design spec §12).
"""

from typing import Any

import asyncpg

from .base import Dialect

#: DSN schemes routed to this dialect.
SCHEMES = ("postgresql", "postgres")

#: Native, session-level read-only enforcement (the Postgres mechanism).
_READ_ONLY_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"

# Leading keywords whose statements return rows (so execute() shapes {columns, rows}).
_ROW_RETURNING = ("select", "with", "values", "table", "show", "explain")

_LIST_TABLES_SQL = """
    SELECT table_schema AS schema,
           table_name   AS name,
           CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name
"""

_COLUMNS_SQL = """
    SELECT column_name                   AS name,
           data_type                     AS type,
           (is_nullable = 'YES')         AS nullable,
           column_default                AS default
    FROM information_schema.columns
    WHERE table_name = $1 AND ($2::text IS NULL OR table_schema = $2)
    ORDER BY ordinal_position
"""

# Primary- and foreign-key columns for one table, with the FK target as "table.column".
_KEYS_SQL = """
    SELECT kcu.column_name AS column,
           tc.constraint_type AS constraint_type,
           CASE WHEN tc.constraint_type = 'FOREIGN KEY'
                THEN ccu.table_name || '.' || ccu.column_name
                ELSE NULL END AS references
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.constraint_schema = tc.constraint_schema
    LEFT JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name
     AND ccu.constraint_schema = tc.constraint_schema
    WHERE tc.table_name = $1 AND ($2::text IS NULL OR tc.table_schema = $2)
      AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
"""


def _quote_identifier(identifier: str) -> str:
    """Safely quote a (optionally ``schema.table``) SQL identifier (Rule 9).

    Mirrors Postgres ``quote_ident``: wrap each dotted part in double quotes and
    double any embedded double quotes, so a hostile name can never break out into
    runnable SQL. Empty parts or null bytes are rejected.
    """
    if not identifier or "\x00" in identifier:
        raise ValueError("Invalid SQL identifier.")
    parts = identifier.split(".")
    if any(part == "" for part in parts):
        raise ValueError("Invalid SQL identifier.")
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts)


def _split_schema_table(table: str) -> tuple[str, str | None]:
    """Return ``(table_name, schema_or_None)`` for introspection parameters."""
    if "." in table:
        schema, name = table.split(".", 1)
        return name, schema
    return table, None


class PostgresDialect(Dialect):
    """``asyncpg``-backed PostgreSQL dialect."""

    scheme = "postgresql"

    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        conn = await asyncpg.connect(dsn)
        if read_only:
            # Enforce read-only natively before the caller can run anything.
            await conn.execute(_READ_ONLY_SQL)
        return conn

    async def list_tables(self, conn: Any) -> list[dict]:
        rows = await conn.fetch(_LIST_TABLES_SQL)
        return [dict(r) for r in rows]

    async def get_schema(self, conn: Any, table: str) -> dict:
        name, schema = _split_schema_table(table)
        columns = await conn.fetch(_COLUMNS_SQL, name, schema)
        keys = [dict(k) for k in await conn.fetch(_KEYS_SQL, name, schema)]
        primary_key = [k["column"] for k in keys if k["constraint_type"] == "PRIMARY KEY"]
        foreign_keys = [
            {"column": k["column"], "references": k["references"]}
            for k in keys
            if k["constraint_type"] == "FOREIGN KEY"
        ]
        return {
            "table": table,
            "columns": [dict(c) for c in columns],
            "primary_key": primary_key,
            "foreign_keys": foreign_keys,
        }

    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        limit = max(0, int(n))  # coerce to a safe non-negative int (never interpolate raw)
        sql = f"SELECT * FROM {_quote_identifier(table)} LIMIT {limit}"
        rows = await conn.fetch(sql)
        return [dict(r) for r in rows]

    async def execute(self, conn: Any, sql: str) -> dict:
        if self._is_row_returning(sql):
            rows = await conn.fetch(sql)
            mapped = [dict(r) for r in rows]
            columns = list(mapped[0].keys()) if mapped else []
            return {"columns": columns, "rows": mapped}
        status = await conn.execute(sql)
        return {"rows_affected": _parse_affected(status)}

    @staticmethod
    def _is_row_returning(sql: str) -> bool:
        """Heuristic: does this statement return a result set?"""
        return sql.lstrip().split(None, 1)[0].lower() in _ROW_RETURNING if sql.strip() else False


def _parse_affected(status: str) -> int:
    """Extract the affected-row count from an asyncpg command tag like ``UPDATE 3``."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
