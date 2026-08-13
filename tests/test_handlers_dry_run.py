"""Dry-run-first enforcement in Handlers: grants recorded, required, consumed."""

import json

import pytest

from db_conn_mcp import handlers as handlers_mod
from db_conn_mcp.handlers import DRY_RUN_GRANT_TTL_SECONDS, Handlers
from db_conn_mcp.safety import WriteRejected


class _FakeDb:
    async def close(self):
        pass


class _FakeDialect:
    """Records committed SQL; never touches a real database."""

    def __init__(self):
        self.committed: list[str] = []
        self.dry_runs: list[str] = []
        self.raise_on_execute: Exception | None = None

    async def connect(self, dsn, *, read_only):
        return _FakeDb()

    async def execute(self, db, sql, params=None, timeout_ms=None):
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.committed.append(sql)
        return {"rows_affected": 1}

    async def execute_dry_run(self, db, sql, params=None, timeout_ms=None):
        self.dry_runs.append(sql)
        return {"rows_affected": 1, "dry_run": True, "rolled_back": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {
        "connections": [
            {"name": "db", "dsn": "postgresql://u:p@h:5432/db", "mode": "write"},
            {"name": "ydb", "dsn": "postgresql://u:p@h:5433/db", "mode": "write", "yolo": True},
        ]
    }
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    fake = _FakeDialect()
    monkeypatch.setattr(handlers_mod, "dialect_for", lambda dsn: fake)
    return Handlers(path), fake


async def test_bare_call_dry_runs_and_commits_nothing(env):
    h, fake = env
    result = await h.execute_write_query("db", "DELETE FROM t")
    assert result["rolled_back"] is True
    assert fake.committed == []
    assert fake.dry_runs == ["DELETE FROM t"]


async def test_commit_without_prior_dry_run_rejected(env):
    h, fake = env
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query("db", "DELETE FROM t", user_consent=True, dry_run=False)
    assert fake.committed == []


async def test_commit_after_dry_run_succeeds_and_consumes_grant(env):
    h, fake = env
    await h.execute_write_query("db", "DELETE FROM t")
    result = await h.execute_write_query("db", "DELETE FROM t", user_consent=True, dry_run=False)
    assert result == {"rows_affected": 1}
    assert fake.committed == ["DELETE FROM t"]
    # Grant consumed: an identical second commit needs a fresh dry-run.
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query("db", "DELETE FROM t", user_consent=True, dry_run=False)


async def test_failed_commit_also_consumes_the_grant(env):
    """A raising commit may still have applied server-side — no blind retry on a live grant."""
    h, fake = env
    sql = "UPDATE t SET n = n + 1"
    await h.execute_write_query("db", sql)
    fake.raise_on_execute = RuntimeError("connection dropped during COMMIT")
    with pytest.raises(RuntimeError):
        await h.execute_write_query("db", sql, user_consent=True, dry_run=False)

    # The attempt consumed the grant: retrying the identical statement is rejected.
    fake.raise_on_execute = None
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query("db", sql, user_consent=True, dry_run=False)
    assert fake.committed == []

    # A fresh preview re-authorizes exactly one more attempt.
    await h.execute_write_query("db", sql)
    assert await h.execute_write_query("db", sql, user_consent=True, dry_run=False) == {
        "rows_affected": 1
    }
    assert fake.committed == [sql]


async def test_changed_sql_or_params_does_not_match_grant(env):
    h, fake = env
    await h.execute_write_query("db", "DELETE FROM t WHERE id = $1", params=[1])
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query(
            "db", "DELETE FROM t WHERE id = $1", params=[2], user_consent=True, dry_run=False
        )


async def test_expired_grant_rejected(env, monkeypatch):
    h, fake = env
    await h.execute_write_query("db", "DELETE FROM t")
    key = next(iter(h._dry_run_grants))
    h._dry_run_grants[key] -= DRY_RUN_GRANT_TTL_SECONDS + 1
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query("db", "DELETE FROM t", user_consent=True, dry_run=False)


async def test_skip_dry_run_with_consent_commits_directly(env):
    h, fake = env
    result = await h.execute_write_query(
        "db", "DELETE FROM t", user_consent=True, dry_run=False, skip_dry_run=True
    )
    assert result == {"rows_affected": 1}
    assert fake.committed == ["DELETE FROM t"]


async def test_yolo_db_still_requires_dry_run_before_commit(env):
    h, fake = env
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query("ydb", "DELETE FROM t", dry_run=False)
    await h.execute_write_query("ydb", "DELETE FROM t")  # dry-run records grant
    result = await h.execute_write_query("ydb", "DELETE FROM t", dry_run=False)
    assert result == {"rows_affected": 1}  # yolo waives consent, not the preview


class _Session:
    """A stand-in for an MCP session object (a weak-referenceable instance)."""


async def test_grant_is_scoped_to_the_session(env):
    """A dry-run in one session cannot authorize a commit from a DIFFERENT session.

    Under `--transport http` a single Handlers instance is shared by every connected
    client, so an unscoped grant let session A's preview satisfy session B's commit of
    the identical statement. The grant must be keyed to the session that previewed.
    """
    h, fake = env
    a, b = _Session(), _Session()

    # Session A previews the statement — records a grant scoped to A.
    await h.execute_write_query("db", "DELETE FROM t", session=a)

    # Session B never previewed: committing the identical (db, sql, params) is rejected.
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query(
            "db", "DELETE FROM t", user_consent=True, dry_run=False, session=b
        )
    assert fake.committed == []

    # Session A — the one that previewed — commits and consumes its own grant.
    result = await h.execute_write_query(
        "db", "DELETE FROM t", user_consent=True, dry_run=False, session=a
    )
    assert result == {"rows_affected": 1}
    assert fake.committed == ["DELETE FROM t"]

    # The grant was single-use: A cannot commit again without a fresh preview.
    with pytest.raises(WriteRejected, match="dry_run"):
        await h.execute_write_query(
            "db", "DELETE FROM t", user_consent=True, dry_run=False, session=a
        )


async def test_stdio_none_session_still_shares_a_grant(env):
    """Under stdio there is one session per process — session=None preserves today's
    behavior: a dry-run then a commit (both session=None) works end to end."""
    h, fake = env
    await h.execute_write_query("db", "DELETE FROM t", session=None)
    result = await h.execute_write_query(
        "db", "DELETE FROM t", user_consent=True, dry_run=False, session=None
    )
    assert result == {"rows_affected": 1}
    assert fake.committed == ["DELETE FROM t"]
