"""The doctor engine: findings shape, crash-guarding, and each check."""

import io
import json
import os
import subprocess
import sys
import urllib.request

import pytest

from db_conn_mcp import __version__, doctor
from db_conn_mcp import clients as clients_mod
from db_conn_mcp import doctor as doctor_mod
from db_conn_mcp.doctor import (
    CheckContext,
    check_client_paths,
    check_config_schema,
    check_connectivity,
    check_process_staleness,
    check_pypi_latest,
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


def _isolate_git(monkeypatch):
    """Ignore the developer's global/system git config so these tests are deterministic."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def test_secrets_git_not_ignored_fails(tmp_path, monkeypatch):
    _isolate_git(monkeypatch)
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    # On POSIX the new file is 0o644, which would emit a permission warn whose detail
    # embeds tmp_path — and pytest names that directory after the test, so it contains
    # "git". Lock the mode down and select the git finding by a phrase only it can have.
    path.chmod(0o600)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    git_finding = next(f for f in findings if "work tree" in f["detail"])
    assert git_finding["status"] == "fail"
    assert git_finding["suggested_action"] == "fix_config"


def test_secrets_git_ignored_ok(tmp_path, monkeypatch):
    _isolate_git(monkeypatch)
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("connections.json\n", encoding="utf-8")
    findings = check_secrets_exposure(CheckContext(config_path=path, offline=True))
    git_finding = next(f for f in findings if "work tree" in f["detail"])
    assert git_finding["status"] == "ok"


def test_secrets_outside_git_ok(tmp_path, monkeypatch):
    _isolate_git(monkeypatch)
    # A work tree ANYWHERE above the temp dir would otherwise flip this result.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    path = tmp_path / "connections.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
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


def _client_with_command(tmp_path, command):
    cfg = tmp_path / "client.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"db-conn-mcp": {"command": command, "args": []}}}),
        encoding="utf-8",
    )
    return clients_mod.ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")


def test_client_paths_warns_on_missing_binary(tmp_path, monkeypatch):
    spec = _client_with_command(tmp_path, str(tmp_path / "gone" / "db-conn-mcp.exe"))
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "warn"
    assert f["suggested_action"] == "repair_client_config"
    assert "Claude Desktop" in f["detail"]


def test_client_paths_ok_when_binary_exists(tmp_path, monkeypatch):
    exe = tmp_path / "db-conn-mcp.exe"
    exe.write_text("", encoding="utf-8")
    spec = _client_with_command(tmp_path, str(exe))
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "ok"


def test_client_paths_warns_when_the_entry_has_no_readable_command(tmp_path, monkeypatch):
    # Injected, but the entry is malformed — injected_command() returns None.
    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps({"mcpServers": {"db-conn-mcp": "not-a-dict"}}), encoding="utf-8")
    spec = clients_mod.ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "warn"
    assert f["suggested_action"] == "repair_client_config"


def test_client_paths_skips_a_detected_client_without_the_entry(tmp_path, monkeypatch):
    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    spec = clients_mod.ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "ok"
    assert "no detected MCP client" in f["detail"]


def test_client_paths_warns_on_an_unparseable_client_config(tmp_path, monkeypatch):
    """The one command whose job is diagnostics must not stay silent about a config
    that `clients` and `status` both flag. The detail names the client and path only —
    a traceback or an echoed body would leak whatever the file holds."""
    cfg = tmp_path / "client.json"
    cfg.write_text('{ "mcpServers": { "secret-token-abc123": ', encoding="utf-8")
    spec = clients_mod.ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "warn"
    assert f["suggested_action"] == "repair_client_config"
    assert "Claude Desktop" in f["detail"]
    assert "secret-token-abc123" not in f["detail"]


def test_client_paths_warns_on_a_non_mapping_client_config(tmp_path, monkeypatch):
    """Valid JSON with an array at the top level is unusable the same way."""
    cfg = tmp_path / "client.json"
    cfg.write_text('["secret-token-abc123"]', encoding="utf-8")
    spec = clients_mod.ClientSpec("claude", "Claude Desktop", cfg, "mcpServers")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "warn"
    assert "secret-token-abc123" not in f["detail"]


def test_client_paths_ok_when_nothing_injected(monkeypatch):
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [])
    (f,) = check_client_paths(CheckContext(config_path=None, offline=True))
    assert f["status"] == "ok"


def _fake_urlopen(payload):
    def opener(req, timeout=None):
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp(json.dumps(payload).encode("utf-8"))

    return opener


def test_pypi_offline_skips():
    (f,) = check_pypi_latest(CheckContext(config_path=None, offline=True))
    assert f["status"] == "skipped"


def test_pypi_newer_version_warns_with_upgrade_command(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen({"info": {"version": "99.0.0"}}))
    (f,) = check_pypi_latest(CheckContext(config_path=None, offline=False))
    assert f["status"] == "warn"
    assert "--no-cache-dir" in f["detail"]
    assert f["suggested_action"] == "upgrade_package"


def test_pypi_same_version_ok(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", _fake_urlopen({"info": {"version": __version__}})
    )
    (f,) = check_pypi_latest(CheckContext(config_path=None, offline=False))
    assert f["status"] == "ok"


def test_pypi_network_error_skips(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    (f,) = check_pypi_latest(CheckContext(config_path=None, offline=False))
    assert f["status"] == "skipped"


class _FakeProc:
    def __init__(self, pid, cmdline, create_time):
        self.info = {"pid": pid, "cmdline": cmdline, "create_time": create_time}


class _FakePsutil:
    """Just enough of psutil's surface for the check."""

    # Names mirror psutil's real API exactly — an "Error" suffix would not match
    # what the check catches, so N818 is suppressed rather than obeyed.
    class NoSuchProcess(Exception):  # noqa: N818
        pass

    class AccessDenied(Exception):  # noqa: N818
        pass

    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, attrs):
        return iter(self._procs)


def test_staleness_skipped_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    (f,) = check_process_staleness(CheckContext(config_path=None, offline=True))
    assert f["status"] == "skipped"
    assert "pipx inject" in f["detail"]


def test_staleness_flags_process_older_than_install(monkeypatch):
    stale = _FakeProc(111, ["python", "-m", "db_conn_mcp"], create_time=100.0)
    fresh = _FakeProc(222, ["db-conn-mcp", "--transport", "stdio"], create_time=9999.0)
    other = _FakeProc(333, ["notepad.exe"], create_time=50.0)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([stale, fresh, other]))
    monkeypatch.setattr(doctor_mod, "_installed_at", lambda: 1000.0)
    findings = check_process_staleness(CheckContext(config_path=None, offline=True))
    warns = [f for f in findings if f["status"] == "warn"]
    assert len(warns) == 1
    assert "111" in warns[0]["detail"]
    assert warns[0]["suggested_action"] == "reconnect_client"


def test_staleness_ok_when_all_processes_fresh(monkeypatch):
    fresh = _FakeProc(222, ["db-conn-mcp"], create_time=9999.0)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([fresh]))
    monkeypatch.setattr(doctor_mod, "_installed_at", lambda: 1000.0)
    (f,) = check_process_staleness(CheckContext(config_path=None, offline=True))
    assert f["status"] == "ok"


def test_staleness_flags_the_doctors_own_stale_process(monkeypatch):
    """Doctor running AS the stale server must report itself, not stay silent."""
    own = _FakeProc(os.getpid(), ["db-conn-mcp", "--transport", "stdio"], create_time=100.0)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([own]))
    monkeypatch.setattr(doctor_mod, "_installed_at", lambda: 1000.0)
    findings = check_process_staleness(CheckContext(config_path=None, offline=True))
    warns = [f for f in findings if f["status"] == "warn"]
    assert len(warns) == 1
    assert "itself" in warns[0]["detail"]
    assert str(os.getpid()) in warns[0]["detail"]
    assert warns[0]["suggested_action"] == "reconnect_client"


def test_staleness_ok_when_own_process_is_fresh(monkeypatch):
    own = _FakeProc(os.getpid(), ["db-conn-mcp", "--transport", "stdio"], create_time=9999.0)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([own]))
    monkeypatch.setattr(doctor_mod, "_installed_at", lambda: 1000.0)
    (f,) = check_process_staleness(CheckContext(config_path=None, offline=True))
    assert f["status"] == "ok"


def test_staleness_skipped_when_install_time_unknown(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil([]))
    monkeypatch.setattr(doctor_mod, "_installed_at", lambda: None)
    (f,) = check_process_staleness(CheckContext(config_path=None, offline=True))
    assert f["status"] == "skipped"


def _write_cfg(tmp_path, fallback_ports=None):
    conn = {"name": "db", "dsn": "postgresql://u:SEKRETPW@sekrethost:5432/d", "mode": "read"}
    if fallback_ports:
        conn["fallback_ports"] = fallback_ports
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [conn]}), encoding="utf-8")
    return path


async def test_connectivity_skipped_without_config():
    (f,) = await check_connectivity(CheckContext(config_path=None, offline=True))
    assert f["status"] == "skipped"


async def test_connectivity_auth_failed_with_fallbacks_probes_port_identity(tmp_path, monkeypatch):
    path = _write_cfg(tmp_path, fallback_ports=[5433])

    async def fake_check_database(self, database=None):
        return [
            {
                "database": "db",
                "status": "UNREACHABLE",
                "category": "AUTH_FAILED",
                "detail": "Authentication failed.",
            }
        ]

    class _ProbeDialect:
        async def probe_listener(self, host, port):
            return port == 5433

    from db_conn_mcp.handlers import Handlers

    monkeypatch.setattr(Handlers, "check_database", fake_check_database)
    monkeypatch.setattr(doctor_mod, "dialect_for", lambda dsn: _ProbeDialect())
    findings = await check_connectivity(CheckContext(config_path=path, offline=True))
    identity = next(f for f in findings if f["check"] == "port_identity")
    assert identity["status"] == "warn"
    assert "5433" in identity["detail"]
    assert identity["suggested_action"] == "swap_primary_port"
    # Rule 6: the host from the DSN never appears.
    assert all("sekrethost" not in f["detail"] for f in findings)


async def test_connectivity_ok_reports_ok(tmp_path, monkeypatch):
    path = _write_cfg(tmp_path)

    async def fake_check_database(self, database=None):
        return [{"database": "db", "status": "OK"}]

    from db_conn_mcp.handlers import Handlers

    monkeypatch.setattr(Handlers, "check_database", fake_check_database)
    (f,) = await check_connectivity(CheckContext(config_path=path, offline=True))
    assert f["status"] == "ok"
    assert "db" in f["detail"]


async def test_connectivity_does_not_probe_identity_when_a_fallback_rejected_auth(
    tmp_path, monkeypatch
):
    """A fallback that rejected credentials already speaks the protocol.

    Telling the agent to swap the primary port to it would be a false statement and
    a no-op fix, so the port-identity probe must not run at all.
    """
    path = _write_cfg(tmp_path, fallback_ports=[5433])

    async def fake_check_database(self, database=None):
        return [
            {
                "database": "db",
                "status": "UNREACHABLE",
                "category": "AUTH_FAILED",
                "detail": "Authentication failed. (on fallback port 5433)",
                "failed_port": 5433,
            }
        ]

    class _NeverProbe:
        async def probe_listener(self, host, port):
            raise AssertionError("must not probe when auth failed on a fallback port")

    from db_conn_mcp.handlers import Handlers

    monkeypatch.setattr(Handlers, "check_database", fake_check_database)
    monkeypatch.setattr(doctor_mod, "dialect_for", lambda dsn: _NeverProbe())
    findings = await check_connectivity(CheckContext(config_path=path, offline=True))
    assert [f["check"] for f in findings] == ["connectivity"]
    assert findings[0]["status"] == "fail"


def test_registry_order_and_membership():
    assert [name for name, _ in doctor._CHECKS] == [
        "process_staleness",
        "pypi_latest",
        "config_schema",
        "secrets_exposure",
        "client_paths",
        "connectivity",
    ]


_POISON = ("sekretuser", "sekretpass", "sekrethost", "sekretdb")
_POISONED_DSN = "postgresql://sekretuser:sekretpass@sekrethost.invalid:5432/sekretdb"


async def _assert_no_leak(tmp_path, conn):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [conn]}), encoding="utf-8")
    findings = await run_checks(path, offline=True)
    blob = json.dumps(findings)
    for secret in _POISON:
        assert secret not in blob, f"finding leaked {secret!r}"


async def test_no_finding_ever_leaks_dsn_material(tmp_path):
    """Poisoned DSN substrings must never appear in any finding (Rule 6)."""
    await _assert_no_leak(
        tmp_path,
        {"name": "db", "dsn": _POISONED_DSN, "mode": "read", "fallback_ports": [5433]},
    )


async def test_no_finding_leaks_dsn_material_from_invalid_field_values(tmp_path):
    """The pydantic-error path must stay value-free even when the VALUES are secrets.

    Here the invalid `mode` is the DSN itself and `yolo` is the password, so any future
    validator that interpolates the offending input would leak straight into a finding.
    """
    await _assert_no_leak(
        tmp_path,
        {"name": "db", "dsn": _POISONED_DSN, "mode": _POISONED_DSN, "yolo": "sekretpass"},
    )
