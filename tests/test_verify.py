"""The verification engine: spawn a real server, speak real MCP to it."""

import json
import socket
import sys

import pytest

from db_conn_mcp import __version__
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
