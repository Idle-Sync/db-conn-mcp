"""The doctor engine: findings shape, crash-guarding, and each check."""

from db_conn_mcp import doctor
from db_conn_mcp.doctor import finding, run_checks


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
