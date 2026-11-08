"""Record every change to ``connections.json`` as one line in a sidecar beside it.

The sidecar is ``connections.json.audit.log``: append-only text, one tab-separated
line per changed connection (timestamp, source, action, name, changed fields). It
never holds a DSN, host, user or password (Rule 6): a DSN change is only named as
``dsn: (replaced)``, and a name shaped like a DSN is written as ``(withheld)``.

Nothing here writes to the terminal: under the stdio transport, standard output is
the MCP protocol. A sidecar that cannot be opened raises ``OSError`` to the caller,
which then makes no change. So does a sidecar that is a link: a cloned repo can commit
``connections.json.audit.log`` as a symlink to a shell profile, and every config
change would then append to that file.
"""

import contextlib
import json
import os
import stat
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from .models import Config, Connection

#: The front-end a change came through.
Source = Literal["cli", "gui", "mcp"]

#: Fields whose values are safe to record, in the order a line lists them.
_RECORDED_FIELDS = ("mode", "yolo", "fallback_ports")

#: Text that marks a name as a pasted URL DSN, a credential, or a libpq ``key=value`` DSN.
_DSN_MARKERS = ("://", "@", "=")

#: ``0`` on Windows, which has no ``O_NOFOLLOW``; the link is checked by path there instead.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


def audit_path(config_path: Path) -> Path:
    """Return the sidecar for the config at ``config_path``, beside a symlink's target.

    Derived from the resolved path, as :func:`config.locked` derives its ``.lock``, so a
    link and its target share one history.
    """
    real = config_path.resolve()
    return real.with_name(real.name + ".audit.log")


def _added(conn: Connection) -> str:
    """Describe a new connection by its recorded fields, never its DSN."""
    return ", ".join(f"{field}: {json.dumps(getattr(conn, field))}" for field in _RECORDED_FIELDS)


def _edited(old: Connection, new: Connection) -> str:
    """Describe what differs between two versions of one connection; ``""`` if nothing."""
    changes = [
        f"{field}: {json.dumps(getattr(old, field))} -> {json.dumps(getattr(new, field))}"
        for field in _RECORDED_FIELDS
        if getattr(old, field) != getattr(new, field)
    ]
    if old.dsn != new.dsn:
        changes.append("dsn: (replaced)")
    return ", ".join(changes)


def changes_between(previous: Config, updated: Config) -> list[tuple[str, str, str]]:
    """Return one ``(action, name, changes)`` per connection that differs, matched by name.

    Removals come first, in ``previous`` order, then edits and adds in ``updated`` order.
    Unknown keys are not compared: no front-end changes them, and they may hold anything.
    """
    before = {conn.name: conn for conn in previous.connections}
    after = {conn.name for conn in updated.connections}
    result = [("remove", conn.name, "") for conn in previous.connections if conn.name not in after]
    for conn in updated.connections:
        old = before.get(conn.name)
        if old is None:
            result.append(("add", conn.name, _added(conn)))
            continue
        changes = _edited(old, conn)
        if changes:
            result.append(("edit", conn.name, changes))
    return result


def _name_column(name: str | None) -> str:
    """Render a connection name so it can neither leak a DSN nor break the line."""
    if name is None:
        return "-"
    if any(marker in name for marker in _DSN_MARKERS):
        return "(withheld)"
    return json.dumps(name)  # quoted: a newline or tab in a name stays inside the column


def line(source: Source, action: str, name: str | None, changes: str = "") -> str:
    """Format one sidecar line, trailing newline included; ``remove``/``reset`` have no changes."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    columns = [timestamp, source, action, _name_column(name)]
    if changes:
        columns.append(changes)
    return "\t".join(columns) + "\n"


def _not_a_plain_file(sidecar: Path) -> OSError:
    """The error for a sidecar that is not a plain file of its own; names no DSN."""
    return OSError(
        f"The audit log {sidecar} is a link or not a regular file; refusing to append to it. "
        "Replace it with a regular file."
    )


def _open_own_file(sidecar: Path) -> int:
    """Open ``sidecar`` for appending, refusing a symlink, a hard link or a non-file."""
    if not _NO_FOLLOW and os.path.islink(sidecar):
        raise _not_a_plain_file(sidecar)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | _NO_FOLLOW
    fd = os.open(sidecar, flags, 0o600)
    status = os.fstat(fd)
    # A hard link shares the target's inode, so it is the symlink attack without a link.
    hard_linked = os.name == "posix" and status.st_nlink != 1
    if not stat.S_ISREG(status.st_mode) or hard_linked:
        os.close(fd)
        raise _not_a_plain_file(sidecar)
    return fd


@contextlib.contextmanager
def appending(sidecar: Path, lines: list[str]) -> Iterator[None]:
    """Open ``sidecar`` before the ``with`` body, append ``lines`` only if the body succeeds.

    Opened first so a sidecar that cannot be opened refuses the change before it is
    made; written last so a change that failed is never recorded. With no ``lines`` the
    filesystem is not touched. ``O_BINARY`` keeps Windows from writing ``\\r\\n``.
    """
    if not lines:
        yield
        return
    fd = _open_own_file(sidecar)
    try:
        yield
        os.write(fd, "".join(lines).encode("utf-8"))
    finally:
        os.close(fd)
