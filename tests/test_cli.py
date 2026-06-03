"""Tests for the CLI: argument dispatch and the wizard's pure helpers.

The interactive prompt loop itself (input/print) is intentionally thin and not unit
tested; all decision logic it relies on lives in pure, tested helpers.
"""

import json

import pytest

from db_conn_mcp import cli, config

# ---- argument parsing & dispatch ---------------------------------------------


def test_parser_defaults_to_stdio():
    args = cli.build_parser().parse_args([])
    assert args.transport == "stdio"
    assert args.command is None


def test_main_launches_server(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.server, "run", lambda **kw: calls.update(kw))
    rc = cli.main(["--transport", "http", "--config", "/tmp/c.json"])
    assert rc == 0
    assert calls == {"transport": "http", "config_path": "/tmp/c.json"}


def test_main_setup_dispatches(monkeypatch):
    marker = {}

    def fake_wizard():
        marker["ran"] = True
        return 0

    monkeypatch.setattr(cli, "run_setup_wizard", fake_wizard)
    rc = cli.main(["setup"])
    assert rc == 0
    assert marker["ran"] is True


# ---- register_database (pure, writes config) ---------------------------------


def test_register_database_writes_repo_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = cli.register_database("repo", "dev", "postgresql://u:p@h/dev", "write")
    assert path == config.repo_config_path()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["connections"][0]["name"] == "dev"
    assert written["connections"][0]["mode"] == "write"


def test_register_database_appends_to_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    cli.register_database("repo", "b", "postgresql://h/b", "read")
    cfg = config.load(str(config.repo_config_path()))
    assert [c.name for c in cfg.connections] == ["a", "b"]


def test_register_database_rejects_unknown_scheme(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="scheme"):
        cli.register_database("repo", "x", "mysql://h/x", "read")


def test_register_database_rejects_duplicate_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "dup", "postgresql://h/a", "read")
    with pytest.raises(ValueError, match="dup"):
        cli.register_database("repo", "dup", "postgresql://h/b", "read")


# ---- MCP injection helpers ---------------------------------------------------


def test_mcp_server_entry_shape(tmp_path):
    entry = cli.mcp_server_entry(tmp_path / "connections.json")
    assert entry["command"] == "db-conn-mcp"
    assert "--config" in entry["args"]


def test_inject_server_adds_entry():
    existing = {"mcpServers": {"other": {"command": "x"}}}
    result = cli.inject_server(existing, "db-conn-mcp", {"command": "db-conn-mcp"})
    assert "other" in result["mcpServers"]
    assert result["mcpServers"]["db-conn-mcp"]["command"] == "db-conn-mcp"


def test_inject_server_creates_mcpservers_key():
    result = cli.inject_server({}, "db-conn-mcp", {"command": "db-conn-mcp"})
    assert result["mcpServers"]["db-conn-mcp"]["command"] == "db-conn-mcp"


# ---- OS-aware agent config discovery -----------------------------------------


def test_claude_path_on_windows(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\me\AppData\Roaming")
    paths = cli.agent_config_paths()
    assert "claude" in paths
    assert paths["claude"].name == "claude_desktop_config.json"
    assert "Claude" in str(paths["claude"])


def test_cursor_path_present(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    paths = cli.agent_config_paths()
    assert paths["cursor"].name == "mcp.json"


def test_agy_path_present(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    paths = cli.agent_config_paths()
    assert "agy" in paths
    assert paths["agy"].name == "mcp_config.json"
    assert ".gemini" in str(paths["agy"])


def test_detected_agent_configs_only_returns_existing(tmp_path, monkeypatch):
    claude = tmp_path / "claude_desktop_config.json"
    claude.write_text("{}", encoding="utf-8")
    cursor = tmp_path / "mcp.json"  # intentionally NOT created
    monkeypatch.setattr(cli, "agent_config_paths", lambda: {"claude": claude, "cursor": cursor})
    detected = cli.detected_agent_configs()
    assert detected == {"claude": claude}


def test_detected_agent_configs_empty_when_none_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "agent_config_paths", lambda: {"claude": tmp_path / "nope.json"})
    assert cli.detected_agent_configs() == {}
