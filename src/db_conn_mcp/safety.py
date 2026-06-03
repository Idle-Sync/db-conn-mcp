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


class WriteRejected(Exception):  # noqa: N818 — spec-named; not an "*Error" by design
    """Raised when a write must not proceed, carrying the precise next step."""


def authorize_write(conn: Connection, user_consent: bool) -> None:
    """Allow the write (return ``None``) or raise :class:`WriteRejected`."""
    raise NotImplementedError
