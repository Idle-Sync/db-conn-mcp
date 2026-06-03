"""The doctor: classify driver/connection errors into sanitized cause + fix.

:func:`explain` receives ONLY an exception — never a ``Connection`` or DSN — so no
host, user, or password can ever leak into a tool result or log (Rule 6).

The sanitation guarantee is structural: the ``cause``/``fixes`` returned are **fixed,
canned strings per category**. The original exception's message (which *might* contain
a host or user) is only inspected for pattern-matching and is never copied into output.
"""

from typing import Literal

Category = Literal[
    "AUTH_FAILED",
    "HOST_UNREACHABLE",
    "DB_NOT_FOUND",
    "DNS_FAILURE",
    "SSL_REQUIRED",
    "POOL_EXHAUSTED",
    "UNKNOWN",
]

#: Canned, sanitized advice per category. No host/user/password ever appears here.
_ADVICE: dict[Category, dict] = {
    "AUTH_FAILED": {
        "cause": "Authentication failed — wrong username/password, or the role does not exist.",
        "fixes": [
            "Verify the username and password in your DSN.",
            "Confirm the role exists and may log in (pg_hba.conf / GRANT).",
        ],
    },
    "HOST_UNREACHABLE": {
        "cause": "The database is not accepting connections (refused or timed out).",
        "fixes": [
            "Check the database is running and the host/port are correct.",
            "Check firewalls / security groups between you and the server.",
            "In Docker, `localhost` inside a container is NOT the database container — "
            "use the service name or host gateway.",
        ],
    },
    "DB_NOT_FOUND": {
        "cause": "The named database does not exist on the server.",
        "fixes": [
            "Check the database name for spelling and case.",
            "Create the database if it has not been created yet.",
        ],
    },
    "DNS_FAILURE": {
        "cause": "The hostname could not be resolved to an address.",
        "fixes": [
            "Check the host portion of the DSN for typos.",
            "Confirm DNS/VPN/network access to the host is available.",
        ],
    },
    "SSL_REQUIRED": {
        "cause": "The server requires an SSL/TLS connection.",
        "fixes": [
            "Add an appropriate SSL mode to the DSN, e.g. `?sslmode=require`.",
        ],
    },
    "POOL_EXHAUSTED": {
        "cause": "The server's connection limit has been reached.",
        "fixes": [
            "Close idle sessions, or raise the server's `max_connections`.",
        ],
    },
    "UNKNOWN": {
        "cause": "Unrecognized connection error.",
        "fixes": [
            "Open the `troubleshoot_connection` prompt for the full gotchas checklist.",
        ],
    },
}

# DNS resolution failures surface as OSError/socket.gaierror with these markers.
_DNS_MARKERS = (
    "name or service not known",
    "could not translate host name",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "name does not resolve",
)


def _classify(error: Exception) -> Category:
    """Map an exception to a category using its type and message *markers only*."""
    # asyncpg's typed errors are the most reliable signal — match by class name so we
    # don't hard-depend on import paths.
    type_name = type(error).__name__
    if type_name == "InvalidPasswordError":
        return "AUTH_FAILED"
    if type_name == "InvalidAuthorizationSpecificationError":
        return "AUTH_FAILED"
    if type_name == "InvalidCatalogNameError":
        return "DB_NOT_FOUND"
    if type_name == "TooManyConnectionsError":
        return "POOL_EXHAUSTED"

    message = str(error).lower()

    if isinstance(error, ConnectionRefusedError):
        return "HOST_UNREACHABLE"
    if isinstance(error, TimeoutError):
        return "HOST_UNREACHABLE"

    if any(marker in message for marker in _DNS_MARKERS):
        return "DNS_FAILURE"
    if "ssl" in message:
        return "SSL_REQUIRED"
    if "too many" in message or "remaining connection slots" in message:
        return "POOL_EXHAUSTED"
    if "password authentication failed" in message or "role" in message and "exist" in message:
        return "AUTH_FAILED"
    if "does not exist" in message and "database" in message:
        return "DB_NOT_FOUND"
    if "connection refused" in message or "timed out" in message or "timeout" in message:
        return "HOST_UNREACHABLE"

    return "UNKNOWN"


def explain(error: Exception) -> dict:
    """Map a driver exception to ``{category, cause, fixes[]}`` (sanitized strings).

    Output contains only canned advice — no host/user/password substrings.
    """
    category = _classify(error)
    advice = _ADVICE[category]
    return {"category": category, "cause": advice["cause"], "fixes": list(advice["fixes"])}
