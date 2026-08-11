"""The codec seam: syntax-aware read/write behind one format-agnostic interface."""

import json

import pytest

from db_conn_mcp.clients import (
    ClientConfigError,
    ClientSpec,
    read_config,
    write_config,
)


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
