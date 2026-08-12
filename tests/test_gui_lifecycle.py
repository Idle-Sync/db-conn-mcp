"""GUI lifecycle: port claiming, stdout purity, log hygiene, idle shutdown decision.

TEST ORDER MATTERS in this file. ``test_start_in_thread_serves_and_writes_token``
claims the real GUI port for the rest of the pytest process (daemon thread, no
clean stop), so it MUST stay last — the skip test needs the port free-then-taken
on its own terms, and every other test here binds an ephemeral port instead.
"""

import socket
import time

import pytest
from starlette.testclient import TestClient

from db_conn_mcp.gui.app import (
    GUI_PORT,
    TOKEN_HEADER,
    _idle_exceeded,
    create_app,
    run_standalone,
    start_in_thread,
)


def test_idle_decision_is_pure():
    now = time.monotonic()
    assert _idle_exceeded(now - 901, now, 900.0) is True
    assert _idle_exceeded(now - 10, now, 900.0) is False


def test_on_request_hook_only_fires_for_authenticated_requests():
    """The idle clock is stamped by real traffic, not by rejected probes."""
    ticks = []
    app = create_app("lifecycle-token", on_request=lambda: ticks.append(1))
    client = TestClient(app, base_url=f"http://127.0.0.1:{GUI_PORT}")
    assert client.get("/api/summary").status_code == 403
    assert ticks == []
    assert client.get("/api/summary", headers={TOKEN_HEADER: "lifecycle-token"}).status_code == 200
    assert len(ticks) == 1


@pytest.fixture()
def fake_uvicorn(tmp_path, monkeypatch):
    """Run both launchers without a real server: capture their uvicorn configs."""
    captured = []
    sockets = []

    class _FakeServer:
        def __init__(self, config):
            captured.append(config)
            self.should_exit = False

        def run(self, sockets=None):
            self.should_exit = True

    def _ephemeral_bind():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        sockets.append(sock)
        return sock

    monkeypatch.setattr("db_conn_mcp.gui.app.uvicorn.Server", _FakeServer)
    monkeypatch.setattr("db_conn_mcp.gui.app._bind_gui_port", _ephemeral_bind)
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "gui-token")
    yield captured
    for sock in sockets:
        sock.close()


def test_both_launchers_disable_the_uvicorn_access_log(fake_uvicorn, capfd):
    """Rule 6: the access log would print ``?token=<SECRET>`` in every request line."""
    assert start_in_thread() is True
    assert run_standalone(open_browser=False, idle_timeout=900.0) == 0
    ride_along, standalone = fake_uvicorn
    assert ride_along.access_log is False
    assert standalone.access_log is False
    # The ride-along additionally silences uvicorn entirely: it shares a process
    # with a stdio MCP server, whose stdout carries the protocol. log_config=None
    # matters on its own — uvicorn's default config runs dictConfig, reconfiguring
    # logging inside the host process and pointing a handler at stdout.
    assert ride_along.log_level == "critical"
    assert ride_along.log_config is None
    assert capfd.readouterr().out == ""


def test_run_standalone_reports_a_held_port(fake_uvicorn, monkeypatch):
    """The Task 10 contract: exit code 2 means "someone else already serves it"."""
    monkeypatch.setattr("db_conn_mcp.gui.app._bind_gui_port", lambda: None)
    assert run_standalone(open_browser=False) == 2
    assert fake_uvicorn == []  # no server was configured, let alone started


def test_start_in_thread_skips_when_port_taken(tmp_path, monkeypatch, capfd):
    # Pointed at tmp_path so a regression that writes the token BEFORE winning the
    # bind fails here instead of silently clobbering the live GUI's real token.
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "gui-token")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", GUI_PORT))
    blocker.listen(1)
    try:
        assert start_in_thread() is False
    finally:
        blocker.close()
    assert not (tmp_path / "gui-token").exists()  # the loser writes nothing
    assert capfd.readouterr().out == ""  # stdout is sacred


def test_server_run_skips_gui_when_disabled(monkeypatch):
    """--no-gui reaches the hook: the dashboard is only offered when gui=True."""
    from db_conn_mcp import server as server_mod

    calls = []
    monkeypatch.setattr(
        "db_conn_mcp.gui.app.start_in_thread", lambda config_arg=None: calls.append(1)
    )
    # run()'s refactor exposes _run_fastmcp for exactly this seam: it owns building
    # the MCP app, so this test never needs a real connections.json.
    monkeypatch.setattr(server_mod, "_run_fastmcp", lambda transport, config_path: None)
    server_mod.run(transport="stdio", config_path=None, gui=False)
    assert calls == []
    server_mod.run(transport="stdio", config_path=None, gui=True)
    assert calls == [1]


def test_server_run_survives_a_broken_gui(monkeypatch):
    """A GUI failure must never stop the MCP server from starting."""
    from db_conn_mcp import server as server_mod

    started = []

    def _boom(config_arg=None):
        raise RuntimeError("gui exploded")

    monkeypatch.setattr("db_conn_mcp.gui.app.start_in_thread", _boom)
    monkeypatch.setattr(
        server_mod, "_run_fastmcp", lambda transport, config_path: started.append(transport)
    )
    server_mod.run(transport="stdio", config_path=None, gui=True)
    assert started == ["stdio"]


def test_start_in_thread_serves_and_writes_token(tmp_path, monkeypatch, capfd):
    import httpx

    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "gui-token")
    assert start_in_thread() is True
    token = (tmp_path / "gui-token").read_text(encoding="utf-8").strip()
    for _ in range(50):  # wait for uvicorn to accept
        try:
            r = httpx.get(
                f"http://127.0.0.1:{GUI_PORT}/api/summary", headers={"X-GUI-Token": token}
            )
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    assert r.status_code == 200
    assert capfd.readouterr().out == ""  # nothing on stdout, ever
