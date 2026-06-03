"""Tests for the MCP tool handlers (the service layer beneath FastMCP).

The handlers are exercised directly with a temp config and a fake dialect, so no
live database or MCP transport is needed. The contracts under test: DSNs never leak,
reads connect read-only, the write gate is enforced, and connection failures surface
as sanitized diagnostics.
"""

import json

import asyncpg
import pytest

from db_conn_mcp import handlers as handlers_mod
from db_conn_mcp import server
from db_conn_mcp.handlers import Handlers
from db_conn_mcp.safety import WriteRejected

CONFIG = {
    "connections": [
        {"name": "prod", "dsn": "postgresql://u:SECRET@h/prod", "mode": "read"},
        {"name": "dev", "dsn": "postgresql://u:SECRET@h/dev", "mode": "write"},
        {"name": "trusted", "dsn": "postgresql://u:SECRET@h/t", "mode": "write", "yolo": True},
    ]
}


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


class FakeConn:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeDialect:
    """Records how it was connected and serves canned results."""

    def __init__(self, *, raise_on_connect=None, exec_result=None):
        self.raise_on_connect = raise_on_connect
        self.exec_result = exec_result or {"columns": [], "rows": []}
        self.connected_read_only = None
        self.conn = FakeConn()

    async def connect(self, dsn, *, read_only):
        self.connected_read_only = read_only
        if self.raise_on_connect:
            raise self.raise_on_connect
        return self.conn

    async def list_tables(self, conn):
        return [{"schema": "public", "name": "users", "kind": "table"}]

    async def get_schema(self, conn, table):
        return {"table": table, "columns": [], "primary_key": [], "foreign_keys": []}

    async def sample_rows(self, conn, table, n=10):
        return [{"id": 1}]

    async def execute(self, conn, sql):
        return self.exec_result

    def validate_read_only(self, sql):
        # Permissive stub; the real read-only gate is covered in test_postgres.py.
        return None


def _patch_dialect(monkeypatch, dialect):
    # Handlers resolves the dialect via its own imported name — patch there.
    monkeypatch.setattr(handlers_mod, "dialect_for", lambda dsn: dialect)


# ---- list_databases never leaks the DSN --------------------------------------


async def test_list_databases_hides_dsn(cfg_path):
    h = Handlers(cfg_path)
    result = await h.list_databases()
    blob = json.dumps(result)
    assert "SECRET" not in blob
    assert all("dsn" not in row for row in result)
    assert {r["name"] for r in result} == {"prod", "dev", "trusted"}


# ---- reads connect read-only -------------------------------------------------


async def test_list_tables_connects_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.list_tables("prod")
    assert dialect.connected_read_only is True
    assert result[0]["name"] == "users"
    assert dialect.conn.closed is True


async def test_execute_read_query_is_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect(exec_result={"columns": ["id"], "rows": [{"id": 1}]})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.execute_read_query("prod", "SELECT id FROM users")
    assert dialect.connected_read_only is True
    assert result["rows"] == [{"id": 1}]


# ---- the write gate ----------------------------------------------------------


async def test_write_to_read_db_is_rejected(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    with pytest.raises(WriteRejected, match="read-only"):
        await h.execute_write_query("prod", "DELETE FROM users", user_consent=True)
    # never even attempted to connect
    assert dialect.connected_read_only is None


async def test_write_without_consent_rejected(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    with pytest.raises(WriteRejected, match="user_consent"):
        await h.execute_write_query("dev", "DELETE FROM users", user_consent=False)


async def test_write_with_consent_runs_writable(cfg_path, monkeypatch):
    dialect = FakeDialect(exec_result={"rows_affected": 2})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.execute_write_query("dev", "DELETE FROM users", user_consent=True)
    assert dialect.connected_read_only is False
    assert result == {"rows_affected": 2}


async def test_write_yolo_runs_without_consent(cfg_path, monkeypatch):
    dialect = FakeDialect(exec_result={"rows_affected": 1})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.execute_write_query("trusted", "DELETE FROM x", user_consent=False)
    assert dialect.connected_read_only is False
    assert result == {"rows_affected": 1}


# ---- set_yolo_mode persists --------------------------------------------------


async def test_set_yolo_mode_persists(cfg_path):
    h = Handlers(cfg_path)
    await h.set_yolo_mode("dev", True)
    reloaded = json.loads(cfg_path.read_text(encoding="utf-8"))
    dev = next(c for c in reloaded["connections"] if c["name"] == "dev")
    assert dev["yolo"] is True


# ---- check_database: sanitized, never raises for unreachable -----------------


async def test_check_database_ok(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    result = await h.check_database("prod")
    assert result[0]["status"] == "OK"


async def test_check_database_unreachable_is_sanitized(cfg_path, monkeypatch):
    err = asyncpg.InvalidPasswordError("auth failed for u at h:5432 with SECRET")
    _patch_dialect(monkeypatch, FakeDialect(raise_on_connect=err))
    h = Handlers(cfg_path)
    result = await h.check_database("prod")
    entry = result[0]
    assert entry["status"] != "OK"
    assert entry["category"] == "AUTH_FAILED"
    assert "SECRET" not in json.dumps(entry)


async def test_check_database_all(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    result = await h.check_database()
    assert {r["database"] for r in result} == {"prod", "dev", "trusted"}


# ---- connect failure on a normal tool surfaces a sanitized diagnostic --------


async def test_tool_connect_failure_is_sanitized(cfg_path, monkeypatch):
    err = asyncpg.InvalidPasswordError("auth failed for u at h with SECRET")
    _patch_dialect(monkeypatch, FakeDialect(raise_on_connect=err))
    h = Handlers(cfg_path)
    with pytest.raises(Exception) as exc:  # noqa: B017 — asserting the message, not type
        await h.list_tables("prod")
    assert "SECRET" not in str(exc.value)


def test_build_server_smoke(cfg_path):
    # The FastMCP app builds and exposes the 8 tools + 1 prompt without error.
    app = server.build_server(cfg_path)
    assert app is not None
