"""Tests for config resolution, validation, lookup, and atomic persistence."""

import json
import os
import re
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
    config.save(cfg, path, previous=Config(), source="cli")
    reloaded = config.load(str(path))
    assert reloaded.connections[0].name == "dev"


def test_save_creates_the_config_directory(tmp_path):
    """The first `setup` writes into `~/.db-conn-mcp`, which does not exist yet."""
    path = tmp_path / "new_dir" / "connections.json"
    config.save(Config(), path, previous=Config(), source="cli")
    assert config.load(str(path)).connections == []


def test_save_writes_two_space_json_with_a_trailing_newline(tmp_path):
    """Hand-edited and diffed by users, so the layout is part of what a save must keep."""
    path = tmp_path / "connections.json"
    cfg = Config(connections=[Connection(name="dev", dsn="postgresql://h/d", mode="write")])
    config.save(cfg, path, previous=Config(), source="cli")
    document = {
        "connections": [{"name": "dev", "dsn": "postgresql://h/d", "mode": "write", "yolo": False}]
    }
    assert path.read_text(encoding="utf-8") == json.dumps(document, indent=2) + "\n"


def test_set_yolo_persists(tmp_path):
    path = _write(tmp_path / "c.json", VALID)
    updated = config.set_yolo(path, "prod", True, source="cli")
    assert config.get(updated, "prod").yolo is True
    # persisted to disk, survives reload
    assert config.get(config.load(str(path)), "prod").yolo is True


def test_set_yolo_unknown_db_errors(tmp_path):
    path = _write(tmp_path / "c.json", VALID)
    with pytest.raises(ConfigError, match="ghost"):
        config.set_yolo(path, "ghost", True, source="cli")


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
    config.set_yolo(path, "a", True, source="cli")
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
    config.save(cfg, path, previous=Config(), source="cli")
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
    config.set_yolo(path, "prod", True, source="cli")
    assert _mode(path) == 0o600


@posix_only
def test_file_mode_0640_survives_save(tmp_path):
    """A mode the user chose on purpose (group-readable, say) is theirs to keep."""
    path = _write(tmp_path / "connections.json", VALID)
    path.chmod(0o640)
    loaded = config.load(str(path))
    config.save(loaded, path, previous=loaded, source="cli")
    assert _mode(path) == 0o640


@posix_only
def test_file_mode_of_a_file_save_creates_is_0600(tmp_path):
    """The file holds plaintext DSNs: it must never start out world-readable."""
    path = tmp_path / "connections.json"
    config.save(Config(), path, previous=Config(), source="cli")
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
    config.set_yolo(path, "a", True, source="cli")
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
    config.set_yolo(path, "a", True, source="cli")
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
    config.set_yolo(path, name, j % 2 == 1, source="cli")
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
        "connections.json.audit.log",
        "connections.json.lock",
    ]
    # Each child's first write keeps the default False, so it records nothing.
    audit_lines = (config_dir / "connections.json.audit.log").read_text(encoding="utf-8")
    assert len(audit_lines.splitlines()) == 8 * (writes - 1)


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


# ---- audit sidecar (issue #37) -------------------------------------------------

YOLO_OFF = {"connections": [{"name": "dev", "dsn": "postgresql://u:SECRET@h/dev", "mode": "write"}]}


def _audit_lines(config_path: Path) -> list[str]:
    sidecar = config_path.with_name(config_path.name + ".audit.log")
    return sidecar.read_text(encoding="utf-8").splitlines()


def test_save_without_audit_keywords_is_a_type_error(tmp_path):
    """The keywords are what make every save leave its line: none may skip them."""
    with pytest.raises(TypeError):
        config.save(Config(), tmp_path / "connections.json")


def test_save_with_no_change_creates_no_audit_sidecar(tmp_path):
    path = _write(tmp_path / "connections.json", VALID)
    loaded = config.load(str(path))
    config.save(loaded, path, previous=loaded.model_copy(deep=True), source="cli")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["connections.json"]


def test_set_yolo_appends_one_audit_line_and_none_when_unchanged(tmp_path):
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    config.set_yolo(path, "dev", True, source="cli")
    lines = _audit_lines(path)
    assert len(lines) == 1
    timestamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"
    assert re.fullmatch(timestamp + '\tcli\tedit\t"dev"\tyolo: false -> true', lines[0])
    config.set_yolo(path, "dev", True, source="cli")
    assert _audit_lines(path) == lines


@posix_only
def test_set_yolo_closes_the_audit_descriptor(tmp_path):
    """The MCP server is long-lived: one leaked descriptor per recorded toggle adds up."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    open_before = len(os.listdir("/dev/fd"))
    config.set_yolo(path, "dev", True, source="cli")
    assert len(os.listdir("/dev/fd")) == open_before
    assert len(_audit_lines(path)) == 1


def test_failed_config_write_records_nothing(tmp_path, monkeypatch):
    """A line is a claim that the change happened, so it is written only after the config."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    sidecar = tmp_path / "connections.json.audit.log"
    sidecar.touch()

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config, "write_text_atomically", failing_write)
    loaded = config.load(str(path))
    previous = loaded.model_copy(deep=True)
    config.get(loaded, "dev").yolo = True
    with pytest.raises(OSError):
        config.save(loaded, path, previous=previous, source="cli")
    assert sidecar.read_bytes() == b""


@posix_only
def test_unopenable_sidecar_refuses_the_change(tmp_path):
    """A change that cannot be recorded is not made: the config keeps its bytes."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    before = path.read_bytes()
    sidecar = tmp_path / "connections.json.audit.log"
    sidecar.touch()
    sidecar.chmod(0o444)
    loaded = config.load(str(path))
    previous = loaded.model_copy(deep=True)
    config.get(loaded, "dev").yolo = True
    try:
        with pytest.raises(PermissionError):
            config.save(loaded, path, previous=previous, source="cli")
    finally:
        sidecar.chmod(0o644)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".connections.json.*.tmp")) == []


def _yolo_flip(path: Path) -> tuple[Config, Config]:
    """The loaded config with dev's yolo turned on, and the config it replaces."""
    loaded = config.load(str(path))
    previous = loaded.model_copy(deep=True)
    config.get(loaded, "dev").yolo = True
    return loaded, previous


def _assert_linked_sidecar_refuses_the_change(tmp_path: Path, path: Path, target: Path) -> None:
    before, target_before = path.read_bytes(), target.read_bytes()
    loaded, previous = _yolo_flip(path)
    with pytest.raises(OSError):
        config.save(loaded, path, previous=previous, source="cli")
    assert target.read_bytes() == target_before
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".connections.json.*.tmp")) == []


@posix_only
def test_symlinked_sidecar_refuses_the_change(tmp_path):
    """A cloned repo can commit the sidecar as a link to a shell profile."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    target = tmp_path / "bashrc"
    target.write_text("export PATH=/usr/bin\n", encoding="utf-8")
    (tmp_path / "connections.json.audit.log").symlink_to(target)
    _assert_linked_sidecar_refuses_the_change(tmp_path, path, target)


@posix_only
def test_symlinked_sidecar_is_refused_where_open_cannot_refuse_links(tmp_path, monkeypatch):
    """Windows has no O_NOFOLLOW; the link check by path stands in for it there."""
    from db_conn_mcp import audit

    monkeypatch.setattr(audit, "_NO_FOLLOW", 0)
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    target = tmp_path / "bashrc"
    target.write_text("export PATH=/usr/bin\n", encoding="utf-8")
    (tmp_path / "connections.json.audit.log").symlink_to(target)
    _assert_linked_sidecar_refuses_the_change(tmp_path, path, target)


@posix_only
def test_hard_linked_sidecar_refuses_the_change(tmp_path):
    """A hard link shares the other file's inode: the same attack, with no link to see."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    target = tmp_path / "bashrc"
    target.write_text("export PATH=/usr/bin\n", encoding="utf-8")
    os.link(target, tmp_path / "connections.json.audit.log")
    _assert_linked_sidecar_refuses_the_change(tmp_path, path, target)


@posix_only
def test_a_sidecar_that_is_not_a_regular_file_refuses_the_change(tmp_path):
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    sidecar = tmp_path / "connections.json.audit.log"
    os.mkfifo(sidecar)
    # A reader keeps the sidecar's write-only open from waiting for one.
    reader = os.open(sidecar, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = path.read_bytes()
        loaded, previous = _yolo_flip(path)
        with pytest.raises(OSError, match="not a regular file"):
            config.save(loaded, path, previous=previous, source="cli")
        assert os.read(reader, 100) == b""  # the refused writer appended nothing
    finally:
        os.close(reader)
    assert path.read_bytes() == before


@posix_only
def test_a_refused_sidecar_closes_its_descriptor(tmp_path):
    """The refusal comes after the open, so it must close what it opened."""
    path = _write(tmp_path / "connections.json", YOLO_OFF)
    target = tmp_path / "bashrc"
    target.write_text("export PATH=/usr/bin\n", encoding="utf-8")
    os.link(target, tmp_path / "connections.json.audit.log")
    loaded, previous = _yolo_flip(path)
    open_before = len(os.listdir("/dev/fd"))
    with pytest.raises(OSError):
        config.save(loaded, path, previous=previous, source="cli")
    assert len(os.listdir("/dev/fd")) == open_before
