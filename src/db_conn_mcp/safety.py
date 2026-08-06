"""The write-safety gate — a pure decision function, no I/O.

Keeping this pure makes the security boundary trivially testable and unmissable.
Decision order (see ARCHITECTURE.md §5):

    1. mode != "write"   -> REJECT  (hard, native; can never be bypassed)
    2. yolo is True      -> ALLOW
    3. user_consent True -> ALLOW
    4. otherwise         -> REJECT  (show SQL to user, re-call with consent)

``yolo`` and ``user_consent`` only relax the *prompt* on an already-``write`` DB;
they can never make a ``read`` DB writable.
"""

from .models import Connection

#: Returned to the agent when a write needs explicit consent (step 4).
CONSENT_INSTRUCTION = (
    "This database requires explicit consent for writes. First read the target "
    "table and its schema, then show the user the exact SQL you intend to run and "
    "ask for permission. Only call again with user_consent=true if they agree."
)


class WriteRejected(Exception):  # noqa: N818 — spec-named; not an "*Error" by design
    """Raised when a write must not proceed, carrying the precise next step."""


def authorize_write(conn: Connection, user_consent: bool) -> None:
    """Allow the write (return ``None``) or raise :class:`WriteRejected`."""
    if conn.mode != "write":
        raise WriteRejected(
            f"Database {conn.name!r} is read-only (mode=read). Writes are blocked at "
            "the database level and cannot be enabled by yolo or consent."
        )
    if conn.yolo:
        return None
    if user_consent:
        return None
    raise WriteRejected(CONSENT_INSTRUCTION)


def authorize_dry_run(conn: Connection) -> None:
    """Allow a dry-run write (execute + ROLLBACK) or raise :class:`WriteRejected`.

    Only the ``mode`` gate applies: a dry-run never commits, so it needs no yolo
    or per-operation consent — its purpose is to show the user what a write
    *would* do **before** they consent to the real one. But it does execute
    server-side (locks, sequence advancement, trigger side effects until
    rollback), so the hard ``mode`` boundary is never waived.
    """
    if conn.mode != "write":
        raise WriteRejected(
            f"Database {conn.name!r} is read-only (mode=read). A dry-run still "
            "executes (then rolls back), so it is only allowed on write-mode databases."
        )
