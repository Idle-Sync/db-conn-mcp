"""Replace a file's contents all at once, so a failed write costs the user nothing.

Standard library only: ``clients.py`` imports this and stays free of ``asyncpg``.
"""

import os
import shutil
import tempfile
from pathlib import Path


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
    """
    target = path.resolve()
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if target.exists():
            shutil.copymode(target, temp_name)
        os.replace(temp_name, target)
    except BaseException:  # Ctrl-C must not leave the temp file behind either
        # missing_ok: a Ctrl-C landing just after the replace must stay a Ctrl-C.
        Path(temp_name).unlink(missing_ok=True)
        raise
