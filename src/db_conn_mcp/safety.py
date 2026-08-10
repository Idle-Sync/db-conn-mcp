"""The write-safety gate — a pure decision function, no I/O.

Keeping this pure makes the security boundary trivially testable and unmissable.
Decision order (see docs/ARCHITECTURE.md §5):

    1. mode != "write"            -> REJECT  (hard, native; can never be bypassed)
    2. no dry-run grant & no skip -> REJECT  (server-enforced preview — commits only)
    3. yolo is True               -> ALLOW
    4. user_consent True          -> ALLOW
    5. otherwise                  -> REJECT  (show SQL to user, re-call with consent)

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

#: Returned to the agent when a commit is attempted without a prior dry-run.
DRY_RUN_INSTRUCTION = (
    "This statement has not been previewed. Call execute_write_query with "
    "dry_run=true (the default) first — it executes the statement and always rolls "
    "back, reporting what would change. Then call again with dry_run=false to "
    "commit. Pass skip_dry_run=true ONLY if the user explicitly asked to skip the "
    "preview."
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


def authorize_commit(
    conn: Connection, user_consent: bool, *, has_grant: bool, skip_dry_run: bool
) -> None:
    """Gate a committing write: mode -> dry-run-first -> yolo -> consent.

    ``has_grant`` means the identical statement was dry-run recently (the caller
    tracks grants). ``skip_dry_run`` waives ONLY the preview stage — the agent may
    set it solely to attest the user explicitly asked to skip (same trust model as
    ``user_consent``). Neither flag can ever bypass ``mode``, and ``yolo`` cannot
    bypass the preview stage.
    """
    if conn.mode != "write":
        raise WriteRejected(
            f"Database {conn.name!r} is read-only (mode=read). Writes are blocked at "
            "the database level and cannot be enabled by yolo or consent."
        )
    if not has_grant and not skip_dry_run:
        raise WriteRejected(DRY_RUN_INSTRUCTION)
    authorize_write(conn, user_consent)


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
