"""Tests for the audit sidecar: what a change is recorded as, and how the line is written."""

import json
import os
import re
from pathlib import Path

import pytest

from db_conn_mcp import audit, config
from db_conn_mcp.models import Config, Connection

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits only")

_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _conn(name: str, **fields) -> Connection:
    fields.setdefault("dsn", "postgresql://h/d")
    fields.setdefault("mode", "read")
    return Connection(name=name, **fields)


# ---- changes_between ------------------------------------------------------------


def test_an_added_connection_lists_all_three_fields_and_no_dsn():
    updated = Config(connections=[_conn("a", mode="write", dsn="postgresql://u:SECRET@h/d")])
    assert audit.changes_between(Config(), updated) == [
        ("add", "a", 'mode: "write", yolo: false, fallback_ports: null')
    ]


def test_an_edit_of_mode_names_old_and_new():
    previous = Config(connections=[_conn("a", mode="write")])
    updated = Config(connections=[_conn("a", mode="read")])
    assert audit.changes_between(previous, updated) == [("edit", "a", 'mode: "write" -> "read"')]


def test_an_edit_of_yolo_names_old_and_new():
    previous = Config(connections=[_conn("a")])
    updated = Config(connections=[_conn("a", yolo=True)])
    assert audit.changes_between(previous, updated) == [("edit", "a", "yolo: false -> true")]


def test_an_edit_of_fallback_ports_from_none_to_a_list():
    previous = Config(connections=[_conn("a")])
    updated = Config(connections=[_conn("a", fallback_ports=[5433])])
    assert audit.changes_between(previous, updated) == [
        ("edit", "a", "fallback_ports: null -> [5433]")
    ]


def test_an_edit_of_two_fields_lists_them_in_the_pinned_order():
    previous = Config(connections=[_conn("a", mode="write", dsn="postgresql://h/old")])
    updated = Config(connections=[_conn("a", mode="read", dsn="postgresql://h/new", yolo=True)])
    assert audit.changes_between(previous, updated) == [
        ("edit", "a", 'mode: "write" -> "read", yolo: false -> true, dsn: (replaced)')
    ]


def test_a_dsn_change_is_only_named_as_replaced():
    previous = Config(connections=[_conn("a", dsn="postgresql://u:SECRET@h/db")])
    updated = Config(connections=[_conn("a", dsn="postgresql://u:OTHER@h/db")])
    assert audit.changes_between(previous, updated) == [("edit", "a", "dsn: (replaced)")]


def test_a_removed_connection_carries_no_changes():
    previous = Config(connections=[_conn("a"), _conn("b")])
    updated = Config(connections=[_conn("b")])
    assert audit.changes_between(previous, updated) == [("remove", "a", "")]


def test_removals_come_first_then_edits_and_adds_in_the_new_order():
    previous = Config(connections=[_conn("x"), _conn("a"), _conn("y")])
    updated = Config(connections=[_conn("new"), _conn("a", yolo=True)])
    assert [(action, name) for action, name, _ in audit.changes_between(previous, updated)] == [
        ("remove", "x"),
        ("remove", "y"),
        ("add", "new"),
        ("edit", "a"),
    ]


def test_a_reorder_is_no_change():
    previous = Config(connections=[_conn("a"), _conn("b")])
    updated = Config(connections=[_conn("b"), _conn("a")])
    assert audit.changes_between(previous, updated) == []


def test_identical_configs_are_no_change():
    previous = Config(connections=[_conn("a", fallback_ports=[5433])])
    assert audit.changes_between(previous, previous.model_copy(deep=True)) == []


def test_a_difference_only_in_an_unknown_key_is_no_change():
    """Unknown keys can hold anything, secrets included, so they are never compared."""
    previous = Config(connections=[_conn("a", comment="old")])
    updated = Config(connections=[_conn("a", comment="new")])
    assert audit.changes_between(previous, updated) == []


# ---- line -----------------------------------------------------------------------


def test_an_edit_line_has_five_tab_separated_columns():
    rendered = audit.line("gui", "edit", "prod", 'mode: "write" -> "read"')
    assert re.fullmatch(_TIMESTAMP + '\tgui\tedit\t"prod"\tmode: "write" -> "read"\n', rendered)


def test_a_remove_line_has_four_columns_and_no_trailing_tab():
    assert re.fullmatch(_TIMESTAMP + '\tcli\tremove\t"a"\n', audit.line("cli", "remove", "a"))


def test_a_reset_line_names_no_connection():
    assert re.fullmatch(_TIMESTAMP + "\tcli\treset\t-\n", audit.line("cli", "reset", None))


@pytest.mark.parametrize(
    "name",
    ["postgresql://u:SECRET@h/db", "u@h", "password=SECRET host=h"],
    ids=["url", "at-sign", "key-value"],
)
def test_a_dsn_shaped_name_is_withheld(name):
    rendered = audit.line("cli", "add", name, "mode: ...")
    assert rendered.split("\t")[3] == "(withheld)"
    assert "SECRET" not in rendered
    assert "@h" not in rendered


# ---- appending ------------------------------------------------------------------


def test_appending_with_no_lines_creates_no_file(tmp_path):
    sidecar = tmp_path / "connections.json.audit.log"
    with audit.appending(sidecar, []):
        pass
    assert not sidecar.exists()


def test_appending_adds_to_what_is_already_there(tmp_path):
    sidecar = tmp_path / "connections.json.audit.log"
    with audit.appending(sidecar, ["one\n"]):
        pass
    with audit.appending(sidecar, ["two\n", "three\n"]):
        pass
    assert sidecar.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_appending_writes_nothing_when_the_body_fails(tmp_path):
    sidecar = tmp_path / "connections.json.audit.log"
    with pytest.raises(OSError), audit.appending(sidecar, ["one\n"]):
        raise OSError("the config write failed")
    assert sidecar.read_bytes() == b""


@posix_only
def test_appending_creates_the_sidecar_user_only(tmp_path):
    """Names and modes of every database are nobody else's business."""
    sidecar = tmp_path / "connections.json.audit.log"
    with audit.appending(sidecar, ["one\n"]):
        pass
    assert _mode(sidecar) == 0o600


def test_audit_path_sits_beside_the_config():
    path = Path("/some/dir/connections.json")
    assert audit.audit_path(path) == path.resolve().with_name("connections.json.audit.log")


@posix_only
def test_appending_puts_the_sidecar_beside_the_symlink_target(tmp_path):
    """Two spellings of one config (link and real path) must share one history."""
    real = _write(tmp_path / "real" / "connections.json", {"connections": []})
    link_dir = tmp_path / "link_dir"
    link_dir.mkdir()
    link = link_dir / "connections.json"
    link.symlink_to(real)

    with audit.appending(audit.audit_path(link), ["one\n"]):
        pass

    assert (tmp_path / "real" / "connections.json.audit.log").read_text(encoding="utf-8") == "one\n"
    assert [p.name for p in link_dir.iterdir()] == ["connections.json"]


def test_a_name_with_a_newline_and_a_tab_stays_one_line(tmp_path):
    """One mutation is one line, whatever the name holds: a newline must not forge a second."""
    sidecar = tmp_path / "connections.json.audit.log"
    name = "evil\n2026-01-01T00:00:00+00:00\tcli\treset\t-\tx"
    with audit.appending(sidecar, [audit.line("cli", "remove", name)]):
        pass
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].split("\t")[3] == json.dumps(name)
    assert b"\r" not in sidecar.read_bytes()


# ---- Rule 6 at the seam ---------------------------------------------------------


def test_no_secret_leaks_into_the_sidecar(tmp_path):
    path = tmp_path / "connections.json"
    previous = Config(connections=[_conn("db", dsn="postgresql://u:SECRET@h/db")])
    updated = Config(connections=[_conn("db", dsn="postgresql://u:OTHER@h/db")])
    config.save(updated, path, previous=previous, source="cli")
    sidecar = audit.audit_path(path)
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].endswith('\tcli\tedit\t"db"\tdsn: (replaced)')

    withheld = [
        audit.line("cli", "add", "postgresql://u:SECRET@h/db", "mode: x"),
        audit.line("cli", "add", "u@h", "mode: x"),
        audit.line("cli", "add", "password=SECRET host=h", "mode: x"),
    ]
    with audit.appending(sidecar, withheld):
        pass
    data = sidecar.read_bytes()
    assert data.count(b"(withheld)") == 3
    for secret in (b"SECRET", b"OTHER", b"@h"):
        assert secret not in data
