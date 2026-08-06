"""The doctor engine: findings shape, crash-guarding, and each check."""

import json
import os
import subprocess

import pytest

from db_conn_mcp import doctor
from db_conn_mcp.doctor import (
    CheckContext,
    check_config_schema,
    check_secrets_exposure,
    finding,
    run_checks,
)


def test_finding_builder_defaults_action_to_none():
    f = finding("x", "ok", "fine")
    assert f == {"check": "x", "status": "ok", "detail": "fine", "suggested_action": "none"}


async def test_run_checks_never_raises_a_crashing_check(monkeypatch):
    def _boom(ctx):
        raise RuntimeError("secret-host-would-be-here")

    monkeypatch.setattr(doctor, "_CHECKS", [("exploding", _boom)])
    findings = await run_checks(None, offline=True)
    assert len(findings) == 1
    assert findings[0]["check"] == "exploding"
    assert findings[0]["status"] == "fail"
    # Only the exception TYPE is reported — never its message (may embed a host).
    assert "RuntimeError" in findings[0]["detail"]
    assert "secret-host" not in findings[0]["detail"]


async def test_run_checks_awaits_an_async_check(monkeypatch):
    async def _ok(ctx):
        return [finding("async-check", "ok", "ran")]

    monkeypatch.setattr(doctor, "_CHECKS", [("async-check", _ok)])
    assert await run_checks(None, offline=True) == [
        {"check": "async-check", "status": "ok", "detail": "ran", "suggested_action": "none"}
    ]


def _ctx(tmp_path, text):
    path = tmp_path / "connections.json"
    path.write_text(text, encoding="utf-8")
    return CheckContext(config_path=path, offline=True)


def test_config_schema_skipped_without_config():
    (f,) = check_config_schema(CheckContext(config_path=None, offline=True))
    assert f["status"] == "skipped"


def test_config_schema_flags_typoed_key_with_did_you_mean(tmp_path):
    cfg = {
        "connections": [
            {
                "name": "db",
                "dsn": "postgresql://u:p@h/d",
                "mode": "read",
                "fallback_port": [5433],
            }
        ]
    }
    findings = check_config_schema(_ctx(tmp_path, json.dumps(cfg)))
    warn = next(f for f in findings if f["status"] == "warn")
    assert "fallback_port" in warn["detail"] and "fallback_ports" in warn["detail"]
    assert warn["suggested_action"] == "fix_config"


def test_config_schema_flags_unknown_key_without_a_bogus_did_you_mean(tmp_path):
    cfg = {"connections": [], "zzzz": 1}
    findings = check_config_schema(_ctx(tmp_path, json.dumps(cfg)))
    warn = next(f for f in findings if f["status"] == "warn")
    assert "zzzz" in warn["detail"]
    # Nothing in the schema is close to "zzzz" — don't invent a suggestion.
    assert "did you mean" not in warn["detail"]


def test_config_schema_reports_bad_type_without_echoing_values(tmp_path):
    cfg = {"connections": [{"name": "db", "dsn": "postgresql://u:SEKRET@h/d", "mode": "banana"}]}
    findings = check_config_schema(_ctx(tmp_path, json.dumps(cfg)))
    fail = next(f for f in findings if f["status"] == "fail")
    assert "mode" in fail["detail"]
    assert "SEKRET" not in fail["detail"]
    # A leak in ANY finding is a Rule 6 violation, not just the one we inspected.
    assert not any("SEKRET" in f["detail"] for f in findings)


def test_config_schema_names_the_document_when_the_whole_file_is_wrong_type(tmp_path):
    (f,) = check_config_schema(_ctx(tmp_path, json.dumps([1, 2])))
    assert f["status"] == "fail"
    assert "the top-level document" in f["detail"]


def test_config_schema_ok_on_valid_config(tmp_path):
    cfg = {"connections": [{"name": "db", "dsn": "postgresql://u:p@h/d", "mode": "read"}]}
    (f,) = check_config_schema(_ctx(tmp_path, json.dumps(cfg)))
    assert f["status"] == "ok"


def test_config_schema_fails_on_invalid_json(tmp_path):
    (f,) = check_config_schema(_ctx(tmp_path, "{not json"))
    assert f["status"] == "fail"
    assert f["suggested_action"] == "fix_config"


def test_config_schema_fails_on_non_utf8_config(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError — it must be diagnosed
    # here, not escape as a generic "check crashed" finding.
    path = tmp_path / "connections.json"
    path.write_bytes(b'{"connections": [\xc9]}')
    (f,) = check_config_schema(CheckContext(config_path=path, offline=True))
    assert f["status"] == "fail"
    assert f["suggested_action"] == "fix_config"
    assert "\\xc9" not in f["detail"] and "É" not in f["detail"]


def test_secrets_git_not_ignored_fails(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    git_finding = next(f for f in findings if "git" in f["detail"].lower())
    assert git_finding["status"] == "fail"
    assert git_finding["suggested_action"] == "fix_config"


def test_secrets_git_ignored_ok(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("connections.json\n", encoding="utf-8")
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    git_finding = next(f for f in findings if "git" in f["detail"].lower())
    assert git_finding["status"] == "ok"


def test_secrets_outside_git_ok(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    assert all(f["status"] in ("ok", "skipped") for f in findings)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_secrets_world_readable_warns(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    perm = next(f for f in findings if "chmod" in f["detail"])
    assert perm["status"] == "warn"
    assert perm["suggested_action"] == "fix_permissions"
