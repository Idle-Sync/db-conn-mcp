"""PostgresDialect unit tests against a fake asyncpg connection.

These cover the behavior we can verify without a live server: native read-only
enforcement is issued, identifiers are safely quoted, introspection rows are mapped
to the documented shapes, and execute() picks the right result shape. A real
read-only-blocks-writes check belongs in a Docker integration test (see module note
in postgres.py).
"""

import asyncpg
import pytest

from db_conn_mcp.dialects.postgres import (
    PostgresDialect,
    _leading_keyword,
    _quote_identifier,
)


class FakeConn:
    """Records executed SQL and serves queued fetch() results in order."""

    def __init__(self, fetch_results=None):
        self.executed: list[str] = []
        self._fetch_queue = list(fetch_results or [])
        self.fetched: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 3"

    async def fetch(self, sql, *args):
        self.fetched.append(sql)
        return self._fetch_queue.pop(0) if self._fetch_queue else []

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
