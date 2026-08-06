"""Resolve, load, validate, and save ``connections.json``.

Knows nothing about any specific database — only about where the config lives and
how to read/write it. Resolution order (first existing wins):

1. ``--config <path>`` (explicit)
2. ``./connections.json`` (repo-scoped)
3. ``~/.db-conn-mcp/connections.json`` (global-scoped)

All errors are raised as :class:`ConfigError` with actionable, **sanitized** messages
— never echoing a DSN or credentials (Rule 6).
"""

import json
import os
from pathlib import Path

from pydantic import ValidationError

from .dialects.registry import dialect_for, supported_schemes
from .models import Config, Connection

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


def save(config: Config, path: Path) -> None:
    """Atomically rewrite ``connections.json`` (temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # exclude_none: optional keys absent from a user's file must stay absent (issue #10).
    payload = config.model_dump(exclude_none=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_yolo(path: Path, name: str, enabled: bool) -> Config:
    """Persist the ``yolo`` flag for one named DB and return the updated config."""
    config = load(str(path))
    get(config, name).yolo = enabled  # raises ConfigError if the name is unknown
    save(config, path)
    return config
