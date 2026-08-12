"""The codec seam (syntax-aware read/write behind one interface) plus Codex's spec."""

import json
import tomllib
import traceback

import pytest
import tomlkit

from db_conn_mcp.clients import (
    ClientConfigError,
    ClientSpec,
    _build_entry,
    client_specs,
    config_readable,
    inject_entry,
    injected_command,
    injected_launch,
    is_injected,
    read_config,
    remove_entry,
    write_config,
)

#: A path that breaks naive TOML basic-string formatting twice over:
#: ``\U`` opens a Unicode escape (loud failure), ``\t`` becomes a TAB (silent
#: corruption). Every serialization test uses it deliberately.
NASTY_PATH = r"C:\Users\dj\.local\bin\db-conn-mcp.exe"
NASTY_ARG = r"C:\Users\dj\testing\connections.json"


def _json_spec(tmp_path):
    return ClientSpec("t", "Test", tmp_path / "cfg.json", "mcpServers")


def test_read_config_absent_file_returns_empty_document(tmp_path):
    assert read_config(_json_spec(tmp_path)) == {}


def test_read_config_parses_existing_json(tmp_path):
    spec = _json_spec(tmp_path)
    spec.path.write_text(json.dumps({"mcpServers": {"a": {}}}), encoding="utf-8")
    assert read_config(spec) == {"mcpServers": {"a": {}}}


def test_read_config_raises_on_unparseable_file(tmp_path):
    spec = _json_spec(tmp_path)
    spec.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ClientConfigError):
        read_config(spec)


def test_read_config_error_never_echoes_file_contents(tmp_path):
    """Rule 6: a config may hold tokens; the message names the path, never the body."""
    spec = _json_spec(tmp_path)
    spec.path.write_text('{ "secret": "hunter2" ', encoding="utf-8")
    with pytest.raises(ClientConfigError) as excinfo:
        read_config(spec)
    assert "hunter2" not in str(excinfo.value)


def test_write_config_json_keeps_todays_byte_format(tmp_path):
    """Existing client files must see no gratuitous diff: indent=2 + trailing newline."""
    spec = _json_spec(tmp_path)
    write_config(spec, {"mcpServers": {"db-conn-mcp": {"command": "x", "args": []}}})
    expected = json.dumps({"mcpServers": {"db-conn-mcp": {"command": "x", "args": []}}}, indent=2)
    assert spec.path.read_text(encoding="utf-8") == expected + "\n"


def test_read_config_toml_roundtrips_through_the_codec(tmp_path):
    spec = ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")
    spec.path.write_text('model = "gpt-5"\n', encoding="utf-8")
    data = read_config(spec)
    assert data["model"] == "gpt-5"


def test_read_config_raises_on_unparseable_toml(tmp_path):
    spec = ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")
    spec.path.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ClientConfigError):
        read_config(spec)


def test_read_config_toml_error_never_echoes_file_contents(tmp_path):
    """Rule 6, TOML edge: tomlkit's ParseError quotes the offending key — we must not."""
    spec = ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")
    spec.path.write_text("my token hunter2 = 1\n", encoding="utf-8")
    with pytest.raises(ClientConfigError) as excinfo:
        read_config(spec)
    assert "hunter2" not in str(excinfo.value)


def test_parse_error_names_the_file_syntax_not_the_container_key(tmp_path):
    """Rule 6 wants the *category* of failure: 'JSON'/'TOML', not the internal fmt token."""
    spec = _json_spec(tmp_path)
    spec.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ClientConfigError) as excinfo:
        read_config(spec)
    assert "JSON" in str(excinfo.value)
    assert "mcpServers" not in str(excinfo.value)

    toml_spec = ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")
    toml_spec.path.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ClientConfigError) as excinfo:
        read_config(toml_spec)
    assert "TOML" in str(excinfo.value)


def test_parse_error_detaches_its_cause_so_tracebacks_cannot_leak(tmp_path):
    """tomlkit's ParseError quotes the offending raw key, so it must not ride along
    as ``__cause__`` — a printed traceback would render the file's contents."""
    spec = ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")
    spec.path.write_text("api_key sk-secret-abc123 = 1\n", encoding="utf-8")
    with pytest.raises(ClientConfigError) as excinfo:
        read_config(spec)
    exc = excinfo.value
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert "sk-secret-abc123" not in str(exc)
    assert "sk-secret-abc123" not in formatted


@pytest.mark.parametrize("body", ["[1, 2]", "null", '"just a string"', "42"])
def test_read_config_rejects_a_non_mapping_top_level(tmp_path, body):
    """Valid JSON is not enough: read_config promises a mutable *mapping*."""
    spec = _json_spec(tmp_path)
    spec.path.write_text(body, encoding="utf-8")
    with pytest.raises(ClientConfigError):
        read_config(spec)
    assert config_readable(spec) is False
    assert is_injected(spec) is False  # never raises, even on a valid-but-wrong document
    assert injected_command(spec) is None


def test_undecodable_bytes_are_an_unreadable_config_not_a_crash(tmp_path):
    """UnicodeDecodeError is a ValueError, so the old ``(JSONDecodeError, OSError)``
    guard missed it and a non-UTF-8 client config took `status` down."""
    spec = _json_spec(tmp_path)
    spec.path.write_bytes(b'{"mcpServers": {"\xff\xfe": {}}}')
    with pytest.raises(ClientConfigError):
        read_config(spec)
    assert config_readable(spec) is False
    assert is_injected(spec) is False
    assert injected_command(spec) is None


def test_is_injected_returns_false_for_unparseable_config(tmp_path):
    """Non-raising contract preserved: unreadable means 'cannot prove injected'."""
    spec = _json_spec(tmp_path)
    spec.path.write_text("{ not json", encoding="utf-8")
    assert is_injected(spec) is False


def test_injected_command_returns_none_for_unparseable_config(tmp_path):
    spec = _json_spec(tmp_path)
    spec.path.write_text("{ not json", encoding="utf-8")
    assert injected_command(spec) is None


def test_config_readable_reports_parse_state(tmp_path):
    spec = _json_spec(tmp_path)
    assert config_readable(spec) is True  # absent is fine — we would create it
    spec.path.write_text("{ not json", encoding="utf-8")
    assert config_readable(spec) is False
    spec.path.write_text("{}", encoding="utf-8")
    assert config_readable(spec) is True


def _codex_spec(tmp_path):
    return ClientSpec("codex", "Codex", tmp_path / "config.toml", "codex")


def test_codex_spec_is_registered():
    by_key = {s.key: s for s in client_specs()}
    assert by_key["codex"].fmt == "codex"
    assert by_key["codex"].path.name == "config.toml"


def test_codex_path_defaults_to_dot_codex(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("db_conn_mcp.clients.Path.home", lambda: tmp_path)
    by_key = {s.key: s for s in client_specs()}
    assert by_key["codex"].path == tmp_path / ".codex" / "config.toml"


def test_codex_home_env_var_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    by_key = {s.key: s for s in client_specs()}
    assert by_key["codex"].path == tmp_path / "elsewhere" / "config.toml"


def test_empty_codex_home_is_ignored(tmp_path, monkeypatch):
    """An exported-but-blank CODEX_HOME must not resolve to './config.toml'."""
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setattr("db_conn_mcp.clients.Path.home", lambda: tmp_path)
    by_key = {s.key: s for s in client_specs()}
    assert by_key["codex"].path == tmp_path / ".codex" / "config.toml"


def test_codex_entry_shape():
    entry = _build_entry("codex", NASTY_PATH, ["--config", NASTY_ARG])
    assert entry == {
        "command": NASTY_PATH,
        "args": ["--config", NASTY_ARG],
        "startup_timeout_sec": 30,
    }
    assert "transport" not in entry  # stdio is implied by `command`


def test_codex_uses_mcp_servers_container():
    out = inject_entry(tomlkit.document(), "codex", "db-conn-mcp", "cmd", [])
    assert "mcp_servers" in out


def test_windows_path_roundtrips_through_stdlib_tomllib(tmp_path):
    """The escape bug, pinned by an *independent* oracle.

    Checking tomlkit against tomlkit would prove nothing about backslashes, so the
    written bytes are re-parsed with stdlib tomllib.
    """
    spec = _codex_spec(tmp_path)
    write_config(
        spec,
        inject_entry(
            read_config(spec), "codex", "db-conn-mcp", NASTY_PATH, ["--config", NASTY_ARG]
        ),
    )
    written = spec.path.read_text(encoding="utf-8")
    parsed = tomllib.loads(written)["mcp_servers"]["db-conn-mcp"]
    assert parsed["command"] == NASTY_PATH
    assert parsed["args"] == ["--config", NASTY_ARG]
    assert parsed["startup_timeout_sec"] == 30
    assert "\t" not in parsed["command"]  # \t must not have become a real TAB


def test_inject_preserves_comments_and_other_servers(tmp_path):
    spec = _codex_spec(tmp_path)
    spec.path.write_text(
        "# My Codex config -- hand maintained.\n"
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-server"  # keep me\n',
        encoding="utf-8",
    )
    write_config(spec, inject_entry(read_config(spec), "codex", "db-conn-mcp", NASTY_PATH, []))
    after = spec.path.read_text(encoding="utf-8")
    assert "# My Codex config -- hand maintained." in after
    assert "# keep me" in after
    assert tomllib.loads(after)["mcp_servers"]["other"]["command"] == "other-server"
    assert tomllib.loads(after)["model"] == "gpt-5"


def test_uninject_preserves_comments_and_other_servers(tmp_path):
    spec = _codex_spec(tmp_path)
    spec.path.write_text(
        "# My Codex config -- hand maintained.\n"
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-server"  # keep me\n',
        encoding="utf-8",
    )
    write_config(spec, inject_entry(read_config(spec), "codex", "db-conn-mcp", NASTY_PATH, []))
    write_config(spec, remove_entry(read_config(spec), "codex", "db-conn-mcp"))
    after = spec.path.read_text(encoding="utf-8")
    assert "# My Codex config -- hand maintained." in after
    assert "# keep me" in after
    parsed = tomllib.loads(after)
    assert "db-conn-mcp" not in parsed["mcp_servers"]
    assert parsed["mcp_servers"]["other"]["command"] == "other-server"


def test_is_injected_and_injected_command_work_for_codex(tmp_path):
    spec = _codex_spec(tmp_path)
    assert is_injected(spec) is False
    write_config(spec, inject_entry(read_config(spec), "codex", "db-conn-mcp", NASTY_PATH, []))
    assert is_injected(spec) is True
    assert injected_command(spec) == NASTY_PATH


def test_injected_launch_returns_command_and_args(tmp_path):
    spec = _json_spec(tmp_path)
    write_config(
        spec,
        inject_entry(
            read_config(spec), "mcpServers", "db-conn-mcp", NASTY_PATH, ["--config", NASTY_ARG]
        ),
    )
    assert injected_launch(spec) == (NASTY_PATH, ["--config", NASTY_ARG])


def test_injected_launch_handles_zed_nesting(tmp_path):
    spec = ClientSpec("zed", "Zed", tmp_path / "settings.json", "zed")
    write_config(
        spec,
        inject_entry(read_config(spec), "zed", "db-conn-mcp", NASTY_PATH, ["--config", NASTY_ARG]),
    )
    assert injected_launch(spec) == (NASTY_PATH, ["--config", NASTY_ARG])


def test_injected_launch_none_when_absent_or_unreadable(tmp_path):
    spec = _json_spec(tmp_path)
    assert injected_launch(spec) is None
    spec.path.write_text("{ not json", encoding="utf-8")
    assert injected_launch(spec) is None


def test_injected_launch_missing_args_defaults_empty(tmp_path):
    spec = _json_spec(tmp_path)
    spec.path.write_text(
        json.dumps({"mcpServers": {"db-conn-mcp": {"command": NASTY_PATH}}}), encoding="utf-8"
    )
    assert injected_launch(spec) == (NASTY_PATH, [])
