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


def test_register_database_rejects_duplicate_dsn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "first", "postgresql://u:p@h/db", "read")
    # Same DSN under a different name should be refused, naming the existing one.
    with pytest.raises(ValueError, match="first"):
        cli.register_database("repo", "second", "postgresql://u:p@h/db", "write")


def test_register_database_duplicate_dsn_error_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret = "postgresql://u:SUPERSECRET@h/db"
    cli.register_database("repo", "first", secret, "read")
    with pytest.raises(ValueError) as exc:
        cli.register_database("repo", "second", secret, "read")
    assert "SUPERSECRET" not in str(exc.value)


# ---- format-aware injection --------------------------------------------------


def test_server_command_and_args(tmp_path):
    assert cli.SERVER_COMMAND == "db-conn-mcp"
    args = cli.server_args(tmp_path / "connections.json")
    assert args[0] == "--config"
    assert str(tmp_path / "connections.json") in args


def test_inject_mcpservers_format():
    out = cli.inject_entry({}, "mcpServers", "db-conn-mcp", "db-conn-mcp", ["--config", "x"])
    assert out["mcpServers"]["db-conn-mcp"] == {"command": "db-conn-mcp", "args": ["--config", "x"]}


def test_inject_preserves_existing_entries():
    existing = {"mcpServers": {"other": {"command": "y"}}}
    out = cli.inject_entry(existing, "mcpServers", "db-conn-mcp", "db-conn-mcp", [])
    assert "other" in out["mcpServers"]
    assert "db-conn-mcp" in out["mcpServers"]


def test_inject_vscode_format_uses_servers_key_and_type():
    out = cli.inject_entry({}, "vscode", "db-conn-mcp", "db-conn-mcp", ["--config", "x"])
    assert out["servers"]["db-conn-mcp"] == {
        "type": "stdio",
        "command": "db-conn-mcp",
        "args": ["--config", "x"],
    }


def test_inject_zed_format_uses_context_servers_nested_command():
    out = cli.inject_entry({}, "zed", "db-conn-mcp", "db-conn-mcp", ["--config", "x"])
    assert out["context_servers"]["db-conn-mcp"] == {
        "source": "custom",
        "command": {"path": "db-conn-mcp", "args": ["--config", "x"]},
    }


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


def test_client_specs_cover_expected(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    keys = {s.key for s in cli.client_specs()}
    assert {
        "claude",
        "cursor",
        "agy",
        "windsurf",
        "claude-code",
        "cline",
        "vscode",
        "zed",
    } <= keys


def test_client_spec_formats_and_paths(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    by_key = {s.key: s for s in cli.client_specs()}
    assert by_key["vscode"].fmt == "vscode"
    assert by_key["vscode"].path.name == "mcp.json"
    assert by_key["zed"].fmt == "zed"
    assert by_key["zed"].path.name == "settings.json"
    assert by_key["windsurf"].fmt == "mcpServers"
    assert ".codeium" in str(by_key["windsurf"].path)
    assert by_key["claude-code"].path.name == ".claude.json"
    assert by_key["cline"].path.name == "cline_mcp_settings.json"


def test_detected_clients_only_returns_existing(tmp_path, monkeypatch):
    present = cli.ClientSpec("a", "A", tmp_path / "a.json", "mcpServers")
    missing = cli.ClientSpec("b", "B", tmp_path / "b.json", "mcpServers")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "client_specs", lambda: [present, missing])
    assert [s.key for s in cli.detected_clients()] == ["a"]


# ---- client selection parsing ------------------------------------------------


def test_selection_empty_is_none():
    assert cli.parse_client_selection("", 3) == []
    assert cli.parse_client_selection("   ", 3) == []


def test_selection_all_keyword():
    assert cli.parse_client_selection("all", 3) == [0, 1, 2]
    assert cli.parse_client_selection("ALL", 2) == [0, 1]


def test_selection_comma_and_space_separated():
    assert cli.parse_client_selection("1,3", 3) == [0, 2]
    assert cli.parse_client_selection("1 3", 3) == [0, 2]
    assert cli.parse_client_selection("2", 3) == [1]


def test_selection_dedups_and_sorts():
    assert cli.parse_client_selection("3,1,1", 3) == [0, 2]


def test_selection_ignores_out_of_range_and_garbage():
    assert cli.parse_client_selection("1, x, 9", 3) == [0]
    assert cli.parse_client_selection("0", 3) == []  # 1-based; 0 is invalid


def test_selection_scales_to_n_clients():
    # Not hardcoded to 3 — works for however many clients are detected.
    assert cli.parse_client_selection("all", 8) == [0, 1, 2, 3, 4, 5, 6, 7]
    assert cli.parse_client_selection("2,5,8", 8) == [1, 4, 7]
    assert cli.parse_client_selection("8", 8) == [7]
    assert cli.parse_client_selection("9", 8) == []  # 9 out of range for 8 clients
    assert cli.parse_client_selection("all", 0) == []  # no clients -> nothing
