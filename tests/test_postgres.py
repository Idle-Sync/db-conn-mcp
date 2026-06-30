"""PostgresDialect unit tests against a fake asyncpg connection.

These cover the behavior we can verify without a live server: native read-only
enforcement is issued, identifiers are safely quoted, introspection rows are mapped
to the documented shapes, and execute() picks the right result shape. A real
read-only-blocks-writes check belongs in a Docker integration test (see module note
in postgres.py).
"""

import asyncpg
import pytest

from db_conn_mcp.dialects import postgres as pg
from db_conn_mcp.dialects.postgres import (
    PostgresDialect,
    _assemble_ddl,
    _build_table_search_sql,
    _is_junk_table,
    _leading_keyword,
    _pg_env_from_dsn,
    _quote_identifier,
    _sanitize_pg_error,
)


class FakeConn:
    """Records executed/fetched SQL (and args) and serves queued results in order."""

    def __init__(self, fetch_results=None, fetchrow_results=None):
        self.executed: list[str] = []
        self._fetch_queue = list(fetch_results or [])
        self._fetchrow_queue = list(fetchrow_results or [])
        self.fetched: list[str] = []
        self.fetch_args: list[tuple] = []
        self.fetchrow_calls: list[str] = []
        self.fetchrow_args: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 3"

    async def fetch(self, sql, *args):
        self.fetched.append(sql)
        self.fetch_args.append(args)
        return self._fetch_queue.pop(0) if self._fetch_queue else []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append(sql)
        self.fetchrow_args.append(args)
        return self._fetchrow_queue.pop(0) if self._fetchrow_queue else None

    async def close(self):
        pass


# ---- identifier quoting (pure, the Rule 9 guard) -----------------------------


def test_quote_simple_identifier():
    assert _quote_identifier("users") == '"users"'


def test_quote_doubles_embedded_quotes():
    assert _quote_identifier('we"ird') == '"we""ird"'


def test_quote_schema_qualified():
    assert _quote_identifier("public.orders") == '"public"."orders"'


def test_quote_rejects_empty():
    with pytest.raises(ValueError):
        _quote_identifier("")


def test_quote_rejects_null_byte():
    with pytest.raises(ValueError):
        _quote_identifier("a\x00b")


def test_quote_neutralizes_injection():
    # A hostile name becomes one inert (quoted) identifier, not runnable SQL.
    hostile = 'users"; DROP TABLE secrets; --'
    quoted = _quote_identifier(hostile)
    assert quoted.startswith('"') and quoted.endswith('"')
    assert quoted.count('"') == 2 + hostile.count('"') * 2


# ---- connect: native read-only enforcement -----------------------------------


async def test_connect_enforces_read_only(monkeypatch):
    fake = FakeConn()

    async def fake_connect(dsn):
        return fake

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    conn = await PostgresDialect().connect("postgresql://h/db", read_only=True)
    assert conn is fake
    assert any("READ ONLY" in sql.upper() for sql in fake.executed)


async def test_connect_writable_skips_read_only(monkeypatch):
    fake = FakeConn()

    async def fake_connect(dsn):
        return fake

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    await PostgresDialect().connect("postgresql://h/db", read_only=False)
    assert not any("READ ONLY" in sql.upper() for sql in fake.executed)


# ---- introspection shapes ----------------------------------------------------


async def test_list_tables_shape():
    rows = [
        {"schema": "public", "name": "users", "kind": "table"},
        {"schema": "public", "name": "active_users", "kind": "view"},
    ]
    conn = FakeConn([rows])
    result = await PostgresDialect().list_tables(conn)
    assert result == rows


async def test_get_schema_shape():
    columns = [{"name": "id", "type": "integer", "nullable": False, "default": None}]
    keys = [
        {"column": "id", "constraint_type": "PRIMARY KEY", "references": None},
        {"column": "org_id", "constraint_type": "FOREIGN KEY", "references": "orgs.id"},
    ]
    conn = FakeConn([columns, keys])
    result = await PostgresDialect().get_schema(conn, "users")
    assert result["table"] == "users"
    assert result["columns"] == columns
    assert result["primary_key"] == ["id"]
    assert result["foreign_keys"] == [{"column": "org_id", "references": "orgs.id"}]


async def test_get_database_schema_groups_columns_and_keys():
    columns = [
        {
            "schema": "public",
            "table": "orgs",
            "name": "id",
            "type": "integer",
            "nullable": False,
            "default": None,
        },
        {
            "schema": "public",
            "table": "users",
            "name": "id",
            "type": "integer",
            "nullable": False,
            "default": None,
        },
        {
            "schema": "public",
            "table": "users",
            "name": "org_id",
            "type": "integer",
            "nullable": True,
            "default": None,
        },
    ]
    keys = [
        {
            "schema": "public",
            "table": "orgs",
            "column": "id",
            "constraint_type": "PRIMARY KEY",
            "references": None,
        },
        {
            "schema": "public",
            "table": "users",
            "column": "id",
            "constraint_type": "PRIMARY KEY",
            "references": None,
        },
        {
            "schema": "public",
            "table": "users",
            "column": "org_id",
            "constraint_type": "FOREIGN KEY",
            "references": "orgs.id",
        },
    ]
    conn = FakeConn([columns, keys])
    result = await PostgresDialect().get_database_schema(conn)
    assert [t["name"] for t in result["tables"]] == ["orgs", "users"]  # insertion order kept
    users = next(t for t in result["tables"] if t["name"] == "users")
    assert users["columns"] == [
        {"name": "id", "type": "integer", "nullable": False, "default": None},
        {"name": "org_id", "type": "integer", "nullable": True, "default": None},
    ]
    assert users["primary_key"] == ["id"]
    assert users["foreign_keys"] == [{"column": "org_id", "references": "orgs.id"}]
    # base-table filter is applied in SQL, not Python
    assert "BASE TABLE" in conn.fetched[0]


async def test_get_database_schema_orders_deterministically():
    # Columns query is ORDER BY-ed; a key for an unknown table is ignored, not crashed.
    columns = [
        {
            "schema": "public",
            "table": "a",
            "name": "id",
            "type": "integer",
            "nullable": False,
            "default": None,
        },
    ]
    keys = [
        {
            "schema": "public",
            "table": "ghost",
            "column": "x",
            "constraint_type": "PRIMARY KEY",
            "references": None,
        },
    ]
    conn = FakeConn([columns, keys])
    result = await PostgresDialect().get_database_schema(conn)
    assert [t["name"] for t in result["tables"]] == ["a"]
    assert result["tables"][0]["primary_key"] == []


async def test_sample_rows_quotes_identifier_and_limits():
    conn = FakeConn([[{"id": 1}]])
    await PostgresDialect().sample_rows(conn, "users", n=5)
    sql = conn.fetched[0]
    assert '"users"' in sql
    assert "5" in sql


# ---- execute result shaping --------------------------------------------------


async def test_execute_select_returns_columns_and_rows():
    conn = FakeConn([[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]])
    result = await PostgresDialect().execute(conn, "SELECT id, name FROM users")
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


async def test_execute_write_returns_rows_affected():
    conn = FakeConn()
    result = await PostgresDialect().execute(conn, "UPDATE users SET x = 1")
    assert result == {"rows_affected": 3}


# ---- leading-keyword extraction (the gate's basis) ---------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "select"),
        ("  \n  select 1", "select"),
        ("WITH t AS (SELECT 1) SELECT * FROM t", "with"),
        ("-- a comment\nSELECT 1", "select"),
        ("/* block */ SELECT 1", "select"),
        ("/* a */ -- b\n  values (1)", "values"),
        ("", ""),
        ("   ", ""),
        ("-- only a comment", ""),
    ],
)
def test_leading_keyword(sql, expected):
    assert _leading_keyword(sql) == expected


# ---- validate_read_only: the read-tool gate ---------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "  select 1",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "VALUES (1), (2)",
        "TABLE users",
        "SHOW search_path",
        "EXPLAIN SELECT 1",
        "-- comment\nSELECT 1",
    ],
)
def test_validate_read_only_accepts_read_statements(sql):
    assert PostgresDialect().validate_read_only(sql) is None


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "-- just a comment",
        "INSERT INTO users VALUES (1)",
        "UPDATE users SET x = 1",
        "DELETE FROM users",
        "DROP TABLE users",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
        # The exact bypass from issue #1: a SET leader (flip the session) is rejected
        # outright by the allowlist — the trailing write never gets a chance to run.
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE; DELETE FROM users",
        "set session characteristics as transaction read write; drop table t",
    ],
)
def test_validate_read_only_rejects_non_read_statements(sql):
    with pytest.raises(ValueError):
        PostgresDialect().validate_read_only(sql)


# ---- find_columns (fuzzy column-name search) --------------------------------


async def test_find_columns_ilike_and_maps():
    rows = [
        {"schema": "public", "table": "users", "column": "email", "type": "text", "nullable": True}
    ]
    conn = FakeConn(fetch_results=[rows])
    result = await PostgresDialect().find_columns(conn, "mail")
    assert result == rows
    assert "ILIKE" in conn.fetched[0].upper()  # fuzzy match
    assert conn.fetch_args[0] == ("mail",)  # pattern is parameterized, not concatenated


# ---- junk-table filter (pure) ------------------------------------------------


def test_is_junk_table():
    assert _is_junk_table("migrations")
    assert _is_junk_table("failed_jobs")
    assert _is_junk_table("awsdms_apply_exceptions")
    assert _is_junk_table("pg_stat_statements")
    assert not _is_junk_table("users")
    assert not _is_junk_table("company_employee")


# ---- per-table search SQL builder (pure) ------------------------------------


def test_build_table_search_sql_quotes_casts_ilikes():
    sql = _build_table_search_sql("public", "users", ["email", 'we"ird'], 5)
    assert '"public"."users"' in sql  # schema.table quoted
    assert '"email"::text ILIKE' in sql  # cast to text + fuzzy
    assert '"we""ird"::text ILIKE' in sql  # hostile column name safely quoted
    assert "$1" in sql  # value parameterized
    assert "[1:5]" in sql  # sample limit applied


# ---- search_value (fuzzy cross-table value search) --------------------------


async def test_search_value_explicit_tables_maps_hits():
    cols = [
        {"schema": "public", "table": "users", "column": "email"},
        {"schema": "public", "table": "users", "column": "name"},
    ]
    agg = {"m_0": 2, "s_0": ["a@x.com", "b@x.com"], "m_1": 0, "s_1": None}
    conn = FakeConn(fetch_results=[cols], fetchrow_results=[agg])
    out = await PostgresDialect().search_value(conn, "x.com", tables=["users"])
    assert out["truncated"] is False
    assert out["results"] == [
        {
            "schema": "public",
            "table": "users",
            "column": "email",
            "matches": 2,
            "samples": ["a@x.com", "b@x.com"],
        }
    ]
    assert conn.fetchrow_args[0] == ("x.com",)  # value parameterized


async def test_search_value_skips_junk_when_unscoped():
    cols = [
        {"schema": "public", "table": "migrations", "column": "name"},
        {"schema": "public", "table": "users", "column": "email"},
    ]
    conn = FakeConn(fetch_results=[cols], fetchrow_results=[{"m_0": 1, "s_0": ["a@x"]}])
    out = await PostgresDialect().search_value(conn, "x", tables=None)
    assert [r["table"] for r in out["results"]] == ["users"]  # migrations skipped
    assert len(conn.fetchrow_calls) == 1  # only the non-junk table was scanned


# ---- self-contained DDL assembly (pure) -------------------------------------


def test_assemble_ddl_orders_and_wraps_native_fragments():
    schemas = [{"schema": "app"}]
    sequences = [{"schema": "app", "name": "users_id_seq"}]
    columns = [
        {"schema": "app", "table": "users", "coldef": '"id" integer NOT NULL'},
        {"schema": "app", "table": "users", "coldef": '"email" text'},
    ]
    constraints = [
        {"schema": "app", "table": "users", "name": '"users_pkey"', "def": "PRIMARY KEY (id)"},
        {
            "schema": "app",
            "table": "orders",
            "name": '"orders_user_fk"',
            "def": "FOREIGN KEY (user_id) REFERENCES app.users(id)",
        },
    ]
    indexes = [{"schema": "app", "table": "users", "def": "CREATE INDEX ix ON app.users (email)"}]
    functions = [{"schema": "app", "name": "touch", "def": "CREATE FUNCTION app.touch()..."}]
    triggers = [{"schema": "app", "table": "users", "def": "CREATE TRIGGER t ... app.touch()"}]

    ddl = _assemble_ddl(schemas, sequences, columns, constraints, indexes, functions, triggers)

    assert 'CREATE SCHEMA IF NOT EXISTS "app";' in ddl
    assert 'CREATE SEQUENCE IF NOT EXISTS "app"."users_id_seq";' in ddl
    assert 'CREATE TABLE "app"."users" (\n    "id" integer NOT NULL,\n    "email" text\n);' in ddl
    assert 'ALTER TABLE "app"."users" ADD CONSTRAINT "users_pkey" PRIMARY KEY (id);' in ddl
    assert "CREATE INDEX ix ON app.users (email);" in ddl
    # ordering invariants that make the script runnable top-to-bottom:
    assert ddl.index("CREATE TABLE") < ddl.index("FOREIGN KEY")  # tables before FKs
    assert ddl.index("CREATE FUNCTION") < ddl.index("CREATE TRIGGER")  # functions before triggers


def test_assemble_ddl_omits_empty_sections():
    ddl = _assemble_ddl([], [], [], [], [], [], [])
    assert "CREATE TABLE" not in ddl
    assert "-- Triggers" not in ddl
    assert ddl.endswith("\n")


async def test_get_database_ddl_runs_seven_scans_in_order():
    conn = FakeConn([[{"schema": "app"}], [], [], [], [], [], []])
    ddl = await PostgresDialect().get_database_ddl(conn)
    # schemas, sequences, columns, constraints, indexes, functions, triggers
    assert len(conn.fetched) == 7
    assert 'CREATE SCHEMA IF NOT EXISTS "app";' in ddl


# ---- pg_dump faithful export -------------------------------------------------


def test_pg_env_from_dsn_maps_parts_and_keeps_dsn_off_argv():
    env = _pg_env_from_dsn("postgresql://me:p%40ss@db.example.com:6543/shop?sslmode=require")
    assert env["PGHOST"] == "db.example.com"
    assert env["PGPORT"] == "6543"
    assert env["PGUSER"] == "me"
    assert env["PGPASSWORD"] == "p@ss"  # URL-decoded
    assert env["PGDATABASE"] == "shop"
    assert env["PGSSLMODE"] == "require"


def test_sanitize_pg_error_strips_secrets():
    dsn = "postgresql://me:topsecret@db.example.com:5432/shop"
    msg = _sanitize_pg_error("could not connect to db.example.com as me: topsecret rejected", dsn)
    assert "topsecret" not in msg
    assert "db.example.com" not in msg
    assert "me" not in msg or "***" in msg


async def test_dump_schema_sql_reports_not_found(monkeypatch):
    monkeypatch.setattr(pg.shutil, "which", lambda name: None)
    result = await PostgresDialect().dump_schema_sql("postgresql://u:p@h/db")
    assert result["status"] == "pg_dump_not_found"
    assert "install" in result["message"].lower()


async def test_dump_schema_sql_returns_ddl_on_success(monkeypatch):
    monkeypatch.setattr(pg.shutil, "which", lambda name: "/usr/bin/pg_dump")

    async def fake_run(pg_dump, args, env):
        assert "--schema-only" in args
        assert env["PGPASSWORD"] == "p"  # connection passed via env, never argv
        return 0, b"CREATE TABLE t ();", b""

    monkeypatch.setattr(pg, "_run_pg_dump", fake_run)
    result = await PostgresDialect().dump_schema_sql("postgresql://u:p@h/db")
    assert result == {"status": "ok", "ddl": "CREATE TABLE t ();"}


async def test_dump_schema_sql_sanitizes_failure(monkeypatch):
    monkeypatch.setattr(pg.shutil, "which", lambda name: "/usr/bin/pg_dump")

    async def fake_run(pg_dump, args, env):
        return 1, b"", b"pg_dump: error: connection to host failed: secretpw"

    monkeypatch.setattr(pg, "_run_pg_dump", fake_run)
    result = await PostgresDialect().dump_schema_sql("postgresql://u:secretpw@host/db")
    assert result["status"] == "pg_dump_failed"
    assert "secretpw" not in result["message"]
