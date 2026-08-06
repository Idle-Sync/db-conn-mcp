"""Tests for the MCP tool handlers (the service layer beneath FastMCP).

The handlers are exercised directly with a temp config and a fake dialect, so no
live database or MCP transport is needed. The contracts under test: DSNs never leak,
reads connect read-only, the write gate is enforced, and connection failures surface
as sanitized diagnostics.
"""

import asyncio
import json
import time
from pathlib import Path

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


class FakeCursor:
    """Serves queued row batches; records whether it was closed."""

    def __init__(self, batches=None):
        self._batches = list(batches or [])
        self.closed = False

    async def fetch(self, n):
        return self._batches.pop(0) if self._batches else []

    async def close(self):
        self.closed = True


class FakeDialect:
    """Records how it was connected and serves canned results."""

    def __init__(
        self,
        *,
        raise_on_connect=None,
        exec_result=None,
        dump_result=None,
        cursor_batches=None,
        db_schema=None,
    ):
        self.raise_on_connect = raise_on_connect
        self.exec_result = exec_result or {"columns": [], "rows": []}
        self.dump_result = dump_result or {"status": "ok", "ddl": "-- pg_dump\nCREATE TABLE x();"}
        self.dumped_dsn = None
        self.connected_read_only = None
        self.conn = FakeConn()
        self.exec_calls: list[dict] = []
        self.dry_run_calls: list[dict] = []
        self.cursor = FakeCursor(cursor_batches)
        self.db_schema = db_schema
        self.explained: list[tuple] = []
        self.cancelled_pids: list[int] = []
        self.activity_include_query = None

    async def connect(self, dsn, *, read_only):
        self.connected_read_only = read_only
        if self.raise_on_connect:
            raise self.raise_on_connect
        return self.conn

    async def list_tables(self, conn):
        return [{"schema": "public", "name": "users", "kind": "table"}]

    async def get_schema(self, conn, table):
        return {"table": table, "columns": [], "primary_key": [], "foreign_keys": []}

    async def get_database_schema(self, conn):
        if self.db_schema is not None:
            return self.db_schema
        return {
            "tables": [
                {
                    "schema": "public",
                    "name": "users",
                    "columns": [],
                    "primary_key": [],
                    "foreign_keys": [],
                }
            ]
        }

    async def get_database_ddl(self, conn):
        return "-- self-contained\nCREATE TABLE public.users (\n    id integer NOT NULL\n);"

    async def dump_schema_sql(self, dsn):
        self.dumped_dsn = dsn
        return self.dump_result

    async def sample_rows(self, conn, table, n=10):
        return [{"id": 1}]

    async def execute(self, conn, sql, params=None, timeout_ms=None):
        self.exec_calls.append({"sql": sql, "params": params, "timeout_ms": timeout_ms})
        return self.exec_result

    async def execute_dry_run(self, conn, sql, params=None, timeout_ms=None):
        self.dry_run_calls.append({"sql": sql, "params": params, "timeout_ms": timeout_ms})
        return {**self.exec_result, "dry_run": True, "rolled_back": True}

    async def open_cursor(self, conn, sql, params=None):
        return self.cursor

    async def get_object_definition(self, conn, object_type, name):
        return [{"schema": "public", "name": name, "kind": object_type, "definition": "..."}]

    async def cancel_backend(self, conn, pid):
        self.cancelled_pids.append(pid)
        return {"pid": pid, "cancelled": True}

    async def explain(self, conn, sql, analyze=False):
        self.explained.append((sql, analyze))
        return {"plan": ["Seq Scan on users"]}

    async def check_sequences(self, conn):
        return [
            {"sequence": "users_id_seq", "table": "users", "column": "id", "behind": True},
            {"sequence": "orgs_id_seq", "table": "orgs", "column": "id", "behind": False},
        ]

    async def table_stats(self, conn):
        return [{"schema": "public", "table": "users", "approx_rows": 5, "total_bytes": 8192}]

    async def show_activity(self, conn, include_query=False):
        self.activity_include_query = include_query
        return [{"pid": 1, "state": "active"}]

    def validate_read_only(self, sql):
        # Permissive stub; the real read-only gate is covered in test_postgres.py.
        return None

    async def find_columns(self, conn, pattern):
        return [{"schema": "public", "table": "users", "column": "email"}]

    async def search_value(self, conn, value, tables=None, limit_per_column=5):
        return {
            "results": [{"schema": "public", "table": "users", "column": "email"}],
            "truncated": False,
        }


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


async def test_get_database_schema_connects_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.get_database_schema("prod")
    assert dialect.connected_read_only is True
    assert result["database"] == "prod"
    assert result["table_count"] == 1
    assert result["tables"][0]["name"] == "users"
    assert dialect.conn.closed is True


async def test_get_database_schema_writes_file_when_output_dir(cfg_path, monkeypatch, tmp_path):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.get_database_schema("prod", output_dir=str(tmp_path))
    # Summary is returned, not the (potentially huge) inline schema.
    assert "tables" not in result
    saved = Path(result["saved_to"])
    assert saved.exists()
    assert saved.name.startswith("prod_schema_") and saved.suffix == ".json"
    written = json.loads(saved.read_text(encoding="utf-8"))
    assert written["database"] == "prod"
    assert written["table_count"] == 1
    assert written["tables"][0]["name"] == "users"


async def test_get_database_schema_sql_returns_ddl_inline(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.get_database_schema("prod", format="sql")
    assert result["format"] == "sql"
    assert "CREATE TABLE public.users" in result["ddl"]
    assert "tables" not in result  # SQL form carries ddl, not the JSON table list
    assert dialect.connected_read_only is True
    assert dialect.conn.closed is True


async def test_get_database_schema_sql_writes_sql_file(cfg_path, monkeypatch, tmp_path):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.get_database_schema("prod", output_dir=str(tmp_path), format="sql")
    assert "ddl" not in result
    saved = Path(result["saved_to"])
    assert saved.exists() and saved.suffix == ".sql"
    assert saved.name.startswith("prod_schema_")
    assert "CREATE TABLE public.users" in saved.read_text(encoding="utf-8")


async def test_get_database_schema_rejects_unknown_format(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    with pytest.raises(ValueError, match="json.*sql"):
        await h.get_database_schema("prod", format="yaml")


async def test_dump_schema_faithful_returns_ddl_inline(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.dump_schema_faithful("prod")
    assert result["status"] == "ok"
    assert "CREATE TABLE x()" in result["ddl"]
    # the dialect receives the DSN, but the tool result never echoes it
    assert dialect.dumped_dsn == "postgresql://u:SECRET@h/prod"


async def test_dump_schema_faithful_writes_sql_file(cfg_path, monkeypatch, tmp_path):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    result = await h.dump_schema_faithful("prod", output_dir=str(tmp_path))
    saved = Path(result["saved_to"])
    assert saved.exists() and saved.suffix == ".sql"
    assert "ddl" not in result


async def test_dump_schema_faithful_passes_through_not_found(cfg_path, monkeypatch):
    dialect = FakeDialect(dump_result={"status": "pg_dump_not_found", "message": "install it"})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.dump_schema_faithful("prod")
    assert result == {"status": "pg_dump_not_found", "message": "install it"}


async def test_find_columns_connects_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.find_columns("prod", "email")
    assert dialect.connected_read_only is True
    assert result[0]["column"] == "email"
    assert dialect.conn.closed is True


async def test_search_value_connects_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.search_value("prod", "x@y.com", tables=["users"])
    assert dialect.connected_read_only is True
    assert result["results"][0]["table"] == "users"
    assert result["truncated"] is False
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


# ---- params & timeouts pass through (issue #8) --------------------------------


async def test_read_query_passes_params_and_timeout(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    await h.execute_read_query("prod", "SELECT * FROM t WHERE x = $1", ["O'Hara"], 2000)
    assert dialect.exec_calls[0] == {
        "sql": "SELECT * FROM t WHERE x = $1",
        "params": ["O'Hara"],
        "timeout_ms": 2000,
    }


async def test_write_query_passes_params(cfg_path, monkeypatch):
    dialect = FakeDialect(exec_result={"rows_affected": 1})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    await h.execute_write_query("dev", "UPDATE t SET x = $1", ["v"], user_consent=True)
    assert dialect.exec_calls[0]["params"] == ["v"]


# ---- dry-run writes ------------------------------------------------------------


async def test_dry_run_needs_no_consent_on_write_db(cfg_path, monkeypatch):
    dialect = FakeDialect(exec_result={"rows_affected": 7})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.execute_write_query("dev", "DELETE FROM t", dry_run=True)
    assert result == {"rows_affected": 7, "dry_run": True, "rolled_back": True}
    assert dialect.dry_run_calls and not dialect.exec_calls  # rolled-back path only


async def test_dry_run_still_blocked_on_read_db(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    with pytest.raises(WriteRejected, match="dry-run|read-only"):
        await h.execute_write_query("prod", "DELETE FROM t", dry_run=True)
    assert dialect.connected_read_only is None  # never connected


# ---- explain / cancel ------------------------------------------------------------


async def test_explain_query_connects_read_only(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.explain_query("prod", "SELECT 1", analyze=True)
    assert dialect.connected_read_only is True
    assert dialect.explained == [("SELECT 1", True)]
    assert result["plan"] == ["Seq Scan on users"]


async def test_cancel_query_passes_pid(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.cancel_query("prod", 4242)
    assert dialect.connected_read_only is True
    assert result == {"pid": 4242, "cancelled": True}


# ---- cursor lifecycle -------------------------------------------------------------


async def test_cursor_open_fetch_drain_autocloses(cfg_path, monkeypatch):
    dialect = FakeDialect(cursor_batches=[[{"id": 1}, {"id": 2}], [{"id": 3}]])
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)

    opened = await h.open_query_cursor("prod", "SELECT * FROM big")
    cursor_id = opened["cursor_id"]
    assert dialect.connected_read_only is True
    assert len(h._cursors) == 1

    first = await h.fetch_rows(cursor_id, 2)
    assert first["rows"] == [{"id": 1}, {"id": 2}]
    assert first["exhausted"] is False

    second = await h.fetch_rows(cursor_id, 2)  # short batch -> exhausted -> auto-close
    assert second["rows"] == [{"id": 3}]
    assert second["exhausted"] is True and second["cursor_closed"] is True
    assert dialect.cursor.closed is True
    assert h._cursors == {}


async def test_fetch_rows_unknown_cursor(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    with pytest.raises(ValueError, match="Unknown cursor_id"):
        await h.fetch_rows("nope", 10)


async def test_close_cursor_is_idempotent(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    opened = await h.open_query_cursor("prod", "SELECT 1")
    first = await h.close_cursor(opened["cursor_id"])
    again = await h.close_cursor(opened["cursor_id"])
    assert first["was_open"] is True and again["was_open"] is False
    assert dialect.cursor.closed is True


async def test_cursor_limit_enforced(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    for _ in range(handlers_mod.MAX_OPEN_CURSORS):
        await h.open_query_cursor("prod", "SELECT 1")
    with pytest.raises(ValueError, match="Too many open cursors"):
        await h.open_query_cursor("prod", "SELECT 1")


async def test_idle_cursors_are_reaped(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    opened = await h.open_query_cursor("prod", "SELECT 1")
    # Age the cursor past the TTL, then any cursor call reaps it.
    h._cursors[opened["cursor_id"]]["last_used"] -= handlers_mod.CURSOR_IDLE_TTL_SECONDS + 1
    with pytest.raises(ValueError, match="Unknown cursor_id"):
        await h.fetch_rows(opened["cursor_id"], 10)
    assert dialect.cursor.closed is True


# ---- object definitions -------------------------------------------------------------


async def test_get_object_definition_wraps_matches(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.get_object_definition("prod", "view", "active_users")
    assert dialect.connected_read_only is True
    assert result["object_type"] == "view"
    assert result["matches"][0]["name"] == "active_users"


# ---- diff_schemas ---------------------------------------------------------------------


def _table(name, columns=None, pk=None, fks=None):
    return {
        "schema": "public",
        "name": name,
        "columns": columns or [],
        "primary_key": pk or [],
        "foreign_keys": fks or [],
    }


def test_diff_schemas_pure_identical():
    a = {"tables": [_table("users")]}
    diff = handlers_mod._diff_schemas(a, {"tables": [_table("users")]})
    assert diff["identical"] is True
    assert diff["only_in_a"] == [] and diff["changed_tables"] == []


def test_diff_schemas_pure_detects_differences():
    col_int = {"name": "id", "type": "integer", "nullable": False, "default": None}
    col_big = {"name": "id", "type": "bigint", "nullable": False, "default": None}
    extra = {"name": "note", "type": "text", "nullable": True, "default": None}
    a = {"tables": [_table("users", [col_int, extra], pk=["id"]), _table("only_a")]}
    b = {"tables": [_table("users", [col_big], pk=["id", "org"]), _table("only_b")]}
    diff = handlers_mod._diff_schemas(a, b)
    assert diff["only_in_a"] == ["public.only_a"]
    assert diff["only_in_b"] == ["public.only_b"]
    changed = diff["changed_tables"][0]
    assert changed["table"] == "public.users"
    assert changed["columns_only_in_a"] == ["note"]
    assert changed["changed_columns"][0]["column"] == "id"
    assert changed["primary_key"] == {"a": ["id"], "b": ["id", "org"]}
    assert diff["identical"] is False


async def test_diff_schemas_handler_reads_both(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.diff_schemas("prod", "dev")
    assert result["database_a"] == "prod" and result["database_b"] == "dev"
    assert result["identical"] is True  # same FakeDialect schema both sides
    assert dialect.connected_read_only is True


# ---- sequence / stats / activity handlers ----------------------------------------------


async def test_check_sequences_counts_behind(cfg_path, monkeypatch):
    _patch_dialect(monkeypatch, FakeDialect())
    h = Handlers(cfg_path)
    result = await h.check_sequences("prod")
    assert result["behind_count"] == 1
    assert len(result["sequences"]) == 2


async def test_table_stats_handler(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.table_stats("prod")
    assert dialect.connected_read_only is True
    assert result["tables"][0]["table"] == "users"


async def test_show_activity_handler_defaults_sanitized(cfg_path, monkeypatch):
    dialect = FakeDialect()
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(cfg_path)
    result = await h.show_activity("prod")
    assert dialect.activity_include_query is False
    assert result["activity"][0]["pid"] == 1


def test_build_server_smoke(cfg_path):
    # The FastMCP app builds and exposes the 22 tools + 2 prompts without error.
    app = server.build_server(cfg_path)
    assert app is not None


# ---- _dsn_with_port (pure, issue #10) ------------------------------------------


def test_dsn_with_port_replaces_existing_port():
    out = handlers_mod._dsn_with_port("postgresql://u:pw@localhost:5432/db?sslmode=require", 5433)
    assert out == "postgresql://u:pw@localhost:5433/db?sslmode=require"


def test_dsn_with_port_appends_when_missing():
    assert handlers_mod._dsn_with_port("postgresql://u:pw@localhost/db", 5433) == (
        "postgresql://u:pw@localhost:5433/db"
    )


def test_dsn_with_port_preserves_credentials_with_special_chars():
    out = handlers_mod._dsn_with_port("postgresql://u%40x:p%23w@h:1/db", 9)
    assert out == "postgresql://u%40x:p%23w@h:9/db"


def test_dsn_with_port_unparsable_returns_none():
    assert handlers_mod._dsn_with_port("not a dsn at all", 5433) is None
    assert handlers_mod._dsn_with_port("postgresql://h:notaport/db", 5433) is None


# ---- fallback-port probing (issue #10) ------------------------------------------

TUNNEL_CONFIG = {
    "connections": [
        {
            "name": "tunnel",
            "dsn": "postgresql://u:SECRET@tunnelhost.invalid:5432/db",
            "mode": "read",
            "fallback_ports": [5433, 15432],
        },
        {
            "name": "plain",
            "dsn": "postgresql://u:SECRET@tunnelhost.invalid:5432/db",
            "mode": "read",
        },
    ]
}


@pytest.fixture
def tunnel_cfg_path(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(TUNNEL_CONFIG), encoding="utf-8")
    return path


class InvalidPasswordError(Exception):
    """Name-matched by diagnostics._classify -> AUTH_FAILED."""


class PortFakeDialect(FakeDialect):
    """Refuses, auth-rejects, or black-holes specific ports; records every port dialed."""

    def __init__(self, refuse_ports=(), auth_fail_ports=(), hang_ports=(), **kw):
        super().__init__(**kw)
        self.refuse_ports = set(refuse_ports)
        self.auth_fail_ports = set(auth_fail_ports)
        self.hang_ports = set(hang_ports)
        self.dialed: list[int] = []

    async def connect(self, dsn, *, read_only):
        port = int(dsn.rsplit("/", 1)[0].rsplit(":", 1)[1])
        self.dialed.append(port)
        if port in self.refuse_ports:
            raise ConnectionRefusedError("connection refused")
        if port in self.auth_fail_ports:
            raise InvalidPasswordError("bad password")
        if port in self.hang_ports:
            await asyncio.sleep(30)  # a firewalled port: never answers, never refuses
        return await super().connect(dsn, read_only=read_only)


async def test_probe_tries_fallbacks_in_order_and_remembers(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432, 5433})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    await h.list_tables("tunnel")
    assert dialect.dialed == [5432, 5433, 15432]
    assert h._active_ports == {"tunnel": 15432}


async def test_remembered_port_tried_first_next_time(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    h._active_ports["tunnel"] = 15432
    await h.list_tables("tunnel")
    assert dialect.dialed == [15432]


async def test_remembered_port_failure_reprobes_from_primary(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={15432})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    h._active_ports["tunnel"] = 15432
    await h.list_tables("tunnel")
    assert dialect.dialed == [15432, 5432]
    assert "tunnel" not in h._active_ports  # primary won -> memory cleared


async def test_auth_error_fails_fast_without_probing(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(auth_fail_ports={5432})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    with pytest.raises(handlers_mod.ConnectionFailedError) as exc:
        await h.list_tables("tunnel")
    assert dialect.dialed == [5432]  # no fallback attempted
    assert exc.value.diag["category"] == "AUTH_FAILED"
    assert "SECRET" not in str(exc.value)


async def test_all_ports_refused_reports_tried_ports_sanitized(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432, 5433, 15432})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    with pytest.raises(handlers_mod.ConnectionFailedError) as exc:
        await h.list_tables("tunnel")
    msg = str(exc.value)
    cause = exc.value.diag["cause"]
    assert "5433" in cause and "15432" in cause
    # Nothing DSN-derived reaches the agent — not the password, not the host (Rule 6).
    # (`localhost` is deliberately NOT asserted absent: the canned HOST_UNREACHABLE
    # advice mentions it as generic Docker guidance, fixed text never built from a DSN.)
    assert "SECRET" not in msg
    assert "tunnelhost.invalid" not in msg
    assert "SECRET" not in cause and "tunnelhost.invalid" not in cause


async def test_hanging_fallback_times_out_and_probing_continues(tunnel_cfg_path, monkeypatch):
    """A black-holed fallback must not stall the chain — the deadline moves it along."""
    dialect = PortFakeDialect(refuse_ports={5432}, hang_ports={5433})
    _patch_dialect(monkeypatch, dialect)
    monkeypatch.setattr(handlers_mod, "FALLBACK_CONNECT_TIMEOUT_SECONDS", 0.05)
    h = Handlers(tunnel_cfg_path)
    started = time.monotonic()
    await h.list_tables("tunnel")
    elapsed = time.monotonic() - started
    assert dialect.dialed == [5432, 5433, 15432]
    assert h._active_ports == {"tunnel": 15432}
    assert elapsed < 5  # the 30s hang was cut short, not waited out


async def test_all_fallbacks_hang_reports_tried_ports_sanitized(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432}, hang_ports={5433, 15432})
    _patch_dialect(monkeypatch, dialect)
    monkeypatch.setattr(handlers_mod, "FALLBACK_CONNECT_TIMEOUT_SECONDS", 0.05)
    h = Handlers(tunnel_cfg_path)
    with pytest.raises(handlers_mod.ConnectionFailedError) as exc:
        await h.list_tables("tunnel")
    assert dialect.dialed == [5432, 5433, 15432]
    cause = exc.value.diag["cause"]
    assert exc.value.diag["category"] == "HOST_UNREACHABLE"  # not UNKNOWN (py3.10 gotcha)
    assert "5433" in cause and "15432" in cause
    assert "SECRET" not in str(exc.value) and "tunnelhost.invalid" not in str(exc.value)


async def test_no_fallback_key_never_probes(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    with pytest.raises(handlers_mod.ConnectionFailedError):
        await h.list_tables("plain")
    assert dialect.dialed == [5432]


async def test_list_databases_shows_fallback_and_active_port(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432, 5433})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    await h.list_tables("tunnel")  # probe lands on 15432
    rows = {r["name"]: r for r in await h.list_databases()}
    assert rows["tunnel"]["fallback_ports"] == [5433, 15432]
    assert rows["tunnel"]["active_port"] == 15432
    assert "active_port" not in rows["plain"] and "fallback_ports" not in rows["plain"]


async def test_check_database_reports_active_port(tunnel_cfg_path, monkeypatch):
    dialect = PortFakeDialect(refuse_ports={5432, 5433})
    _patch_dialect(monkeypatch, dialect)
    h = Handlers(tunnel_cfg_path)
    report = {r["database"]: r for r in await h.check_database()}
    assert report["tunnel"]["status"] == "OK"
    assert report["tunnel"]["active_port"] == 15432
    assert "active_port" not in report["plain"]
