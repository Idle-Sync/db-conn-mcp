"""The verification engine: spawn a real server, speak real MCP to it."""

import json
import socket
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from db_conn_mcp import __version__, verify
from db_conn_mcp.verify import EXPECTED_TOOL_COUNT, verify_http, verify_stdio


def _temp_config(tmp_path):
    """A throwaway config for the spawned server.

    It holds one entry because ``list_databases`` returns a list, and FastMCP emits
    *no* text block for an empty one — the DSN is never dialled (the handler only
    reads config), so nothing here touches the network.
    """
    entry = {"name": "verify-fixture", "dsn": "postgresql://u@127.0.0.1:1/x", "mode": "read"}
    p = tmp_path / "connections.json"
    p.write_text(json.dumps({"connections": [entry]}), encoding="utf-8")
    return p


async def test_verify_stdio_end_to_end_against_this_repo(tmp_path):
    """The load-bearing test: the repo's own server must answer real MCP."""
    cfg = _temp_config(tmp_path)
    result = await verify_stdio(
        sys.executable, ["-m", "db_conn_mcp", "--config", str(cfg)], timeout=60.0
    )
    assert result["verdict"] == "answers", result["detail"]
    assert result["tool_count"] == EXPECTED_TOOL_COUNT
    assert result["server_version"] == __version__
    assert result["stale"] is False
    assert result["instructions"]  # the untrusted-data policy arrived
    assert result["list_databases_text"] is not None
    assert result["list_databases_error"] is None


async def test_verify_stdio_reports_tool_call_failed_for_a_malformed_config(tmp_path):
    """A server that starts but cannot read its config must not look healthy (#52).

    Two connections with one name are valid JSON, so the server starts and answers the
    handshake; only ``list_databases`` fails, with a sanitized ``ConfigError``.
    """
    entry = {"name": "twin", "dsn": "postgresql://u:hunter2-marker@127.0.0.1:1/x", "mode": "read"}
    cfg = tmp_path / "connections.json"
    cfg.write_text(json.dumps({"connections": [entry, entry]}), encoding="utf-8")
    result = await verify_stdio(
        sys.executable, ["-m", "db_conn_mcp", "--config", str(cfg)], timeout=60.0
    )
    assert result["verdict"] == "tool_call_failed", result["detail"]
    assert result["tool_count"] == EXPECTED_TOOL_COUNT
    assert result["list_databases_text"] is None
    assert "Duplicate" in result["list_databases_error"]
    assert "hunter2-marker" not in json.dumps(result)


class _FakeSession:
    """A client session whose ``list_databases`` fails with no text, on a wrong tool count."""

    def __init__(self, *_streams):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def initialize(self):
        server_info = SimpleNamespace(name="db-conn-mcp", version="0.0.0-stale")
        return SimpleNamespace(serverInfo=server_info, instructions=None)

    async def list_tools(self):
        return SimpleNamespace(tools=[object()])

    async def call_tool(self, _name, _arguments):
        return SimpleNamespace(isError=True, content=[])


@asynccontextmanager
async def _fake_stdio_client(_params):
    yield None, None


async def test_verify_stdio_tool_call_failed_wins_over_count_and_keeps_stale(monkeypatch):
    """A failed tool call is not downgraded to a count warning, even with no error text."""
    monkeypatch.setattr(verify, "ClientSession", _FakeSession)
    monkeypatch.setattr(verify, "stdio_client", _fake_stdio_client)
    result = await verify_stdio("ignored", [])
    assert result["verdict"] == "tool_call_failed"
    assert result["tool_count"] == 1
    assert result["stale"] is True
    assert result["list_databases_error"] is None
    assert result["list_databases_text"] is None
    assert result["suggested_action"].startswith("fix the connections.json")


async def test_verify_stdio_launch_failed_for_missing_binary(tmp_path):
    result = await verify_stdio(str(tmp_path / "no-such-binary.exe"), [], timeout=15.0)
    assert result["verdict"] == "launch_failed"
    assert result["suggested_action"]


async def test_verify_stdio_handshake_failed_for_non_mcp_process():
    """A process that runs but speaks no MCP must not hang or crash the engine."""
    result = await verify_stdio(
        sys.executable, ["-c", "print('hello'); import time; time.sleep(5)"], timeout=8.0
    )
    assert result["verdict"] in ("handshake_failed", "timeout")


async def test_verify_client_without_entry_reports_launch_failed(tmp_path):
    from db_conn_mcp.clients import ClientSpec
    from db_conn_mcp.verify import verify_client

    spec = ClientSpec("t", "Test", tmp_path / "cfg.json", "mcpServers")
    result = await verify_client(spec)
    assert result["verdict"] == "launch_failed"
    assert "no db-conn-mcp entry" in result["detail"]


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


async def test_verify_http_end_to_end(tmp_path):
    if not _port_free(8000):
        pytest.skip("port 8000 busy on this machine; HTTP verify would report port_in_use")
    cfg = _temp_config(tmp_path)
    result = await verify_http(
        sys.executable, ["-m", "db_conn_mcp", "--config", str(cfg)], timeout=60.0
    )
    assert result["verdict"] == "answers", result["detail"]
    assert result["tool_count"] == EXPECTED_TOOL_COUNT


async def test_verify_http_reports_port_in_use(tmp_path):
    cfg = _temp_config(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        result = await verify_http(
            sys.executable, ["-m", "db_conn_mcp", "--config", str(cfg)], port=busy_port
        )
        assert result["verdict"] == "port_in_use"
    finally:
        blocker.close()
