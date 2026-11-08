"""Resolve, load, validate, and save ``connections.json``.

Knows nothing about any specific database — only about where the config lives and
how to read/write it. Resolution order (first existing wins):

1. ``--config <path>`` (explicit)
2. ``./connections.json`` (repo-scoped)
3. ``~/.db-conn-mcp/connections.json`` (global-scoped)

All errors are raised as :class:`ConfigError` with actionable, **sanitized** messages
— never echoing a DSN or credentials (Rule 6).
"""

import contextlib
import errno
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from . import audit
from .atomic_write import write_text_atomically
from .dialects.registry import dialect_for, supported_schemes
from .models import Config, Connection

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

#: Filename used for both the repo- and global-scoped locations.
CONFIG_FILENAME = "connections.json"


class ConfigError(Exception):
    """Raised for missing/invalid config, with an actionable, sanitized message."""


def repo_config_path() -> Path:
    """Return the repo-scoped config path (``./connections.json``)."""
    return Path.cwd() / CONFIG_FILENAME


def global_config_path() -> Path:
    """Return the global-scoped config path (``~/.db-conn-mcp/connections.json``)."""
    return Path.home() / ".db-conn-mcp" / CONFIG_FILENAME


def resolve_path(explicit: str | None = None) -> Path:
    """Return the first config path that exists per the resolution order.

    Raises :class:`ConfigError` with guidance (including ``setup``) if none exist.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(
                f"No config file at the path you passed with --config: {path}. "
                "Create it, or run `db-conn-mcp setup` to generate one."
            )
        return path

    for candidate in (repo_config_path(), global_config_path()):
        if candidate.is_file():
            return candidate

    raise ConfigError(
        "No connections.json found. Looked in: "
        f"{repo_config_path()} and {global_config_path()}. "
        "Run `db-conn-mcp setup` to create one, or pass --config <path>."
    )


def _validate(config: Config) -> Config:
    """Apply cross-field rules beyond pydantic: unique names, known DSN schemes."""
    seen: set[str] = set()
    for conn in config.connections:
        if conn.name in seen:
            raise ConfigError(f"Duplicate connection name {conn.name!r}. Names must be unique.")
        seen.add(conn.name)
        try:
            dialect_for(conn.dsn)
        except ValueError:
            raise ConfigError(
                f"Connection {conn.name!r} uses an unsupported DSN scheme. "
                f"Supported schemes: {', '.join(supported_schemes())}."
            ) from None
    return config


def load(explicit: str | None = None) -> Config:
    """Resolve, parse, and validate the config into a :class:`Config`."""
    path = resolve_path(explicit)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}).") from None
    except UnicodeDecodeError:
        # from None: the decode error's .object holds the whole file, DSNs included (Rule 6).
        raise ConfigError(f"{path} is not valid JSON: the file is not UTF-8 text.") from None
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        # Summarize without echoing field values (a value could be a DSN).
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"Invalid connections.json: {problems}.") from None
    return _validate(config)


def get(config: Config, name: str) -> Connection:
    """Look up a connection by name; unknown name lists the available names."""
    for conn in config.connections:
        if conn.name == name:
            return conn
    available = ", ".join(c.name for c in config.connections) or "(none configured)"
    raise ConfigError(f"Unknown database {name!r}. Available: {available}.")


def _dump_connection(conn: Connection) -> dict:
    """Return one connection's JSON document, unknown keys included."""
    # exclude_none: optional keys absent from a user's file must stay absent (issue #10).
    # It would drop a null-valued unknown key too, so model_extra goes back on top.
    return {**conn.model_dump(exclude_none=True), **(conn.model_extra or {})}


def save(config: Config, path: Path, *, previous: Config, source: audit.Source) -> None:
    """Atomically rewrite ``connections.json``, keeping its mode and any unknown keys.

    Each connection that differs from ``previous`` gets one line in the audit sidecar,
    tagged with ``source``. The sidecar is opened before the config is written, so a
    sidecar that cannot be opened raises ``OSError`` with the config untouched. An append
    or close that fails after the write raises :class:`audit.UnrecordedChangeError`, with
    the change in effect.
    """
    lines = [
        audit.line(source, action, name, changes)
        for action, name, changes in audit.changes_between(previous, config)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **config.model_dump(exclude_none=True, exclude={"connections"}),
        "connections": [_dump_connection(conn) for conn in config.connections],
        **(config.model_extra or {}),
    }
    with audit.appending(audit.audit_path(path), lines):
        write_text_atomically(path, json.dumps(payload, indent=2) + "\n")


def _acquire_lock(fd: int) -> None:
    """Block until this process holds the exclusive lock on the open file ``fd``."""
    if sys.platform != "win32":
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    # Not LK_LOCK: it stalls a full second per attempt, which a busy config can't afford.
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno != errno.EACCES:  # EACCES is the CRT's "already locked"
                raise
            time.sleep(0.01)
        else:
            return


def _release_lock(fd: int) -> None:
    """Release the lock taken by :func:`_acquire_lock`."""
    if sys.platform != "win32":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock on the config at ``path`` for the ``with`` body.

    The lock lives on a ``connections.json.lock`` sidecar, never on the config itself:
    :func:`save` swaps the config's inode, so a lock on it would guard a file that is
    gone. The sidecar is derived from the resolved path, so a symlink and its target
    contend on one lock. It is never deleted, for the same reason: a process blocked on
    the old inode would then hold a lock nobody else can see.
    """
    real = path.resolve()
    sidecar = real.with_name(real.name + ".lock")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(sidecar, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire_lock(fd)
        try:
            yield
        finally:
            _release_lock(fd)
    finally:
        os.close(fd)


def set_yolo(path: Path, name: str, enabled: bool, *, source: audit.Source) -> Config:
    """Persist the ``yolo`` flag for one named DB and return the updated config."""
    with locked(path):
        config = load(str(path))
        previous = config.model_copy(deep=True)
        get(config, name).yolo = enabled  # raises ConfigError if the name is unknown
        save(config, path, previous=previous, source=source)
    return config
