"""The write-gate truth table: mode (hard) -> yolo -> user_consent."""

import pytest

from db_conn_mcp.models import Connection
from db_conn_mcp.safety import WriteRejected, authorize_write


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
