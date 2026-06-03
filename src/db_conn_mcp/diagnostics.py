"""The doctor: classify driver/connection errors into sanitized cause + fix.

:func:`explain` receives ONLY an exception — never a ``Connection`` or DSN — so no
host, user, or password can ever leak into a tool result or log (Rule 6). Every tool
that opens a connection routes failures through here.
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


def explain(error: Exception) -> dict:
    """Map a driver exception to ``{category, cause, fixes[]}`` (sanitized strings).

    Output must contain no host/user/password substrings.
    """
    raise NotImplementedError
