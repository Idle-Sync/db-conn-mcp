"""Replace a file's contents all at once, so a failed write costs the user nothing.

Standard library only: ``clients.py`` imports this and stays free of ``asyncpg``.
"""

import contextlib
import errno
import os
import shutil
import tempfile
import time
from pathlib import Path

# Windows can refuse a swap with error 5 for a moment while another handle is on the target.
_TRANSIENT_WINERRORS = (5,)
_REPLACE_ATTEMPTS = 20


def _replace_with_retry(temp_name: str, target: Path) -> None:
    """Swap the temp file over ``target``, waiting out a brief refusal from Windows."""
    # Asked once, before any attempt: os.access is also False while the target's attributes
    # can't be read, the state being waited out. A read-only target never clears.
    read_only = target.exists() and not os.access(target, os.W_OK)
    attempts_left = _REPLACE_ATTEMPTS
    while True:
        attempts_left -= 1
        try:
            os.replace(temp_name, target)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)  # the attribute exists on Windows only
            transient = exc.errno == errno.EACCES and winerror in _TRANSIENT_WINERRORS
            if read_only or not transient or attempts_left == 0:
                raise
            time.sleep(0.05)
        else:
            return


def write_text_atomically(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 to a temp file beside ``path``, then swap it into place.

    The target is never opened for writing, so on any failure it keeps its old bytes
    and no temp file is left behind. A symlinked ``path`` stays a symlink: the file it
    points at is the one replaced.

    The temp file is user-only (0600) from its creation, not chmod'ed afterwards: a
    write-then-chmod leaves the contents on disk at the umask default (typically 0644)
    for the window in between, readable by every local user. An existing target's mode
    is copied over before the swap, so only a file created from nothing ends up 0600.
    The mode is advisory on Windows, where NTFS ignores POSIX bits.

    A swap Windows refuses with error 5 is retried for up to about a second before the
    error reaches the caller, unless the target is read-only. A temp file that inherited a
    read-only target's mode is made writable before removal, so the swap's error is the one
    raised and no temp is left.
    """
    target = path.resolve()
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if target.exists():
            shutil.copymode(target, temp_name)
        _replace_with_retry(temp_name, target)
    except BaseException:  # Ctrl-C must not leave the temp file behind either
        # missing_ok: a Ctrl-C landing just after the replace must stay a Ctrl-C.
        # Windows refuses to delete a read-only file, and the temp file copied that bit.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(temp_name, 0o600)
        Path(temp_name).unlink(missing_ok=True)
        raise
