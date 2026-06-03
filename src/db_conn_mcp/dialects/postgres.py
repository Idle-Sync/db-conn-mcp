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

# Leading keywords that begin a read-only, row-returning statement. This single set
# does double duty: execute() uses it to shape the result ({columns, rows}), and the
# read tool uses it as an allowlist (validate_read_only). The overlap is the point —
# because every permitted read leader is row-returning, the statement runs through
# asyncpg's extended (prepared-statement) protocol via fetch(), which refuses to run
# more than one command. So banning non-read leaders also bans a trailing "; DELETE".
_READ_ONLY_LEADERS = frozenset({"select", "with", "values", "table", "show", "explain"})

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


def _leading_keyword(sql: str) -> str:
    """Return the first SQL keyword, lowercased, ignoring leading whitespace/comments.

    Leading ``--`` line comments and ``/* */`` block comments are skipped so a query
    like ``-- note\\nSELECT 1`` still reports ``select``. Returns ``""`` for blank or
    comment-only input. Used both to shape results and to gate the read tool.
    """
    s = sql.lstrip()
    while s:
        if s.startswith("--"):
            newline = s.find("\n")
            s = "" if newline == -1 else s[newline + 1 :].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :].lstrip()
        else:
            break
    return s.split(None, 1)[0].lower() if s else ""


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

    def validate_read_only(self, sql: str) -> None:
        """Reject anything the read tool must not run (see :meth:`Dialect.validate_read_only`).

        The bar is a single read-only, row-returning statement. Requiring a leader in
        :data:`_READ_ONLY_LEADERS` bans ``SET`` (so the session can't be flipped out of
        ``READ ONLY``) and every DML/DDL command; and since those leaders all route to
        ``fetch()`` — asyncpg's extended protocol, which runs exactly one command — a
        piggy-backed ``; DELETE ...`` is refused too. No SQL parsing, by design.
        """
        leader = _leading_keyword(sql)
        if not leader:
            raise ValueError("Empty SQL: provide a single read-only query.")
        if leader not in _READ_ONLY_LEADERS:
            allowed = ", ".join(sorted(_READ_ONLY_LEADERS)).upper()
            raise ValueError(
                f"Read query must be a single read-only statement starting with one of: "
                f"{allowed}. Got {leader.upper()!r}. Use a write-mode database and "
                "execute_write_query for INSERT/UPDATE/DELETE/DDL or SET."
            )

    @staticmethod
    def _is_row_returning(sql: str) -> bool:
        """Heuristic: does this statement return a result set?"""
        return _leading_keyword(sql) in _READ_ONLY_LEADERS


def _parse_affected(status: str) -> int:
    """Extract the affected-row count from an asyncpg command tag like ``UPDATE 3``."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
