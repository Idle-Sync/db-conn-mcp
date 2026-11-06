"""Tests for config resolution, validation, lookup, and atomic persistence."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_non_utf8_file_is_a_config_error_with_no_file_contents(tmp_path):
    """The decode error carries the file's bytes, so it must not stay as the cause."""
    path = tmp_path / "connections.json"
    path.write_bytes('{"connections": [{"dsn": "postgresql://u:SEKRET@h/d"}]}'.encode("utf-16"))
    with pytest.raises(ConfigError, match="is not valid JSON") as caught:
        config.load(str(path))
    assert caught.value.__suppress_context__
    assert "SEKRET" not in str(caught.value)


# ---- save + set_yolo ---------------------------------------------------------


def test_save_round_trip(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config(connections=[Connection(name="dev", dsn="postgresql://h/d", mode="write")])
    config.save(cfg, path)
    reloaded = config.load(str(path))
    assert reloaded.connections[0].name == "dev"


def test_save_creates_the_config_directory(tmp_path):
    """The first `setup` writes into `~/.db-conn-mcp`, which does not exist yet."""
    path = tmp_path / "new_dir" / "connections.json"
    config.save(Config(), path)
    assert config.load(str(path)).connections == []


def test_save_writes_two_space_json_with_a_trailing_newline(tmp_path):
    """Hand-edited and diffed by users, so the layout is part of what a save must keep."""
    path = tmp_path / "connections.json"
    cfg = Config(connections=[Connection(name="dev", dsn="postgresql://h/d", mode="write")])
    config.save(cfg, path)
    document = {
        "connections": [{"name": "dev", "dsn": "postgresql://h/d", "mode": "write", "yolo": False}]
    }
    assert path.read_text(encoding="utf-8") == json.dumps(document, indent=2) + "\n"


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


# ---- fallback_ports (issue #10) -----------------------------------------------


def test_fallback_ports_optional_and_default_none():
    c = Connection(name="a", dsn="postgresql://u:p@h:5432/d", mode="read")
    assert c.fallback_ports is None
    assert "fallback_ports" not in c.public_view()


def test_fallback_ports_in_public_view_when_set():
    c = Connection(name="a", dsn="postgresql://u:p@h:5432/d", mode="read", fallback_ports=[5433])
    assert c.public_view()["fallback_ports"] == [5433]
    assert "dsn" not in c.public_view()


def test_fallback_ports_validates_range():
    with pytest.raises(ValidationError):
        Connection(name="a", dsn="postgresql://u@h/d", mode="read", fallback_ports=[0])
    with pytest.raises(ValidationError):
        Connection(name="a", dsn="postgresql://u@h/d", mode="read", fallback_ports=[70000])


def test_save_never_injects_fallback_ports_into_old_files(tmp_path):
    """An existing user's file must not gain the key on a set_yolo rewrite."""
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {"connections": [{"name": "a", "dsn": "postgresql://u:p@h/d", "mode": "write"}]}
        ),
        encoding="utf-8",
    )
    config.set_yolo(path, "a", True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "fallback_ports" not in saved["connections"][0]
    assert saved["connections"][0]["yolo"] is True  # existing dump behavior kept


def test_save_round_trips_fallback_ports(tmp_path):
    path = tmp_path / "connections.json"
    cfg = Config(
        connections=[
            Connection(
                name="a", dsn="postgresql://u:p@h/d", mode="read", fallback_ports=[5433, 15432]
            )
        ]
    )
    config.save(cfg, path)
    reloaded = config.load(str(path))
    assert reloaded.connections[0].fallback_ports == [5433, 15432]


# ---- file mode (issue #43) -----------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits only")


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@posix_only
def test_file_mode_0600_survives_set_yolo(tmp_path):
    path = _write(tmp_path / "connections.json", VALID)
    path.chmod(0o600)
    config.set_yolo(path, "prod", True)
    assert _mode(path) == 0o600


@posix_only
def test_file_mode_0640_survives_save(tmp_path):
    """A mode the user chose on purpose (group-readable, say) is theirs to keep."""
    path = _write(tmp_path / "connections.json", VALID)
    path.chmod(0o640)
    config.save(config.load(str(path)), path)
    assert _mode(path) == 0o640


@posix_only
def test_file_mode_of_a_file_save_creates_is_0600(tmp_path):
    """The file holds plaintext DSNs: it must never start out world-readable."""
    path = tmp_path / "connections.json"
    config.save(Config(), path)
    assert _mode(path) == 0o600


# ---- unknown keys (issue #46) --------------------------------------------------

WITH_UNKNOWN_KEYS = {
    "notes": "y",
    "owner": None,
    "connections": [
        {
            "name": "a",
            "dsn": "postgresql://u:p@h/d",
            "mode": "write",
            "comment": "x",
            "flag": None,
        }
    ],
}


def test_unknown_keys_survive_set_yolo(tmp_path):
    """Null-valued keys too: `exclude_none` is only meant for this tool's own optionals."""
    path = _write(tmp_path / "connections.json", WITH_UNKNOWN_KEYS)
    config.set_yolo(path, "a", True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["notes"] == "y"
    assert saved["owner"] is None and "owner" in saved
    conn = saved["connections"][0]
    assert conn["comment"] == "x"
    assert conn["flag"] is None and "flag" in conn
    assert conn["yolo"] is True
    assert "fallback_ports" not in conn


def test_saved_layout_with_unknown_keys_is_pinned(tmp_path):
    """Non-null unknown top-level keys stay ahead of ``connections``; null ones follow."""
    path = _write(tmp_path / "connections.json", WITH_UNKNOWN_KEYS)
    config.set_yolo(path, "a", True)
    document = {
        "notes": "y",
        "connections": [
            {
                "name": "a",
                "dsn": "postgresql://u:p@h/d",
                "mode": "write",
                "yolo": True,
                "comment": "x",
                "flag": None,
            }
        ],
        "owner": None,
    }
    assert path.read_text(encoding="utf-8") == json.dumps(document, indent=2) + "\n"


def test_unknown_keys_survive_load_but_stay_out_of_public_view(tmp_path):
    """A kept key is the file's business, not the agent's: it may hold anything."""
    path = _write(tmp_path / "connections.json", WITH_UNKNOWN_KEYS)
    assert "comment" not in config.get(config.load(str(path)), "a").public_view()


# ---- cross-process writes (issue #45) ------------------------------------------

_SET_YOLO_REPEATEDLY = """
import sys
from pathlib import Path

from db_conn_mcp import config

path, name, writes = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for j in range(writes):
    config.set_yolo(path, name, j % 2 == 1)
"""


def test_cross_process_set_yolo_loses_no_update(tmp_path):
    """Each child's last write sets its own flag, so one lost update leaves a False."""
    config_dir = tmp_path / "config_dir"
    path = _write(
        config_dir / "connections.json",
        {
            "connections": [
                {"name": f"p{i}", "dsn": "postgresql://h/d", "mode": "write"} for i in range(8)
            ]
        },
    )
    writes = 50 if sys.platform == "win32" else 200
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}

    def run_child(i: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _SET_YOLO_REPEATEDLY, str(path), f"p{i}", str(writes)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    # Threads only to overlap the children: run in sequence, they could never collide.
    with ThreadPoolExecutor(max_workers=8) as pool:
        children = list(pool.map(run_child, range(8)))

    report = "\n".join(f"child {i} rc={c.returncode}\n{c.stderr}" for i, c in enumerate(children))
    assert [c.returncode for c in children] == [0] * 8, report
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [conn["yolo"] for conn in saved["connections"]] == [True] * 8
    assert sorted(p.name for p in config_dir.iterdir()) == [
        "connections.json",
        "connections.json.lock",
    ]


@posix_only
def test_locked_puts_the_sidecar_beside_the_symlink_target(tmp_path):
    """Two spellings of one config (link and real path) must contend on one lock file."""
    real = _write(tmp_path / "real" / "connections.json", VALID)
    link_dir = tmp_path / "link_dir"
    link_dir.mkdir()
    link = link_dir / "connections.json"
    link.symlink_to(real)

    with config.locked(link):
        pass

    assert _mode(tmp_path / "real" / "connections.json.lock") == 0o600
    assert [p.name for p in link_dir.iterdir()] == ["connections.json"]


@posix_only
def test_locked_closes_its_file_descriptor(tmp_path):
    """The MCP server is long-lived: one leaked descriptor per yolo toggle adds up."""
    path = _write(tmp_path / "connections.json", VALID)
    open_before = len(os.listdir("/dev/fd"))
    with config.locked(path):
        pass
    assert len(os.listdir("/dev/fd")) == open_before


def test_locked_creates_the_sidecar_directory_for_a_config_not_yet_written(tmp_path):
    """A first save takes the lock before the save can create the config's directory."""
    path = tmp_path / "new_dir" / "connections.json"
    with config.locked(path):
        pass
    assert path.with_name("connections.json.lock").exists()
