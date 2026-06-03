"""Resolve, load, validate, and save ``connections.json``.

Knows nothing about any specific database — only about where the config lives and
how to read/write it. Resolution order (first existing wins):

1. ``--config <path>`` (explicit)
2. ``./connections.json`` (repo-scoped)
3. ``~/.db-conn-mcp/connections.json`` (global-scoped)
"""

from pathlib import Path

from .models import Config, Connection

#: Global-scoped config location (third in the resolution order).
GLOBAL_CONFIG_PATH = Path.home() / ".db-conn-mcp" / "connections.json"
#: Repo-scoped config location (second in the resolution order).
REPO_CONFIG_PATH = Path("connections.json")


class ConfigError(Exception):
    """Raised for missing/invalid config, with an actionable, sanitized message."""


def resolve_path(explicit: str | None = None) -> Path:
    """Return the first config path that exists per the resolution order.

    Raises :class:`ConfigError` with guidance (including ``setup``) if none exist.
    """
    raise NotImplementedError


def load(explicit: str | None = None) -> Config:
    """Resolve, parse, and validate the config into a :class:`Config`."""
    raise NotImplementedError


def get(config: Config, name: str) -> Connection:
    """Look up a connection by name; unknown name lists the available names."""
    raise NotImplementedError


def save(config: Config, path: Path) -> None:
    """Atomically rewrite ``connections.json`` (temp file + replace)."""
    raise NotImplementedError


def set_yolo(path: Path, name: str, enabled: bool) -> Config:
    """Persist the ``yolo`` flag for one named DB and return the updated config."""
    raise NotImplementedError
