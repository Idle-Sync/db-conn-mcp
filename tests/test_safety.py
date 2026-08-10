"""The write-gate truth table: mode (hard) -> yolo -> user_consent."""

import pytest

from db_conn_mcp.models import Connection
from db_conn_mcp.safety import (
    WriteRejected,
    authorize_commit,
    authorize_dry_run,
    authorize_write,
)


def _conn(mode, yolo=False):
    return Connection(name="db", dsn="postgresql://h/db", mode=mode, yolo=yolo)


# ---- Step 1: mode is the hard, unbypassable boundary -------------------------


def test_read_mode_always_rejected_even_with_consent():
    with pytest.raises(WriteRejected, match="read"):
        authorize_write(_conn("read"), user_consent=True)


def test_read_mode_rejected_even_with_yolo():
    # yolo can NEVER make a read DB writable.
    with pytest.raises(WriteRejected):
        authorize_write(_conn("read", yolo=True), user_consent=False)


# ---- Step 2: yolo allows on a write DB ---------------------------------------


def test_write_yolo_allows_without_consent():
    assert authorize_write(_conn("write", yolo=True), user_consent=False) is None


# ---- Step 3: consent allows on a write DB ------------------------------------


def test_write_consent_allows():
    assert authorize_write(_conn("write"), user_consent=True) is None


# ---- Step 4: write DB, no yolo, no consent -> reject with instruction --------


def test_write_no_yolo_no_consent_rejected_with_instruction():
    with pytest.raises(WriteRejected, match="user_consent"):
        authorize_write(_conn("write"), user_consent=False)


# ---- Dry-run: mode gate only (nothing commits, so no consent needed) ----------


def test_dry_run_allowed_on_write_db_without_consent_or_yolo():
    assert authorize_dry_run(_conn("write")) is None


def test_dry_run_rejected_on_read_db():
    # A dry-run still EXECUTES server-side before rolling back; the hard mode
    # boundary is never waived.
    with pytest.raises(WriteRejected, match="dry-run"):
        authorize_dry_run(_conn("read"))


def test_dry_run_rejected_on_read_db_even_with_yolo():
    with pytest.raises(WriteRejected):
        authorize_dry_run(_conn("read", yolo=True))


# ---- Commit gate: mode -> dry-run-first -> yolo -> consent --------------------


def test_commit_without_grant_or_skip_rejected_with_dry_run_instruction():
    with pytest.raises(WriteRejected, match="dry_run"):
        authorize_commit(_conn("write"), user_consent=True, has_grant=False, skip_dry_run=False)


def test_commit_with_grant_and_consent_allowed():
    assert (
        authorize_commit(_conn("write"), user_consent=True, has_grant=True, skip_dry_run=False)
        is None
    )


def test_commit_with_grant_but_no_consent_still_needs_consent():
    # A dry-run grant satisfies ONLY the preview stage — never consent.
    with pytest.raises(WriteRejected, match="user_consent"):
        authorize_commit(_conn("write"), user_consent=False, has_grant=True, skip_dry_run=False)


def test_skip_dry_run_with_consent_allowed_without_grant():
    assert (
        authorize_commit(_conn("write"), user_consent=True, has_grant=False, skip_dry_run=True)
        is None
    )


def test_yolo_cannot_skip_the_dry_run_stage():
    # THE core requirement: even write+yolo must dry-run first.
    with pytest.raises(WriteRejected, match="dry_run"):
        authorize_commit(
            _conn("write", yolo=True), user_consent=False, has_grant=False, skip_dry_run=False
        )


def test_yolo_with_grant_allowed_without_consent():
    assert (
        authorize_commit(
            _conn("write", yolo=True), user_consent=False, has_grant=True, skip_dry_run=False
        )
        is None
    )


def test_commit_read_mode_rejected_even_with_grant_skip_and_consent():
    with pytest.raises(WriteRejected, match="read"):
        authorize_commit(_conn("read"), user_consent=True, has_grant=True, skip_dry_run=True)
