"""Tests for config resolution, validation, lookup, and atomic persistence."""

import json
from pathlib import Path

import pytest

from db_conn_mcp import config
from db_conn_mcp.config import ConfigError
from db_conn_mcp.models import Config, Connection


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


VALID = {
    "connections": [
        {"name": "prod", "dsn": "postgresql://u:p@h/prod", "mode": "read"},
        {"name": "dev", "dsn": "postgres://u:p@h/dev", "mode": "write", "yolo": True},
    ]
}


# ---- resolution order --------------------------------------------------------


def test_resolve_explicit_path_wins(tmp_path):
    explicit = _write(tmp_path / "custom.json", VALID)
    assert config.resolve_path(str(explicit)) == explicit


def test_resolve_explicit_missing_path_errors(tmp_path):
    with pytest.raises(ConfigError):
        config.resolve_path(str(tmp_path / "nope.json"))


def test_resolve_prefers_repo_over_global(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = _write(tmp_path / "connections.json", VALID)
    _write(home / ".db-conn-mcp" / "connections.json", VALID)
    assert config.resolve_path() == repo


def test_resolve_falls_back_to_global(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    glob = _write(home / ".db-conn-mcp" / "connections.json", VALID)
    assert config.resolve_path() == glob


def test_resolve_none_found_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    with pytest.raises(ConfigError, match="setup"):
        config.resolve_path()


# ---- load + validation -------------------------------------------------------


def test_load_valid(tmp_path):
    path = _write(tmp_path / "c.json", VALID)
    cfg = config.load(str(path))
    assert [c.name for c in cfg.connections] == ["prod", "dev"]
    assert cfg.connections[1].yolo is True


def test_load_rejects_duplicate_names(tmp_path):
    payload = {
        "connections": [
            {"name": "dup", "dsn": "postgresql://h/a", "mode": "read"},
            {"name": "dup", "dsn": "postgresql://h/b", "mode": "read"},
        ]
    }
    path = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConfigError, match="dup"):
        config.load(str(path))


def test_load_rejects_unknown_scheme(tmp_path):
    payload = {"connections": [{"name": "x", "dsn": "mysql://h/a", "mode": "read"}]}
    path = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConfigError, match="scheme"):
        config.load(str(path))


def test_load_rejects_bad_mode(tmp_path):
    payload = {"connections": [{"name": "x", "dsn": "postgresql://h/a", "mode": "admin"}]}
    path = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConfigError):
        config.load(str(path))


def test_load_rejects_malformed_json(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load(str(path))


def test_config_error_never_leaks_dsn(tmp_path):
    secret = "postgresql://user:SUPERSECRET@host/db"
    payload = {
        "connections": [
            {"name": "dup", "dsn": secret, "mode": "read"},
            {"name": "dup", "dsn": secret, "mode": "read"},
        ]
    }
    path = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConfigError) as exc:
        config.load(str(path))
    assert "SUPERSECRET" not in str(exc.value)


# ---- lookup ------------------------------------------------------------------


def test_get_by_name():
    cfg = Config(connections=[Connection(name="prod", dsn="postgresql://h/p", mode="read")])
    assert config.get(cfg, "prod").name == "prod"


def test_get_unknown_lists_available():
    cfg = Config(connections=[Connection(name="prod", dsn="postgresql://h/p", mode="read")])
    with pytest.raises(ConfigError, match="prod"):
        config.get(cfg, "missing")


# ---- save + set_yolo ---------------------------------------------------------


def test_save_round_trip(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config(connections=[Connection(name="dev", dsn="postgresql://h/d", mode="write")])
    config.save(cfg, path)
    reloaded = config.load(str(path))
    assert reloaded.connections[0].name == "dev"


def test_set_yolo_persists(tmp_path):
    path = _write(tmp_path / "c.json", VALID)
    updated = config.set_yolo(path, "prod", True)
    assert config.get(updated, "prod").yolo is True
    # persisted to disk, survives reload
    assert config.get(config.load(str(path)), "prod").yolo is True


def test_set_yolo_unknown_db_errors(tmp_path):
    path = _write(tmp_path / "c.json", VALID)
    with pytest.raises(ConfigError, match="ghost"):
        config.set_yolo(path, "ghost", True)
