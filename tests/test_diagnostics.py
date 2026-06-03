"""The doctor: driver exceptions -> sanitized {category, cause, fixes}.

Critically: output must NEVER contain a host, user, or password substring.
"""

import asyncpg
import pytest

from db_conn_mcp.diagnostics import explain

# Distinctive credential *values* that would only appear if the original exception
# message were echoed. (Generic English words like "user" are intentionally excluded —
# the canned advice legitimately says "username".)
SECRET_DSN_BITS = ["SUPERSECRET", "dbadmin", "myhost.example.com", "5432"]


def _make(exc_type, message="boom"):
    """Build a bare asyncpg PostgresError of the given class with a message."""
    return exc_type(message)


def test_invalid_password_is_auth_failed():
    result = explain(asyncpg.InvalidPasswordError("password authentication failed"))
    assert result["category"] == "AUTH_FAILED"


def test_invalid_catalog_is_db_not_found():
    result = explain(asyncpg.InvalidCatalogNameError('database "x" does not exist'))
    assert result["category"] == "DB_NOT_FOUND"


def test_connection_refused_is_host_unreachable():
    result = explain(ConnectionRefusedError("[Errno 111] Connection refused"))
    assert result["category"] == "HOST_UNREACHABLE"


def test_timeout_is_host_unreachable():
    result = explain(TimeoutError("timed out"))
    assert result["category"] == "HOST_UNREACHABLE"


def test_dns_failure_classified():
    result = explain(OSError("[Errno -2] Name or service not known"))
    assert result["category"] == "DNS_FAILURE"


def test_unknown_falls_through():
    result = explain(RuntimeError("something weird"))
    assert result["category"] == "UNKNOWN"
    # Unknown still points the agent at the full checklist.
    blob = (result["cause"] + " " + " ".join(result["fixes"])).lower()
    assert "troubleshoot_connection" in blob


def test_shape_is_category_cause_fixes():
    result = explain(RuntimeError("x"))
    assert set(result) == {"category", "cause", "fixes"}
    assert isinstance(result["fixes"], list)


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.InvalidPasswordError("auth failed for 'dbadmin' at myhost.example.com:5432"),
        ConnectionRefusedError("refused connecting to myhost.example.com:5432 as dbadmin"),
        OSError("could not translate host name myhost.example.com to address"),
    ],
)
def test_never_leaks_connection_details(exc):
    result = explain(exc)
    blob = (result["cause"] + " " + " ".join(result["fixes"])).lower()
    for secret in SECRET_DSN_BITS:
        assert secret.lower() not in blob, f"{secret!r} leaked into diagnostic"
