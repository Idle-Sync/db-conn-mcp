"""Tests for the CLI: argument dispatch and the wizard's pure helpers.

The interactive prompt loop's decision logic lives in pure, tested helpers; the
wizard's transactional behavior (commit-at-end, graceful Ctrl+C) is tested by
driving it with scripted ``input``.
"""

import builtins
import io
import json
import sys

import pytest

from db_conn_mcp import cli, clients, config
from db_conn_mcp import cli as cli_module


def _scripted_input(answers):
    """Return a fake ``input`` that yields each answer, then raises KeyboardInterrupt.

    A sentinel of ``KeyboardInterrupt`` in the list simulates Ctrl+C at that prompt.
    """
    it = iter(answers)

    def _input(prompt=""):
        try:
            value = next(it)
        except StopIteration as exc:
            raise KeyboardInterrupt from exc
        if value is KeyboardInterrupt:
            raise KeyboardInterrupt
        return value

    return _input


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path_factory, monkeypatch):
    """Point the global config at an empty temp dir so tests never touch the real one."""
    gdir = tmp_path_factory.mktemp("global-home")
    monkeypatch.setattr(config, "global_config_path", lambda: gdir / "connections.json")


# ---- argument parsing & dispatch ---------------------------------------------


def test_parser_defaults_to_stdio():
    args = cli.build_parser().parse_args([])
    assert args.transport == "stdio"
    assert args.command is None


def test_version_flag_prints_version(capsys):
    from db_conn_mcp import __commit__, __version__

    for flag in ("-v", "--version"):
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args([flag])
        assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert __commit__ in out


def test_main_launches_server(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.server, "run", lambda **kw: calls.update(kw))
    rc = cli.main(["--transport", "http", "--config", "/tmp/c.json"])
    assert rc == 0
    assert calls == {"transport": "http", "config_path": "/tmp/c.json", "gui": True}


def test_main_stdio_on_tty_explains_instead_of_hanging(monkeypatch, capsys):
    """A human running the bare stdio server in a terminal gets guidance, not a hang."""
    ran = {"server": False}
    monkeypatch.setattr(cli.server, "run", lambda **kw: ran.update(server=True))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = cli.main([])
    assert rc == 1
    assert ran["server"] is False  # server never started -> cannot block on stdin
    captured = capsys.readouterr()
    assert "setup" in captured.err  # guidance points at the real commands
    assert captured.out == ""  # nothing on the stdout protocol channel


def test_main_stdio_with_piped_stdin_launches_server(monkeypatch):
    """When stdin is a pipe (a real MCP client), the stdio server runs normally."""
    calls = {}
    monkeypatch.setattr(cli.server, "run", lambda **kw: calls.update(kw))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    rc = cli.main([])
    assert rc == 0
    assert calls == {"transport": "stdio", "config_path": None, "gui": True}


def test_main_http_on_tty_still_runs_server(monkeypatch):
    """http is a legitimate foreground server; a TTY must not block it."""
    calls = {}
    monkeypatch.setattr(cli.server, "run", lambda **kw: calls.update(kw))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = cli.main(["--transport", "http"])
    assert rc == 0
    assert calls == {"transport": "http", "config_path": None, "gui": True}


def test_star_cta_prints_only_when_called(capsys):
    """The star nudge fires from its function — never as a side effect of import."""
    out = capsys.readouterr()  # drop anything an earlier fixture printed
    cli._print_star_cta()
    out = capsys.readouterr().out
    assert cli.REPO_URL in out
    assert "db-conn-mcp gui" in out  # the tip is part of the same call


def test_star_cta_survives_a_cp1252_stdout(monkeypatch):
    """A redirected Windows console cannot encode the star — the tip must still land.

    The star used to print first and unguarded, so ``UnicodeEncodeError`` ate the
    dashboard tip on exactly the platform the tip is most needed on.
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    cli._print_star_cta()
    stream.flush()
    printed = stream.buffer.getvalue().decode("cp1252")
    assert "db-conn-mcp gui" in printed
    assert cli.REPO_URL in printed
    assert "⭐" not in printed  # swapped for the ASCII stand-in, not dropped


def test_main_setup_dispatches(monkeypatch):
    marker = {}

    def fake_wizard(config_arg=None):
        marker["ran"] = True
        return 0

    monkeypatch.setattr(cli, "run_setup_wizard", fake_wizard)
    rc = cli.main(["setup"])
    assert rc == 0
    assert marker["ran"] is True


def test_parser_has_management_subcommands():
    p = cli.build_parser()
    assert p.parse_args(["status"]).command == "status"
    assert p.parse_args(["add"]).command == "add"
    assert p.parse_args(["clients"]).command == "clients"
    rm = p.parse_args(["remove", "db1"])
    assert rm.command == "remove" and rm.name == "db1"
    yo = p.parse_args(["yolo", "db1", "on"])
    assert yo.command == "yolo" and yo.name == "db1" and yo.state == "on"


def test_config_flag_works_after_subcommand():
    # --config is accepted both before and after the subcommand.
    assert cli.build_parser().parse_args(["status", "--config", "x.json"]).config == "x.json"
    assert cli.build_parser().parse_args(["--config", "x.json", "status"]).config == "x.json"


def test_main_dispatches_management_commands(monkeypatch):
    seen = {}

    def fake_status(config_arg):
        seen["status"] = config_arg
        return 0

    def fake_remove(config_arg, name):
        seen["remove"] = name
        return 0

    def fake_yolo(config_arg, name, state):
        seen["yolo"] = (name, state)
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    monkeypatch.setattr(cli, "cmd_remove", fake_remove)
    monkeypatch.setattr(cli, "cmd_yolo", fake_yolo)
    assert cli.main(["--config", "c.json", "status"]) == 0
    assert cli.main(["remove", "db1"]) == 0
    assert cli.main(["yolo", "db1", "off"]) == 0
    assert seen == {"status": "c.json", "remove": "db1", "yolo": ("db1", "off")}


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


def test_server_launch_prefers_installed_script(monkeypatch, tmp_path):
    # When the console script resolves on PATH, inject its ABSOLUTE path so the
    # MCP client doesn't need it on its own PATH.
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\venv\Scripts\db-conn-mcp.exe")
    command, args = cli.server_launch(tmp_path / "connections.json")
    assert command == r"C:\venv\Scripts\db-conn-mcp.exe"
    assert args == ["--config", str(tmp_path / "connections.json")]


def test_server_launch_falls_back_to_module(monkeypatch, tmp_path):
    # No console script found -> run the package via the current interpreter.
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    command, args = cli.server_launch(tmp_path / "connections.json")
    assert command == cli.sys.executable
    assert args[:2] == ["-m", "db_conn_mcp"]
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
        "codex",
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
    # detected_clients lives in clients.py, so its client_specs lookup binds there;
    # cli.detected_clients is the same function, re-exported for back-compat.
    monkeypatch.setattr(clients, "client_specs", lambda: [present, missing])
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


# ---- wizard: transactional behavior & graceful Ctrl+C ------------------------


def test_wizard_completes_and_saves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "detected_clients", lambda: [])  # no injection prompts
    monkeypatch.setattr(
        builtins, "input", _scripted_input(["r", "mydb", "postgresql://h/db", "r", ""])
    )
    rc = cli.run_setup_wizard()
    assert rc == 0
    cfg = config.load(str(config.repo_config_path()))
    assert cfg.connections[0].name == "mydb"


def test_wizard_ctrl_c_at_start_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, "input", _scripted_input([KeyboardInterrupt]))
    rc = cli.run_setup_wizard()
    assert rc == 130
    assert not config.repo_config_path().is_file()


def test_wizard_ctrl_c_after_db_prompts_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client_cfg = tmp_path / "claude.json"
    client_cfg.write_text("{}", encoding="utf-8")
    fake = cli.ClientSpec("claude", "Claude Desktop", client_cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [fake])
    # Answer all DB prompts, then Ctrl+C at the injection selection.
    monkeypatch.setattr(
        builtins,
        "input",
        _scripted_input(["r", "mydb", "postgresql://h/db", "r", "", KeyboardInterrupt]),
    )
    rc = cli.run_setup_wizard()
    assert rc == 130
    assert not config.repo_config_path().is_file()  # connection NOT persisted
    assert client_cfg.read_text(encoding="utf-8") == "{}"  # client config untouched


def test_setup_existing_config_shows_status_and_menu(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "mydb", "postgresql://h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    # Config exists -> setup shows status, then we quit the menu.
    monkeypatch.setattr(builtins, "input", _scripted_input(["q"]))
    rc = cli.run_setup_wizard(str(config.repo_config_path()))
    assert rc == 0
    out = capsys.readouterr().out
    assert "mydb" in out
    assert "https://github.com/Idle-Sync/db-conn-mcp" in out


def test_wizard_prints_star_cta_on_success(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    monkeypatch.setattr(
        builtins, "input", _scripted_input(["r", "mydb", "postgresql://h/db", "r", ""])
    )
    rc = cli.run_setup_wizard()
    assert rc == 0
    assert cli.REPO_URL in capsys.readouterr().out  # nudge shown the moment it worked


def test_wizard_no_star_cta_on_cancel(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, "input", _scripted_input([KeyboardInterrupt]))
    rc = cli.run_setup_wizard()
    assert rc == 130
    assert cli.REPO_URL not in capsys.readouterr().out  # cancel never asks for a star


# ---- management commands -----------------------------------------------------


def test_is_injected_detects_presence(tmp_path):
    f = tmp_path / "c.json"
    spec = cli.ClientSpec("x", "X", f, "mcpServers")
    f.write_text(json.dumps({"mcpServers": {"db-conn-mcp": {}}}), encoding="utf-8")
    assert cli.is_injected(spec) is True
    f.write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8")
    assert cli.is_injected(spec) is False
    # honors the per-format container key
    vs = cli.ClientSpec("v", "V", f, "vscode")
    f.write_text(json.dumps({"servers": {"db-conn-mcp": {}}}), encoding="utf-8")
    assert cli.is_injected(vs) is True
    missing = cli.ClientSpec("m", "M", tmp_path / "nope.json", "mcpServers")
    assert cli.is_injected(missing) is False


def test_cmd_status_lists_dbs_without_dsn(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://u:SECRET@h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    rc = cli.cmd_status(str(config.repo_config_path()))
    out = capsys.readouterr().out
    assert rc == 0
    assert "a" in out and "SECRET" not in out


def test_cmd_status_no_config_returns_1(tmp_path):
    assert cli.cmd_status(str(tmp_path / "nope.json")) == 1


def test_cmd_add_appends(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    monkeypatch.setattr(builtins, "input", _scripted_input(["b", "postgresql://h/b", "r", ""]))
    rc = cli.cmd_add(str(config.repo_config_path()))
    assert rc == 0
    cfg = config.load(str(config.repo_config_path()))
    assert [c.name for c in cfg.connections] == ["a", "b"]


def test_cmd_add_duplicate_name_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "mydb", "postgresql://h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    monkeypatch.setattr(builtins, "input", _scripted_input(["mydb", "postgresql://h/b", "r", ""]))
    rc = cli.cmd_add(str(config.repo_config_path()))
    assert rc == 1
    cfg = config.load(str(config.repo_config_path()))
    assert [c.name for c in cfg.connections] == ["mydb"]


def test_cmd_remove(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    cli.register_database("repo", "b", "postgresql://h/b", "read")
    rc = cli.cmd_remove(str(config.repo_config_path()), "a")
    assert rc == 0
    cfg = config.load(str(config.repo_config_path()))
    assert [c.name for c in cfg.connections] == ["b"]


def test_cmd_remove_unknown_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    rc = cli.cmd_remove(str(config.repo_config_path()), "ghost")
    assert rc == 1
    assert [c.name for c in config.load(str(config.repo_config_path())).connections] == ["a"]


def test_cmd_yolo_on_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "write")
    p = str(config.repo_config_path())
    assert cli.cmd_yolo(p, "a", "on") == 0
    assert config.get(config.load(p), "a").yolo is True
    assert cli.cmd_yolo(p, "a", "off") == 0
    assert config.get(config.load(p), "a").yolo is False


def test_cmd_yolo_unknown_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "write")
    assert cli.cmd_yolo(str(config.repo_config_path()), "ghost", "on") == 1


def test_cmd_clients_injects_selected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    client_cfg = tmp_path / "claude.json"
    client_cfg.write_text("{}", encoding="utf-8")
    fake = cli.ClientSpec("claude", "Claude Desktop", client_cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [fake])
    monkeypatch.setattr(builtins, "input", _scripted_input(["1"]))  # select client #1
    rc = cli.cmd_clients(str(config.repo_config_path()))
    assert rc == 0
    data = json.loads(client_cfg.read_text(encoding="utf-8"))
    assert "db-conn-mcp" in data["mcpServers"]


# ---- uninject (clients --remove) ---------------------------------------------


def test_remove_entry_pure():
    out = cli.remove_entry(
        {"mcpServers": {"db-conn-mcp": {}, "other": {}}}, "mcpServers", "db-conn-mcp"
    )
    assert "db-conn-mcp" not in out["mcpServers"]
    assert "other" in out["mcpServers"]  # other entries preserved
    # missing entry is a no-op, not an error
    assert cli.remove_entry({}, "mcpServers", "db-conn-mcp") == {}


def test_cmd_clients_remove_uninjects(tmp_path, monkeypatch):
    client_cfg = tmp_path / "claude.json"
    client_cfg.write_text(
        json.dumps({"mcpServers": {"db-conn-mcp": {"command": "x"}, "keep": {}}}), encoding="utf-8"
    )
    fake = cli.ClientSpec("claude", "Claude Desktop", client_cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [fake])
    monkeypatch.setattr(builtins, "input", _scripted_input(["1"]))
    rc = cli.cmd_clients(remove=True)  # no config needed to uninject
    assert rc == 0
    data = json.loads(client_cfg.read_text(encoding="utf-8"))
    assert "db-conn-mcp" not in data["mcpServers"]
    assert "keep" in data["mcpServers"]


def test_cmd_clients_remove_when_none_injected(tmp_path, monkeypatch, capsys):
    """Every config parsed, so the plain sentence stands — no hedging for the common case."""
    client_cfg = tmp_path / "claude.json"
    client_cfg.write_text("{}", encoding="utf-8")
    fake = cli.ClientSpec("claude", "Claude Desktop", client_cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [fake])
    rc = cli.cmd_clients(remove=True)
    assert rc == 0  # clean no-op
    out = capsys.readouterr().out
    assert out.strip() == "db-conn-mcp is not injected into any detected MCP client."


# ---- check (doctor) ----------------------------------------------------------


def test_cmd_check_all_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")

    class FakeHandlers:
        def __init__(self, path):
            pass

        async def check_database(self, name=None):
            return [{"database": "a", "status": "OK"}]

    monkeypatch.setattr(cli, "Handlers", FakeHandlers)
    rc = cli.cmd_check(str(config.repo_config_path()), None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "a" in out and "OK" in out


def test_cmd_check_unreachable_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")

    class FakeHandlers:
        def __init__(self, path):
            pass

        async def check_database(self, name=None):
            return [{"database": "a", "status": "UNREACHABLE", "detail": "[AUTH_FAILED] ..."}]

    monkeypatch.setattr(cli, "Handlers", FakeHandlers)
    rc = cli.cmd_check(str(config.repo_config_path()), None)
    assert rc == 2


def test_cmd_check_no_config_returns_1(tmp_path):
    assert cli.cmd_check(str(tmp_path / "nope.json"), None) == 1


def test_main_dispatches_clients_remove_and_check(monkeypatch):
    seen = {}

    def fake_clients(config_arg, remove=False):
        seen["clients"] = remove
        return 0

    def fake_check(config_arg, name):
        seen["check"] = name
        return 0

    monkeypatch.setattr(cli, "cmd_clients", fake_clients)
    monkeypatch.setattr(cli, "cmd_check", fake_check)
    assert cli.main(["clients", "--remove"]) == 0
    assert cli.main(["check", "db1"]) == 0
    assert seen == {"clients": True, "check": "db1"}


# ---- reset (delete the whole config — fresh slate) --------------------------


def test_parser_has_reset():
    assert cli.build_parser().parse_args(["reset"]).command == "reset"


def test_cmd_reset_deletes_on_confirm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    p = config.repo_config_path()
    assert p.is_file()
    monkeypatch.setattr(builtins, "input", _scripted_input(["y"]))
    rc = cli.cmd_reset(str(p))
    assert rc == 0
    assert not p.is_file()  # whole config removed


def test_cmd_reset_cancel_keeps_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    p = config.repo_config_path()
    monkeypatch.setattr(builtins, "input", _scripted_input(["n"]))
    rc = cli.cmd_reset(str(p))
    assert rc == 0
    assert p.is_file()  # untouched on decline


def test_cmd_reset_no_config_is_already_fresh(tmp_path):
    assert cli.cmd_reset(str(tmp_path / "nope.json")) == 0


# ---- fallback-ports prompt parsing (issue #10) ---------------------------------


def test_parse_fallback_ports_empty_means_none():
    assert cli._parse_fallback_ports("") is None


def test_parse_fallback_ports_parses_csv():
    assert cli._parse_fallback_ports(" 5433, 15432 ") == [5433, 15432]


def test_parse_fallback_ports_rejects_junk_and_range():
    with pytest.raises(ValueError):
        cli._parse_fallback_ports("abc")
    with pytest.raises(ValueError):
        cli._parse_fallback_ports("5433, 70000")


def test_wizard_persists_fallback_ports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    monkeypatch.setattr(
        builtins, "input", _scripted_input(["b", "postgresql://h/b", "r", "5433, 15432"])
    )
    assert cli.cmd_add(str(config.repo_config_path())) == 0
    cfg = config.load(str(config.repo_config_path()))
    assert cfg.connections[-1].fallback_ports == [5433, 15432]


def test_wizard_rejects_invalid_fallback_ports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    monkeypatch.setattr(builtins, "input", _scripted_input(["b", "postgresql://h/b", "r", "70000"]))
    assert cli.cmd_add(str(config.repo_config_path())) == 1
    cfg = config.load(str(config.repo_config_path()))
    assert [c.name for c in cfg.connections] == ["a"]  # nothing persisted


def test_status_shows_fallback_ports(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://u:SECRET@h/a", "read")
    p = config.repo_config_path()
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["connections"][0]["fallback_ports"] = [5433]
    p.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(cli, "detected_clients", lambda: [])
    assert cli.cmd_status(str(p)) == 0
    out = capsys.readouterr().out
    assert "fallback_ports=[5433]" in out
    assert "SECRET" not in out


def test_cmd_check_prints_active_port(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "a", "postgresql://h/a", "read")

    class FakeHandlers:
        def __init__(self, path):
            pass

        async def check_database(self, name=None):
            return [{"database": "a", "status": "OK", "active_port": 15432}]

    monkeypatch.setattr(cli, "Handlers", FakeHandlers)
    rc = cli.cmd_check(str(config.repo_config_path()), None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "a: OK (active_port=15432)" in out


def test_injected_command_reads_mcpservers_entry(tmp_path):
    from db_conn_mcp.clients import ClientSpec, injected_command

    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"db-conn-mcp": {"command": "/x/db-conn-mcp", "args": []}}}),
        encoding="utf-8",
    )
    spec = ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    assert injected_command(spec) == "/x/db-conn-mcp"


def test_injected_command_none_when_absent(tmp_path):
    from db_conn_mcp.clients import ClientSpec, injected_command

    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")
    spec = ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    assert injected_command(spec) is None


def test_injected_command_reads_zed_nested_path(tmp_path):
    from db_conn_mcp.clients import ClientSpec, injected_command

    cfg = tmp_path / "settings.json"
    cfg.write_text(
        json.dumps(
            {
                "context_servers": {
                    "db-conn-mcp": {
                        "source": "custom",
                        "command": {"path": "/x/db-conn-mcp", "args": []},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    spec = ClientSpec("zed", "Zed", cfg, "zed")
    assert injected_command(spec) == "/x/db-conn-mcp"


def test_injected_command_none_for_non_dict_entry(tmp_path):
    from db_conn_mcp.clients import ClientSpec, injected_command

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"db-conn-mcp": "not-a-dict"}}), encoding="utf-8")
    spec = ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    assert injected_command(spec) is None


def test_doctor_exit_zero_when_no_fail(monkeypatch, capsys):
    async def fake_run_checks(path, *, offline=False):
        return [
            {"check": "pypi_latest", "status": "ok", "detail": "fresh", "suggested_action": "none"},
            {
                "check": "secrets_exposure",
                "status": "warn",
                "detail": "loose",
                "suggested_action": "fix_permissions",
            },
            {
                "check": "process_staleness",
                "status": "skipped",
                "detail": "no psutil",
                "suggested_action": "none",
            },
        ]

    monkeypatch.setattr(cli_module.doctor, "run_checks", fake_run_checks)
    monkeypatch.setattr(cli_module, "_existing_config", lambda arg: None)
    assert cli_module.cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "pypi_latest" in out and "secrets_exposure" in out


def test_doctor_exit_two_on_fail(monkeypatch, capsys):
    async def fake_run_checks(path, *, offline=False):
        return [
            {
                "check": "connectivity",
                "status": "fail",
                "detail": "db: unreachable",
                "suggested_action": "none",
            }
        ]

    monkeypatch.setattr(cli_module.doctor, "run_checks", fake_run_checks)
    monkeypatch.setattr(cli_module, "_existing_config", lambda arg: None)
    assert cli_module.cmd_doctor() == 2


def test_doctor_offline_flag_parses():
    args = cli_module.build_parser().parse_args(["doctor", "--offline"])
    assert args.command == "doctor"
    assert args.offline is True


class _FakeStream:
    """Minimal stdout stand-in that only carries an encoding."""

    def __init__(self, encoding):
        self.encoding = encoding


def test_doctor_markers_use_icons_on_utf8(monkeypatch):
    monkeypatch.setattr(cli_module.sys, "stdout", _FakeStream("utf-8"))
    assert cli_module._status_markers() == cli_module._DOCTOR_ICONS


def test_doctor_markers_fall_back_to_ascii_on_cp1252(monkeypatch):
    """A Windows cp1252 stream must get ASCII markers, not a UnicodeEncodeError."""
    monkeypatch.setattr(cli_module.sys, "stdout", _FakeStream("cp1252"))
    assert cli_module._status_markers() == cli_module._DOCTOR_ASCII


def test_doctor_prints_without_crashing_on_cp1252(monkeypatch, capsys):
    async def fake_run_checks(path, *, offline=False):
        return [
            {
                "check": "connectivity",
                "status": "skipped",
                "detail": "nothing to probe",
                "suggested_action": "none",
            }
        ]

    monkeypatch.setattr(cli_module.doctor, "run_checks", fake_run_checks)
    monkeypatch.setattr(cli_module, "_existing_config", lambda arg: None)
    monkeypatch.setattr(cli_module, "_status_markers", lambda: cli_module._DOCTOR_ASCII)
    assert cli_module.cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "connectivity" in out
    assert out.isascii()


def test_inject_refuses_to_clobber_unparseable_config(tmp_path, capsys):
    """The whole point of the no-clobber rule: a broken file survives untouched."""
    cfg = tmp_path / "broken.json"
    original = '{ "mcpServers": { "other": '  # truncated on purpose
    cfg.write_text(original, encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    cli._inject_into_client(spec, "db-conn-mcp", ["--config", "x"])
    assert cfg.read_text(encoding="utf-8") == original
    out = capsys.readouterr().out
    assert "Broken Client" in out
    assert "other" not in out  # Rule 6: the file's contents are never echoed


def test_uninject_refuses_to_clobber_unparseable_config(tmp_path, capsys):
    cfg = tmp_path / "broken.json"
    original = "{ not json at all"
    cfg.write_text(original, encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    cli._uninject_from_client(spec)
    assert cfg.read_text(encoding="utf-8") == original


def test_inject_refuses_to_clobber_a_non_mapping_top_level(tmp_path, capsys):
    """Valid JSON, wrong shape: a top-level array is still someone's file, not ours."""
    cfg = tmp_path / "array.json"
    original = '["secret-token-abc123"]'
    cfg.write_text(original, encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    cli._inject_into_client(spec, "db-conn-mcp", ["--config", "x"])
    assert cfg.read_text(encoding="utf-8") == original
    out = capsys.readouterr().out
    assert "Broken Client" in out
    assert "secret-token-abc123" not in out  # Rule 6


def test_detected_clients_still_lists_a_broken_config(tmp_path, monkeypatch):
    """Discovery is 'the file exists', not 'the file parses' — hiding it hides the fix."""
    broken = cli.ClientSpec("b", "B", tmp_path / "b.json", "mcpServers")
    (tmp_path / "b.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(clients, "client_specs", lambda: [broken])
    assert [s.key for s in cli.detected_clients()] == ["b"]


def test_injection_menu_marks_an_unreadable_config(tmp_path, monkeypatch, capsys):
    """The listing must show the broken client so the user can go fix it (CHANGELOG promise)."""
    cfg = tmp_path / "broken.json"
    cfg.write_text('{ "mcpServers": { "secret-token-abc123": ', encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [spec])
    monkeypatch.setattr(builtins, "input", _scripted_input([""]))
    assert cli._choose_injection_targets() == []
    out = capsys.readouterr().out
    assert "Broken Client" in out
    assert "config unreadable" in out
    assert "secret-token-abc123" not in out  # Rule 6


def test_uninject_flags_an_unreadable_config_when_nothing_injected(tmp_path, monkeypatch, capsys):
    """`clients --remove` must not claim a clean slate when a config could not be read."""
    cfg = tmp_path / "broken.json"
    original = '{ "mcpServers": { "other": '  # truncated on purpose
    cfg.write_text(original, encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [spec])
    assert cli.cmd_clients(remove=True) == 0
    out = capsys.readouterr().out
    assert "Broken Client" in out
    assert str(cfg) in out
    # The bare "nothing injected" line must not be the whole story — and, with a file we
    # could not read, it must not claim more than it checked.
    assert "db-conn-mcp is not injected into any client it could check." in out
    assert "any detected MCP client" not in out
    assert cfg.read_text(encoding="utf-8") == original  # byte-identical


def test_uninject_numbering_skips_an_unreadable_config(tmp_path, monkeypatch, capsys):
    """The unreadable client is flagged but unselectable, so '1' still means the injected one."""
    broken = tmp_path / "broken.json"
    broken_original = "{ not json at all"
    broken.write_text(broken_original, encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"mcpServers": {"db-conn-mcp": {"command": "x"}, "keep": {}}}), encoding="utf-8"
    )
    specs = [
        cli.ClientSpec("broken", "Broken Client", broken, "mcpServers"),
        cli.ClientSpec("claude", "Claude Desktop", good, "mcpServers"),
    ]
    monkeypatch.setattr(cli, "detected_clients", lambda: specs)
    monkeypatch.setattr(builtins, "input", _scripted_input(["1"]))
    assert cli.cmd_clients(remove=True) == 0
    out = capsys.readouterr().out
    assert "Broken Client" in out  # flagged...
    assert "1. Claude Desktop" in out  # ...but never numbered
    data = json.loads(good.read_text(encoding="utf-8"))
    assert "db-conn-mcp" not in data["mcpServers"]  # '1' removed from the injected client
    assert "keep" in data["mcpServers"]
    assert broken.read_text(encoding="utf-8") == broken_original  # untouched


def test_uninject_never_echoes_the_broken_config_contents(tmp_path, monkeypatch, capsys):
    """Rule 6: the notice names the client and the path only."""
    cfg = tmp_path / "array.json"
    cfg.write_text('["secret-token-abc123"]', encoding="utf-8")  # valid JSON, wrong shape
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [spec])
    assert cli.cmd_clients(remove=True) == 0
    assert "secret-token-abc123" not in capsys.readouterr().out


def test_cli_has_gui_subcommand_and_no_gui_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["--no-gui"])
    assert args.no_gui is True
    args = parser.parse_args(["gui"])
    assert args.command == "gui"


def test_gui_command_starts_the_dashboard_standalone(tmp_path, monkeypatch):
    """With nothing on the port, `gui` runs the standalone server with --config."""
    seen = []
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "absent-token")
    monkeypatch.setattr(
        "db_conn_mcp.gui.app.run_standalone", lambda config_arg=None: seen.append(config_arg) or 0
    )
    assert cli.main(["gui", "--config", "somewhere.json"]) == 0
    assert seen == ["somewhere.json"]


def test_gui_command_reports_a_foreign_holder_of_the_port(tmp_path, monkeypatch, capsys):
    """run_standalone's exit code 2 becomes a human instruction, not a traceback."""
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "absent-token")
    monkeypatch.setattr("db_conn_mcp.gui.app.run_standalone", lambda config_arg=None: 2)
    assert cli.main(["gui"]) == 2
    out = capsys.readouterr().out
    assert "31415" in out and "db-conn-mcp gui" in out


def test_gui_command_reuses_a_live_dashboard_without_the_token_in_the_url(
    tmp_path, monkeypatch, capsys
):
    """Rule 6 / I-3: the probe carries the token in a HEADER and prints it nowhere."""
    from db_conn_mcp.gui.app import TOKEN_HEADER

    (tmp_path / "gui-token").write_text("s3cret-token\n", encoding="utf-8")
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "gui-token")
    monkeypatch.setattr(
        "db_conn_mcp.gui.app.run_standalone",
        lambda config_arg=None: pytest.fail("a live dashboard must be reused, not restarted"),
    )
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"app": "db-conn-mcp-gui"}

    def _get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers or {}, timeout=timeout)
        return _Resp()

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("webbrowser.open", lambda url: seen.update(opened=url))
    assert cli.main(["gui"]) == 0
    assert seen["headers"][TOKEN_HEADER] == "s3cret-token"
    assert "s3cret-token" not in seen["url"]
    assert seen["timeout"] == 2.0
    assert "s3cret-token" not in capsys.readouterr().out


def test_gui_command_ignores_a_stranger_on_the_port(tmp_path, monkeypatch):
    """A 200 from something that is not our dashboard is not ours — fall through."""
    (tmp_path / "gui-token").write_text("s3cret-token", encoding="utf-8")
    monkeypatch.setattr("db_conn_mcp.gui.app.token_path", lambda: tmp_path / "gui-token")
    started = []
    monkeypatch.setattr(
        "db_conn_mcp.gui.app.run_standalone", lambda config_arg=None: started.append(1) or 2
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"app": "something-else"}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("not our dashboard"))
    assert cli.main(["gui"]) == 2
    assert started == [1]


def test_no_gui_flag_reaches_server_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.server, "run", lambda **kwargs: seen.update(kwargs))
    assert cli.main(["--transport", "http", "--no-gui"]) == 0
    assert seen["gui"] is False
    assert cli.main(["--transport", "http"]) == 0
    assert seen["gui"] is True


def test_status_marks_an_unreadable_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "broken.json"
    cfg.write_text('{ "mcpServers": { "secret-token-abc123": ', encoding="utf-8")
    spec = cli.ClientSpec("broken", "Broken Client", cfg, "mcpServers")
    monkeypatch.setattr(cli, "detected_clients", lambda: [spec])
    monkeypatch.chdir(tmp_path)
    cli.register_database("repo", "mydb", "postgresql://h/a", "read")
    cli._print_status(config.repo_config_path())
    out = capsys.readouterr().out
    assert "Broken Client: config unreadable" in out
    assert "secret-token-abc123" not in out  # Rule 6
