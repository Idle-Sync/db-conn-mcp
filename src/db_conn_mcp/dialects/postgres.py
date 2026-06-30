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


#: Every column of every non-system base table, ordered for byte-stable output.
_DB_COLUMNS_SQL = """
    SELECT c.table_schema AS schema,
           c.table_name   AS table,
           c.column_name  AS name,
           c.data_type    AS type,
           (c.is_nullable = 'YES') AS nullable,
           c.column_default AS default
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema
     AND t.table_name = c.table_name
     AND t.table_type = 'BASE TABLE'
    WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

#: Every PK/FK column of every base table (FK target as "table.column"), stably ordered.
_DB_KEYS_SQL = """
    SELECT tc.table_schema AS schema,
           tc.table_name   AS table,
           kcu.column_name AS column,
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
    WHERE tc.table_schema NOT IN ('pg_catalog', 'information_schema')
      AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
    ORDER BY tc.table_schema, tc.table_name, tc.constraint_type, kcu.ordinal_position
"""


#: Fuzzy column-name search (Tool A). ``$1`` is the parameterized pattern.
_FIND_COLUMNS_SQL = """
    SELECT table_schema AS schema,
           table_name   AS table,
           column_name  AS column,
           data_type    AS type,
           (is_nullable = 'YES') AS nullable
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
      AND column_name ILIKE '%' || $1 || '%'
    ORDER BY table_schema, table_name, ordinal_position
"""

#: Base-table columns (excludes views/system schemas); ``$1::text[]`` (NULL = all)
#: restricts to specific table names for a scoped value search.
_SEARCH_COLUMNS_SQL = """
    SELECT c.table_schema AS schema,
           c.table_name   AS table,
           c.column_name  AS column
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema
     AND t.table_name = c.table_name
     AND t.table_type = 'BASE TABLE'
    WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
      AND ($1::text[] IS NULL OR c.table_name = ANY($1::text[]))
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

#: Bound a value-search scan so a wide/huge scan fails safe instead of hanging.
_SEARCH_TIMEOUT_SQL = "SET statement_timeout = '8s'"

#: Tables skipped by default in an unscoped value search (framework/replication/stats).
_JUNK_TABLES = frozenset({"migrations", "jobs", "failed_jobs", "password_resets"})
_JUNK_PREFIXES = ("awsdms_", "pg_stat")


def _is_junk_table(name: str) -> bool:
    """Whether a table is noise skipped by default (an explicit ``tables=`` overrides this)."""
    return name in _JUNK_TABLES or name.startswith(_JUNK_PREFIXES)


def _build_table_search_sql(schema: str, table: str, columns: list[str], limit: int) -> str:
    """Single-pass aggregate that, per column, counts and samples fuzzy text matches.

    Scans the table once. ``$1`` is the (parameterized) search value; every column is
    cast to text and matched case-insensitively (``ILIKE``). Identifiers are safely
    quoted (Rule 9); the value is never interpolated.
    """
    qtable = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    parts: list[str] = []
    for i, col in enumerate(columns):
        qc = f"{_quote_identifier(col)}::text"
        cond = f"{qc} ILIKE '%' || $1 || '%'"
        parts.append(f"count(*) FILTER (WHERE {cond}) AS m_{i}")
        parts.append(f"(array_agg(DISTINCT {qc}) FILTER (WHERE {cond}))[1:{limit}] AS s_{i}")
    return f"SELECT {', '.join(parts)} FROM {qtable}"


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

    async def get_database_schema(self, conn: Any) -> dict:
        """Assemble the whole-database schema from two ordered catalog scans.

        One pass over columns and one over key constraints, both pre-sorted by the
        query, are stitched into ``{table -> {columns, primary_key, foreign_keys}}``.
        Dict insertion order preserves the SQL ``ORDER BY``, so the result is
        deterministic across runs. Pure assembly — no identifiers are interpolated.
        """
        col_rows = await conn.fetch(_DB_COLUMNS_SQL)
        key_rows = await conn.fetch(_DB_KEYS_SQL)

        tables: dict[tuple[str, str], dict] = {}
        for r in col_rows:
            d = dict(r)
            entry = tables.get((d["schema"], d["table"]))
            if entry is None:
                entry = {
                    "schema": d["schema"],
                    "name": d["table"],
                    "columns": [],
                    "primary_key": [],
                    "foreign_keys": [],
                }
                tables[(d["schema"], d["table"])] = entry
            entry["columns"].append(
                {
                    "name": d["name"],
                    "type": d["type"],
                    "nullable": d["nullable"],
                    "default": d["default"],
                }
            )

        for r in key_rows:
            d = dict(r)
            entry = tables.get((d["schema"], d["table"]))
            if entry is None:  # a constraint on something without listed columns — skip
                continue
            if d["constraint_type"] == "PRIMARY KEY":
                entry["primary_key"].append(d["column"])
            else:
                entry["foreign_keys"].append({"column": d["column"], "references": d["references"]})

        return {"tables": list(tables.values())}

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

    async def find_columns(self, conn: Any, pattern: str) -> list[dict]:
        rows = await conn.fetch(_FIND_COLUMNS_SQL, pattern)
        return [dict(r) for r in rows]

    async def search_value(
        self, conn: Any, value: str, tables: list[str] | None = None, limit_per_column: int = 5
    ) -> dict:
        limit = max(1, int(limit_per_column))
        await conn.execute(_SEARCH_TIMEOUT_SQL)  # fail safe on a wide/huge scan
        col_rows = await conn.fetch(_SEARCH_COLUMNS_SQL, list(tables) if tables else None)

        grouped: dict[tuple[str, str], list[str]] = {}
        for r in col_rows:
            d = dict(r)
            grouped.setdefault((d["schema"], d["table"]), []).append(d["column"])

        results: list[dict] = []
        truncated = False
        for (schema, table), cols in grouped.items():
            if not tables and _is_junk_table(table):
                continue
            sql = _build_table_search_sql(schema, table, cols, limit)
            try:
                row = await conn.fetchrow(sql, value)
            except asyncpg.QueryCanceledError:  # statement_timeout tripped
                truncated = True
                break
            except asyncpg.PostgresError:  # one unscannable table — skip, keep going
                continue
            if row is None:
                continue
            mapped = dict(row)
            for i, col in enumerate(cols):
                matches = mapped.get(f"m_{i}") or 0
                if matches:
                    results.append(
                        {
                            "schema": schema,
                            "table": table,
                            "column": col,
                            "matches": int(matches),
                            "samples": list(mapped.get(f"s_{i}") or []),
                        }
                    )
        return {"results": results, "truncated": truncated}

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
