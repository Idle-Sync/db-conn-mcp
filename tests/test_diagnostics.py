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


def test_missing_database_named_like_role_is_db_not_found():
    result = explain(RuntimeError('FATAL: database "user_roles_db" does not exist'))
    assert result["category"] == "DB_NOT_FOUND"


def test_missing_role_named_like_database_is_auth_failed():
    # Rejects a plain reorder of the AUTH and DB branches.
    result = explain(RuntimeError('FATAL: role "database_admin" does not exist'))
    assert result["category"] == "AUTH_FAILED"


@pytest.mark.parametrize(
    "message",
    [
        'FATAL: role "reporting" does not exist',
        'FATAL: password authentication failed for user "x"',
    ],
)
def test_untyped_auth_messages_are_auth_failed(message):
    assert explain(RuntimeError(message))["category"] == "AUTH_FAILED"


def test_untyped_missing_database_is_db_not_found():
    result = explain(RuntimeError('FATAL: database "payroll" does not exist'))
    assert result["category"] == "DB_NOT_FOUND"


def test_missing_database_named_like_ssl_is_db_not_found():
    result = explain(RuntimeError('FATAL: database "ssl_audit" does not exist'))
    assert result["category"] == "DB_NOT_FOUND"


@pytest.mark.parametrize(
    "message",
    [
        'FATAL: no pg_hba.conf entry for host "10.0.0.5", user "app", database "payroll", SSL off',
        "FATAL: SSL required",
        "connection is insecure (try using `sslmode=require`)",
        "SSL connection is required for user: app",
        "SSL is required",
    ],
)
def test_ssl_markers_match_server_phrasing(message):
    assert explain(RuntimeError(message))["category"] == "SSL_REQUIRED"


@pytest.mark.parametrize(
    "message",
    [
        'FATAL: no pg_hba.conf entry for host "10.0.0.5", user "app", database "payroll", '
        "SSL encryption",
        'PostgreSQL server at "h:5432" rejected SSL upgrade',
        "FATAL: ssl requested but unavailable",
    ],
)
def test_ssl_messages_not_about_requiring_ssl_are_unknown(message):
    assert explain(RuntimeError(message))["category"] == "UNKNOWN"


def test_too_many_open_files_is_not_pool_exhausted():
    result = explain(OSError("[Errno 24] Too many open files"))
    assert result["category"] == "UNKNOWN"


@pytest.mark.parametrize(
    "message",
    [
        'FATAL: too many connections for role "app"',
        "FATAL: sorry, too many clients already",
        "FATAL: remaining connection slots are reserved for roles with privileges of the "
        '"pg_use_reserved_connections" role',
    ],
)
def test_pool_markers_match_server_phrasing(message):
    assert explain(RuntimeError(message))["category"] == "POOL_EXHAUSTED"


def test_statement_timeout_is_not_host_unreachable():
    result = explain(RuntimeError("FATAL: canceling statement due to statement timeout"))
    assert result["category"] == "UNKNOWN"


@pytest.mark.parametrize(
    "message",
    [
        "[Errno 110] Connection timed out",
        "FATAL: client_login_timeout (server down)",
        "[Errno 111] Connection refused",
    ],
)
def test_timeout_markers_match_server_phrasing(message):
    assert explain(RuntimeError(message))["category"] == "HOST_UNREACHABLE"


def test_query_wait_timeout_is_unknown():
    result = explain(RuntimeError("FATAL: query_wait_timeout"))
    assert result["category"] == "UNKNOWN"


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.InvalidPasswordError("auth failed for 'dbadmin' at myhost.example.com:5432"),
        ConnectionRefusedError("refused connecting to myhost.example.com:5432 as dbadmin"),
        OSError("could not translate host name myhost.example.com to address"),
        ConnectionError('PostgreSQL server at "myhost.example.com:5432" rejected SSL upgrade'),
    ],
)
def test_never_leaks_connection_details(exc):
    result = explain(exc)
    blob = (result["cause"] + " " + " ".join(result["fixes"])).lower()
    for secret in SECRET_DSN_BITS:
        assert secret.lower() not in blob, f"{secret!r} leaked into diagnostic"


def test_missing_database_named_like_password_is_db_not_found():
    result = explain(RuntimeError('FATAL: database "password" does not exist'))
    assert result["category"] == "DB_NOT_FOUND"


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: relation does not exist in database payroll",
        "operation timed after 5 seconds",
        "connection was refused by the proxy policy",
        'FATAL: role "app" is not permitted to log in',
        'ERROR: role "app" already exists',
        'ERROR: database "payroll" already exists',
        "FATAL: unsupported ssl of this version",
        "SSL is not supported by this build",
        "ssl is requested but unavailable",
        "client certificate is required",
        "could not initialize SSL connection: EOF detected",
        "ssl connection is closed",
        "ssl connection is requested but unavailable",
        "authenticated connection is required",
        "server certificate is insecure",
        "cannot perform operation: connection is closed",
        "too many connect attempts",
        "too many closed sockets",
        "remaining retries: 0",
        "proxy: upstream authentication failed, retrying",
        "password authentication is disabled",
        "server_login_timeout reached",
    ],
)
def test_loose_wording_does_not_match_a_marker(message):
    assert explain(RuntimeError(message))["category"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("could not translate host name: SSL required", "DNS_FAILURE"),
        ("SSL required: too many clients", "SSL_REQUIRED"),
        ("password authentication failed: too many clients", "POOL_EXHAUSTED"),
        ('role "app" does not exist in database "payroll"', "AUTH_FAILED"),
        ('database "timed out" does not exist', "DB_NOT_FOUND"),
    ],
)
def test_message_with_two_markers_takes_the_earlier_branch(message, expected):
    assert explain(RuntimeError(message))["category"] == expected
