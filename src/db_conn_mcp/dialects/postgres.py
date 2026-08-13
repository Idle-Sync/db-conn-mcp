"""PostgreSQL dialect (``asyncpg``) — the only implementation in v1.

The only module in the project that knows it is talking to PostgreSQL. Read-only is
enforced natively here via session characteristics; introspection uses
``information_schema``; identifiers are safely quoted before interpolation (Rule 9).

.. note::
   The unit tests exercise query shaping and identifier quoting against a fake
   connection. A full "read-only really blocks a write" assertion requires a live
   server and belongs in a Docker-backed integration test (design spec §12).
"""

import asyncio
import contextlib
import os
import re
import shutil
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import asyncpg

from .base import Dialect

#: DSN schemes routed to this dialect.
SCHEMES = ("postgresql", "postgres")

#: Native, session-level read-only enforcement (the Postgres mechanism).
_READ_ONLY_SQL = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"

#: Timeout for each credential-free listener probe (doctor's port-identity check).
PROBE_TIMEOUT_SECONDS = 3.0

#: The 8-byte PostgreSQL SSLRequest: length=8, code=80877103 (magic constant).
_SSL_REQUEST = (8).to_bytes(4, "big") + (80877103).to_bytes(4, "big")

# Leading keywords that begin a read-only, row-returning statement. This single set
# does double duty: execute() uses it to shape the result ({columns, rows}), and the
# read tool uses it as an allowlist (validate_read_only). The overlap is the point —
# because every permitted read leader is row-returning, the statement runs through
# asyncpg's extended (prepared-statement) protocol via fetch(), which refuses to run
# more than one command. So banning non-read leaders also bans a trailing "; DELETE".
_READ_ONLY_LEADERS = frozenset({"select", "with", "values", "table", "show", "explain"})

#: Tables and views. Split so :func:`_build_list_tables_sql` can add a name filter
#: between the two halves; the unfiltered text is :data:`_LIST_TABLES_SQL`.
_LIST_TABLES_SELECT = """
    SELECT table_schema AS schema,
           table_name   AS name,
           CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"""

_LIST_TABLES_ORDER = "\n    ORDER BY table_schema, table_name\n"

_LIST_TABLES_SQL = _LIST_TABLES_SELECT + _LIST_TABLES_ORDER

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


# Indexes on one table, from the native catalogs. The table is resolved exactly like
# _COLUMNS_SQL/_KEYS_SQL do it — bound name plus optional bound schema (Rule 9). Each
# key column is rendered by Postgres itself via pg_get_indexdef(oid, ordinal, pretty),
# so an expression index reports its expression rather than a missing name.
# indnkeyatts, not indnatts: the latter counts INCLUDE payload columns as well, and
# listing those among the key columns would claim a lookup they cannot serve.
_INDEXES_SQL = """
    SELECT i.relname AS name,
           ARRAY(
               SELECT pg_get_indexdef(ix.indexrelid, k.ordinal, true)
               FROM generate_series(1, ix.indnkeyatts) AS k(ordinal)
               ORDER BY k.ordinal
           ) AS columns,
           ix.indisunique AS unique,
           am.amname AS method
    FROM pg_index ix
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_am am ON am.oid = i.relam
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = $1 AND ($2::text IS NULL OR n.nspname = $2)
    ORDER BY i.relname
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


# ---- Self-contained DDL export (get_database_ddl) ----------------------------
# Seven ordered catalog scans whose fragments stitch into a runnable script. Every
# identifier/definition is produced by Postgres itself (quote_ident / pg_get_*def), so
# nothing is hand-interpolated. ``pg\_%`` excludes pg_toast/pg_temp (literal underscore).

#: User schemas that own a table/sequence, deterministically ordered.
_DDL_SCHEMAS_SQL = r"""
    SELECT n.nspname AS schema
    FROM pg_namespace n
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
      AND EXISTS (
          SELECT 1 FROM pg_class c
          WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'S', 'p')
      )
    ORDER BY n.nspname
"""

#: User sequences (so ``serial``/``nextval`` defaults resolve when the script runs).
_DDL_SEQUENCES_SQL = r"""
    SELECT schemaname AS schema, sequencename AS name
    FROM pg_sequences
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
      AND schemaname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY schemaname, sequencename
"""

#: One ready-to-use column definition per row (type, identity, NOT NULL, DEFAULT).
_DDL_COLUMNS_SQL = r"""
    SELECT n.nspname AS schema,
           c.relname AS table,
           quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod)
             || CASE a.attidentity
                  WHEN 'a' THEN ' GENERATED ALWAYS AS IDENTITY'
                  WHEN 'd' THEN ' GENERATED BY DEFAULT AS IDENTITY'
                  ELSE '' END
             || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
             || CASE WHEN ad.adbin IS NOT NULL AND a.attidentity = ''
                     THEN ' DEFAULT ' || pg_get_expr(ad.adbin, ad.adrelid)
                     ELSE '' END AS coldef
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
    WHERE c.relkind IN ('r', 'p')
      AND a.attnum > 0 AND NOT a.attisdropped
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY n.nspname, c.relname, a.attnum
"""

#: Constraints as native definitions; PK→UNIQUE→CHECK→FK so FKs are added last.
_DDL_CONSTRAINTS_SQL = r"""
    SELECT n.nspname AS schema,
           c.relname AS table,
           quote_ident(con.conname) AS name,
           pg_get_constraintdef(con.oid) AS def
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE con.contype IN ('p', 'u', 'c', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY n.nspname, c.relname,
             CASE con.contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 ELSE 3 END,
             con.conname
"""

#: Secondary indexes only (those backing a PK/UNIQUE constraint are emitted above).
_DDL_INDEXES_SQL = r"""
    SELECT n.nspname AS schema,
           c.relname AS table,
           pg_get_indexdef(i.indexrelid) AS def
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT i.indisprimary
      AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid)
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY n.nspname, c.relname, ic.relname
"""

#: Distinct functions invoked by user triggers — emitted before the triggers.
_DDL_FUNCTIONS_SQL = r"""
    SELECT DISTINCT n.nspname AS schema,
           p.proname AS name,
           pg_get_functiondef(p.oid) AS def
    FROM pg_trigger t
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE NOT t.tgisinternal
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY n.nspname, p.proname
"""

#: User triggers as native definitions (FK/internal triggers excluded).
_DDL_TRIGGERS_SQL = r"""
    SELECT n.nspname AS schema,
           c.relname AS table,
           pg_get_triggerdef(t.oid) AS def
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_%' ESCAPE '\'
    ORDER BY n.nspname, c.relname, t.tgname
"""


# ---- Non-table object definitions (get_object_definition) --------------------
# One parameterized catalog query per object type. Every definition is produced by
# Postgres itself (pg_get_*def / quote_ident / format), never hand-assembled, and the
# object name is always bound as $1/$2 — no identifier interpolation (Rule 9).

_OBJECT_DEFINITION_SQL: dict[str, str] = {
    "view": """
        SELECT n.nspname AS schema, c.relname AS name,
               CASE c.relkind WHEN 'm' THEN 'materialized view' ELSE 'view' END AS kind,
               CASE c.relkind WHEN 'm' THEN 'CREATE MATERIALIZED VIEW '
                    ELSE 'CREATE VIEW ' END
                 || quote_ident(n.nspname) || '.' || quote_ident(c.relname)
                 || ' AS' || E'\\n' || pg_get_viewdef(c.oid, true) AS definition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('v', 'm') AND c.relname = $1
          AND ($2::text IS NULL OR n.nspname = $2)
        ORDER BY n.nspname
    """,
    "function": """
        SELECT n.nspname AS schema, p.proname AS name,
               CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prokind IN ('f', 'p') AND p.proname = $1
          AND ($2::text IS NULL OR n.nspname = $2)
        ORDER BY n.nspname, p.oid
    """,
    "trigger": """
        SELECT n.nspname AS schema, t.tgname AS name, 'trigger' AS kind,
               pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND t.tgname = $1
          AND ($2::text IS NULL OR n.nspname = $2)
        ORDER BY n.nspname, c.relname
    """,
    "sequence": """
        SELECT schemaname AS schema, sequencename AS name, 'sequence' AS kind,
               format(
                   'CREATE SEQUENCE %I.%I START WITH %s INCREMENT BY %s '
                   'MINVALUE %s MAXVALUE %s CACHE %s%s;',
                   schemaname, sequencename, start_value, increment_by,
                   min_value, max_value, cache_size,
                   CASE WHEN cycle THEN ' CYCLE' ELSE '' END
               ) AS definition
        FROM pg_sequences
        WHERE sequencename = $1 AND ($2::text IS NULL OR schemaname = $2)
        ORDER BY schemaname
    """,
    "index": """
        SELECT n.nspname AS schema, c.relname AS name, 'index' AS kind,
               pg_get_indexdef(c.oid) AS definition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i' AND c.relname = $1
          AND ($2::text IS NULL OR n.nspname = $2)
        ORDER BY n.nspname
    """,
}

#: Native statement cancellation; same-user backends only unless superuser.
_CANCEL_BACKEND_SQL = "SELECT pg_cancel_backend($1) AS cancelled"

#: Column-owned sequences (serial/identity) with their owning table.column.
_OWNED_SEQUENCES_SQL = """
    SELECT sn.nspname AS sequence_schema,
           s.relname  AS sequence,
           tn.nspname AS table_schema,
           t.relname  AS table,
           a.attname  AS column
    FROM pg_depend d
    JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
    JOIN pg_namespace sn ON sn.oid = s.relnamespace
    JOIN pg_class t ON t.oid = d.refobjid
    JOIN pg_namespace tn ON tn.oid = t.relnamespace
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
    WHERE d.classid = 'pg_class'::regclass
      AND d.refclassid = 'pg_class'::regclass
      AND d.deptype IN ('a', 'i')
    ORDER BY sn.nspname, s.relname
"""

#: Per-table size/row statistics from the statistics collector (no table scans). Split
#: so :func:`_build_table_stats_sql` can slot a WHERE between the two halves.
_TABLE_STATS_SELECT = """
    SELECT s.schemaname AS schema,
           s.relname AS table,
           s.n_live_tup AS approx_rows,
           pg_table_size(s.relid) AS table_bytes,
           pg_indexes_size(s.relid) AS index_bytes,
           pg_total_relation_size(s.relid) AS total_bytes
    FROM pg_stat_user_tables s"""

#: The total-size expression the rows report — also what ``min_size_bytes`` filters on.
_TABLE_STATS_SIZE = "pg_total_relation_size(s.relid)"

_TABLE_STATS_ORDER = f"\n    ORDER BY {_TABLE_STATS_SIZE} DESC, s.schemaname, s.relname\n"

#: The unfiltered query — what an argument-free ``table_stats`` still runs, verbatim.
_TABLE_STATS_SQL = _TABLE_STATS_SELECT + _TABLE_STATS_ORDER

#: Sanitized activity view — no user names, no client addresses; query text is
#: opt-in ($1) and truncated so secrets embedded in SQL can't leak by default.
_SHOW_ACTIVITY_SQL = """
    SELECT pid,
           state,
           wait_event_type,
           wait_event,
           backend_type,
           application_name,
           date_trunc('second', now() - query_start)::text AS query_age,
           date_trunc('second', now() - xact_start)::text  AS transaction_age,
           CASE WHEN $1 THEN left(query, 300) ELSE NULL END AS query
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
    ORDER BY query_start NULLS LAST
"""


#: Fuzzy column-name search (Tool A). ``$1`` is the parameterized pattern. Split so
#: :func:`_build_find_columns_sql` can append a LIMIT; unbounded it is
#: :data:`_FIND_COLUMNS_SQL`.
_FIND_COLUMNS_SELECT = """
    SELECT table_schema AS schema,
           table_name   AS table,
           column_name  AS column,
           data_type    AS type,
           (is_nullable = 'YES') AS nullable
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
      AND column_name ILIKE '%' || $1 || '%'"""

_FIND_COLUMNS_ORDER = "\n    ORDER BY table_schema, table_name, ordinal_position\n"

_FIND_COLUMNS_SQL = _FIND_COLUMNS_SELECT + _FIND_COLUMNS_ORDER

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


def _limit_clause(args: list[Any], limit: int | None) -> str:
    """Append ``limit`` to ``args`` and return the matching ``LIMIT $n`` line (or "").

    The bound value is coerced to a non-negative int; the number itself is never
    interpolated into the SQL text (Rule 9).
    """
    if limit is None:
        return ""
    args.append(max(0, int(limit)))
    return f"    LIMIT ${len(args)}\n"


def _build_list_tables_sql(
    pattern: str | None = None, limit: int | None = None
) -> tuple[str, list[Any]]:
    """Compose the table listing with an optional name filter and bound; ``(sql, args)``.

    ``pattern`` is a case-insensitive containment match on the table name — the exact
    form :data:`_FIND_COLUMNS_SQL` uses for column names, so the two name searches behave
    identically — and is bound, never interpolated (Rule 9). ``limit`` caps the rows
    returned; the caller gets a plain list, so exactly ``limit`` rows are fetched. With
    neither argument the text is exactly :data:`_LIST_TABLES_SQL`.
    """
    args: list[Any] = []
    sql = _LIST_TABLES_SELECT
    if pattern is not None:
        args.append(pattern)
        sql += f"\n      AND table_name ILIKE '%' || ${len(args)} || '%'"
    sql += _LIST_TABLES_ORDER + _limit_clause(args, limit)
    return sql, args


def _build_find_columns_sql(pattern: str, limit: int | None = None) -> tuple[str, list[Any]]:
    """Compose the column-name search with an optional bound; returns ``(sql, args)``.

    ``pattern`` is always bound as ``$1``; ``limit``, when given, is bound as ``$2``.
    Without a limit the text is exactly :data:`_FIND_COLUMNS_SQL`.
    """
    args: list[Any] = [pattern]
    return _FIND_COLUMNS_SQL + _limit_clause(args, limit), args


def _build_table_stats_sql(
    table: str | None = None,
    min_size_bytes: int | None = None,
    limit: int | None = None,
) -> tuple[str, list[Any]]:
    """Compose the table-stats query with optional filters; return ``(sql, args)``.

    Every filter *value* is bound through a placeholder (Rule 9) — nothing is
    interpolated. ``table`` matches the table name and may be schema-qualified
    (``analytics.events`` binds both parts); ``min_size_bytes`` filters on the same
    total-size expression the rows report; ``limit`` caps the answer but binds
    ``limit + 1`` so one row *beyond* the limit comes back and the caller can tell a
    truncated answer from a complete one. With no filters the text is exactly
    :data:`_TABLE_STATS_SQL`, so the default answer is unchanged.
    """
    conditions: list[str] = []
    args: list[Any] = []
    if table is not None:
        name, schema = _split_schema_table(table)
        args.append(name)
        conditions.append(f"s.relname = ${len(args)}")
        if schema is not None:
            args.append(schema)
            conditions.append(f"s.schemaname = ${len(args)}")
    if min_size_bytes is not None:
        args.append(int(min_size_bytes))
        conditions.append(f"{_TABLE_STATS_SIZE} >= ${len(args)}")

    sql = _TABLE_STATS_SELECT
    if conditions:
        sql += "\n    WHERE " + "\n      AND ".join(conditions)
    sql += _TABLE_STATS_ORDER
    if limit is not None:
        args.append(max(0, int(limit)) + 1)
        sql += f"    LIMIT ${len(args)}\n"
    return sql, args


def _assemble_ddl(
    schemas: list,
    sequences: list,
    columns: list,
    constraints: list,
    indexes: list,
    functions: list,
    triggers: list,
) -> str:
    """Stitch the seven catalog scans into one runnable, deterministic DDL script.

    Pure assembly — every value here is already a database-produced identifier or
    definition. Tables are created with columns only; *all* constraints (FKs last)
    are added afterwards via ``ALTER TABLE`` so cross-table references resolve no
    matter the table order.
    """
    out: list[str] = [
        "-- db-conn-mcp self-contained schema export (catalog DDL, not pg_dump).",
        "-- Run top-to-bottom to recreate the structure. For a byte-faithful dump,",
        "-- use the dump_schema_faithful tool (requires pg_dump).",
        "",
    ]

    def section(title: str, lines: list[str]) -> None:
        if lines:
            out.append(f"-- {title}")
            out.extend(lines)
            out.append("")

    section(
        "Schemas",
        [f"CREATE SCHEMA IF NOT EXISTS {_quote_identifier(r['schema'])};" for r in schemas],
    )
    section(
        "Sequences",
        [
            f"CREATE SEQUENCE IF NOT EXISTS "
            f"{_quote_identifier(r['schema'])}.{_quote_identifier(r['name'])};"
            for r in sequences
        ],
    )

    grouped: dict[tuple[str, str], list[str]] = {}
    for r in columns:
        grouped.setdefault((r["schema"], r["table"]), []).append(r["coldef"])
    tables = []
    for (schema, table), coldefs in grouped.items():
        qname = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        body = ",\n".join(f"    {c}" for c in coldefs)
        tables.append(f"CREATE TABLE {qname} (\n{body}\n);")
    section("Tables", tables)

    section(
        "Constraints (primary, unique, check, foreign keys)",
        [
            f"ALTER TABLE {_quote_identifier(r['schema'])}.{_quote_identifier(r['table'])} "
            f"ADD CONSTRAINT {r['name']} {r['def']};"
            for r in constraints
        ],
    )
    section("Indexes", [f"{r['def']};" for r in indexes])
    section("Trigger functions", [f"{r['def']};" for r in functions])
    section("Triggers", [f"{r['def']};" for r in triggers])

    return "\n".join(out).rstrip() + "\n"


#: Agent-facing guidance when pg_dump is missing (no DSN/host — Rule 6 safe).
_PG_DUMP_INSTALL_HINT = (
    "pg_dump was not found on the machine running this server. Either use "
    "get_database_schema(format='sql') for a self-contained export that needs no extra "
    "tools, or install the PostgreSQL client and retry: Windows "
    "`winget install PostgreSQL.PostgreSQL`; macOS `brew install libpq`; Debian/Ubuntu "
    "`sudo apt-get install -y postgresql-client`; RHEL/Fedora `sudo dnf install -y postgresql`."
)


def _pg_env_from_dsn(dsn: str) -> dict[str, str]:
    """Map a libpq URI DSN to ``PG*`` env vars so pg_dump never sees it on argv (Rule 6).

    Passing the DSN as a command-line argument would expose the password in the process
    list; instead we hand pg_dump discrete ``PGHOST``/``PGUSER``/``PGPASSWORD``/… vars.
    """
    u = urlsplit(dsn)
    env: dict[str, str] = {}
    if u.hostname:
        env["PGHOST"] = u.hostname
    if u.port:
        env["PGPORT"] = str(u.port)
    if u.username:
        env["PGUSER"] = unquote(u.username)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    name = u.path.lstrip("/")
    if name:
        env["PGDATABASE"] = unquote(name)
    query = dict(parse_qsl(u.query))
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"]
    return env


def _sanitize_pg_error(text: str, dsn: str) -> str:
    """Strip any DSN secret (host/user/password) from pg_dump's stderr before returning it."""
    u = urlsplit(dsn)
    for secret in (u.password, u.username, u.hostname):
        if secret:
            text = text.replace(unquote(secret), "***")
    return text.strip()[:500] or "pg_dump failed (no detail)."


async def _run_pg_dump(
    pg_dump: str, args: list[str], env: dict[str, str]
) -> tuple[int, bytes, bytes]:
    """Run pg_dump and return ``(returncode, stdout, stderr)``. Seam for tests to patch."""
    proc = await asyncio.create_subprocess_exec(
        pg_dump,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


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


def _timeout_seconds(timeout_ms: int | None) -> float | None:
    """Convert a per-call ``timeout_ms`` into asyncpg's ``timeout`` (seconds)."""
    if timeout_ms is None:
        return None
    ms = int(timeout_ms)
    if ms <= 0:
        raise ValueError("timeout_ms must be a positive number of milliseconds.")
    return ms / 1000.0


class _PostgresCursor:
    """A server-side cursor that owns its connection and read-only transaction.

    asyncpg cursors only live inside a transaction, so the handle keeps both and
    releases them together: ``close()`` rolls the transaction back (it never
    committed anything — it is read-only) and closes the connection.
    """

    def __init__(self, conn: Any, tx: Any, cursor: Any):
        self._conn = conn
        self._tx = tx
        self._cursor = cursor

    async def fetch(self, n: int) -> list[dict]:
        """Fetch up to ``n`` more rows; fewer (or none) means the cursor is exhausted."""
        rows = await self._cursor.fetch(max(1, int(n)))
        return [dict(r) for r in rows]

    async def close(self) -> None:
        """Release the cursor, its transaction, and the underlying connection."""
        try:
            await self._tx.rollback()
        finally:
            await self._conn.close()


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


# Matches a dollar-quote delimiter at a given position: ``$$`` or ``$tag$`` where the
# tag starts with a letter/underscore. ``$1`` (a parameter placeholder) never matches,
# because a tag cannot start with a digit.
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _reject_multiple_statements(sql: str) -> None:
    """Raise ``ValueError`` unless *sql* is a single top-level statement.

    asyncpg runs an argument-free ``execute`` over the *simple* query protocol, which
    executes several ``;``-separated commands in one call. On the write path that lets an
    embedded ``COMMIT`` escape the dry-run wrapping transaction (issue #40), permanently
    committing a change the tool reports as rolled back — bypassing the whole
    ``mode`` → ``dry_run`` → ``yolo`` → ``user_consent`` gate. We close the hole by
    rejecting stacked statements before dispatch.

    This is a small single-pass character scanner, not an AST parser (project Rule 1). It
    skips semicolons that live inside single-quoted literals (with ``''`` escapes),
    ``E'...'`` escape strings (backslash escapes), double-quoted identifiers, dollar-quoted
    bodies (``$tag$ ... $tag$``, empty tag ``$$`` included), line comments (``-- ...``) and
    nested block comments (``/* ... */``). A single trailing semicolon, optionally followed
    by whitespace or comments, is one statement and is allowed; two or more non-empty
    statements are rejected.

    The message names no SQL content (Rule 6): it states only the constraint.
    """
    statements = 0
    has_content = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):  # line comment
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if ch == "/" and sql.startswith("/*", i):  # nested block comment
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if ch == "$":  # dollar-quoted string ($$...$$ or $tag$...$tag$)
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                i = n if end == -1 else end + len(tag)
                has_content = True
                continue
        if ch == '"':  # double-quoted identifier ("" escapes an embedded quote)
            i += 1
            while i < n:
                if sql[i] == '"':
                    if sql.startswith('""', i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            has_content = True
            continue
        if ch == "'":  # single-quoted literal; E'...' allows backslash escapes
            escaped = (
                i > 0
                and sql[i - 1] in "Ee"
                and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            )
            i += 1
            while i < n:
                cur = sql[i]
                if escaped and cur == "\\":
                    i += 2
                    continue
                if cur == "'":
                    if sql.startswith("''", i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            has_content = True
            continue
        if ch == ";":  # top-level statement separator
            if has_content:
                statements += 1
            has_content = False
            i += 1
            continue
        if not ch.isspace():
            has_content = True
        i += 1
    if has_content:
        statements += 1
    if statements > 1:
        raise ValueError("Multiple SQL statements are not allowed; submit a single statement.")


class PostgresDialect(Dialect):
    """``asyncpg``-backed PostgreSQL dialect."""

    scheme = "postgresql"

    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        conn = await asyncpg.connect(dsn)
        if read_only:
            # Enforce read-only natively before the caller can run anything.
            await conn.execute(_READ_ONLY_SQL)
        return conn

    async def list_tables(
        self, conn: Any, pattern: str | None = None, limit: int | None = None
    ) -> list[dict]:
        sql, args = _build_list_tables_sql(pattern, limit)
        rows = await conn.fetch(sql, *args)
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

    async def table_indexes(self, conn: Any, table: str) -> list[dict]:
        name, schema = _split_schema_table(table)
        rows = await conn.fetch(_INDEXES_SQL, name, schema)
        return [dict(r) for r in rows]

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

    async def get_database_ddl(self, conn: Any) -> str:
        """Build a self-contained, runnable DDL script from native catalog functions.

        Seven ordered scans (schemas, sequences, columns, constraints, indexes, trigger
        functions, triggers) are stitched by :func:`_assemble_ddl`. The heavy lifting —
        exact types, defaults, constraint/index/trigger text — is done by Postgres'
        ``format_type``/``pg_get_*def`` functions, so the output is faithful for the
        common cases and there is no identifier interpolation to get wrong.
        """
        return _assemble_ddl(
            await conn.fetch(_DDL_SCHEMAS_SQL),
            await conn.fetch(_DDL_SEQUENCES_SQL),
            await conn.fetch(_DDL_COLUMNS_SQL),
            await conn.fetch(_DDL_CONSTRAINTS_SQL),
            await conn.fetch(_DDL_INDEXES_SQL),
            await conn.fetch(_DDL_FUNCTIONS_SQL),
            await conn.fetch(_DDL_TRIGGERS_SQL),
        )

    async def dump_schema_sql(self, dsn: str) -> dict:
        """Faithful ``pg_dump --schema-only`` export; DSN secrets never touch argv or output.

        Returns ``{"status": "ok", "ddl": ...}``; or ``pg_dump_not_found`` with install
        guidance if the binary is absent; or ``pg_dump_failed`` with a sanitized message.
        The connection is passed to pg_dump via ``PG*`` env vars (see
        :func:`_pg_env_from_dsn`), and any error text is scrubbed (:func:`_sanitize_pg_error`).
        """
        pg_dump = shutil.which("pg_dump")
        if pg_dump is None:
            return {"status": "pg_dump_not_found", "message": _PG_DUMP_INSTALL_HINT}
        env = {**os.environ, **_pg_env_from_dsn(dsn)}
        args = ["--schema-only", "--no-owner", "--no-privileges"]
        try:
            code, out, err = await _run_pg_dump(pg_dump, args, env)
        except OSError as exc:  # binary vanished / not executable between which() and exec
            return {"status": "pg_dump_failed", "message": _sanitize_pg_error(str(exc), dsn)}
        if code != 0:
            return {
                "status": "pg_dump_failed",
                "message": _sanitize_pg_error(err.decode("utf-8", "replace"), dsn),
            }
        return {"status": "ok", "ddl": out.decode("utf-8", "replace")}

    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        limit = max(0, int(n))  # coerce to a safe non-negative int (never interpolate raw)
        sql = f"SELECT * FROM {_quote_identifier(table)} LIMIT {limit}"
        rows = await conn.fetch(sql)
        return [dict(r) for r in rows]

    async def execute(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        # Defense-in-depth (issue #40): reject stacked statements up front, before either
        # dispatch branch, so an embedded ``COMMIT`` can never escape the dry-run
        # transaction regardless of the row-returning heuristic.
        _reject_multiple_statements(sql)
        args = list(params or [])
        timeout = _timeout_seconds(timeout_ms)
        try:
            if self._is_row_returning(sql):
                rows = await conn.fetch(sql, *args, timeout=timeout)
                mapped = [dict(r) for r in rows]
                columns = list(mapped[0].keys()) if mapped else []
                return {"columns": columns, "rows": mapped}
            status = await conn.execute(sql, *args, timeout=timeout)
            return {"rows_affected": _parse_affected(status)}
        except TimeoutError:
            # Sanitized: the query was cancelled server-side by the driver.
            raise ValueError(f"Query exceeded timeout_ms={timeout_ms} and was cancelled.") from None

    async def execute_dry_run(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        tx = conn.transaction()
        await tx.start()
        try:
            result = await self.execute(conn, sql, params, timeout_ms)
        finally:
            await tx.rollback()  # always discard — that is the whole point
        return {**result, "dry_run": True, "rolled_back": True}

    async def open_cursor(self, conn: Any, sql: str, params: list[Any] | None = None) -> Any:
        tx = conn.transaction()
        await tx.start()
        try:
            cursor = await conn.cursor(sql, *(params or []))
        except Exception:
            await tx.rollback()
            raise
        return _PostgresCursor(conn, tx, cursor)

    async def get_object_definition(self, conn: Any, object_type: str, name: str) -> list[dict]:
        query = _OBJECT_DEFINITION_SQL.get(object_type)
        if query is None:
            supported = ", ".join(sorted(_OBJECT_DEFINITION_SQL))
            raise ValueError(f"Unknown object_type {object_type!r}. Supported: {supported}.")
        obj, schema = _split_schema_table(name)
        rows = await conn.fetch(query, obj, schema)
        return [dict(r) for r in rows]

    async def cancel_backend(self, conn: Any, pid: int) -> dict:
        row = await conn.fetchrow(_CANCEL_BACKEND_SQL, int(pid))
        return {"pid": int(pid), "cancelled": bool(row["cancelled"]) if row else False}

    async def explain(
        self,
        conn: Any,
        sql: str,
        analyze: bool = False,
        params: list[Any] | None = None,
    ) -> dict:
        prefix = "EXPLAIN (ANALYZE, BUFFERS) " if analyze else "EXPLAIN "
        # Same bind path as execute(): asyncpg's $1/$2 positional arguments, so the
        # planner sees the real values rather than a query mangled by interpolation.
        rows = await conn.fetch(prefix + sql, *(params or []))
        # Each row is one plan line under a single "QUERY PLAN" column.
        return {"plan": [next(iter(dict(r).values())) for r in rows]}

    async def check_sequences(self, conn: Any, behind_only: bool = True) -> dict:
        # "behind" needs the sequence's own last_value/is_called next to the column's
        # max, which is one probe pair per sequence — the catalog query can't decide it,
        # so the filtering happens here, over the rows we inspected.
        report: list[dict] = []
        scanned = 0
        for r in await conn.fetch(_OWNED_SEQUENCES_SQL):
            d = dict(r)
            qseq = f"{_quote_identifier(d['sequence_schema'])}.{_quote_identifier(d['sequence'])}"
            qtable = f"{_quote_identifier(d['table_schema'])}.{_quote_identifier(d['table'])}"
            qcol = _quote_identifier(d["column"])
            try:
                seq = await conn.fetchrow(f"SELECT last_value, is_called FROM {qseq}")
                mx = await conn.fetchrow(f"SELECT max({qcol})::bigint AS max_id FROM {qtable}")
            except asyncpg.PostgresError:  # non-numeric column / no privilege — skip
                continue
            last_value = seq["last_value"]
            is_called = seq["is_called"]
            max_id = mx["max_id"]
            # An uncalled sequence hands out last_value itself next, so its safe
            # ceiling is one lower than a called one.
            ceiling = last_value if is_called else last_value - 1
            behind = max_id is not None and max_id > ceiling
            scanned += 1
            if behind_only and not behind:
                continue
            report.append(
                {
                    **d,
                    "last_value": last_value,
                    "is_called": is_called,
                    "max_id": max_id,
                    "behind": behind,
                }
            )
        return {"total_sequences": scanned, "sequences": report}

    async def table_stats(
        self,
        conn: Any,
        limit: int | None = None,
        min_size_bytes: int | None = None,
        table: str | None = None,
    ) -> list[dict]:
        sql, args = _build_table_stats_sql(table, min_size_bytes, limit)
        return [dict(r) for r in await conn.fetch(sql, *args)]

    async def show_activity(self, conn: Any, include_query: bool = False) -> list[dict]:
        rows = await conn.fetch(_SHOW_ACTIVITY_SQL, bool(include_query))
        mapped = [dict(r) for r in rows]
        if not include_query:
            for row in mapped:
                row.pop("query", None)
        return mapped

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

    async def find_columns(self, conn: Any, pattern: str, limit: int | None = None) -> list[dict]:
        sql, args = _build_find_columns_sql(pattern, limit)
        rows = await conn.fetch(sql, *args)
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

    async def probe_listener(self, host: str, port: int) -> bool:
        """TCP + SSLRequest probe; sends no credentials, reads one status byte."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=PROBE_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError):
            return False
        try:
            writer.write(_SSL_REQUEST)
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(1), timeout=PROBE_TIMEOUT_SECONDS)
            return reply in (b"S", b"N")
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            return False
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

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
