"""Known MCP clients: where each stores its config and how a server entry is shaped.

Extracted from ``cli.py`` so ``doctor.py`` can reuse it without a circular import.
"""

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomlkit

#: How an MCP client stores server entries. Each value maps to a container key, an
#: entry shape (:func:`_build_entry`) and a file syntax (:data:`_CODEC`) — the three
#: axes on which known clients differ. Values are NOT all JSON: ``codex`` is TOML.
ClientFormat = Literal["mcpServers", "vscode", "zed", "codex"]


@dataclass(frozen=True)
class ClientSpec:
    """One MCP client: where its config lives and which entry format it uses."""

    key: str  # short id, e.g. "vscode"
    label: str  # human-facing name
    path: Path  # config file (may not exist yet)
    fmt: ClientFormat


#: Container key (top-level JSON object) each format stores server entries under.
_CONTAINER_KEY: dict[ClientFormat, str] = {
    "mcpServers": "mcpServers",
    "vscode": "servers",
    "zed": "context_servers",
}


class ClientConfigError(Exception):
    """A client config file exists but could not be read or parsed.

    Raised instead of returning an empty document, so a read-merge-write can never
    silently overwrite a file whose contents we failed to understand. Messages name
    the client and its path only — never the file's contents (Rule 6).
    """


@dataclass(frozen=True)
class Codec:
    """Text <-> mapping for one config-file syntax."""

    loads: Callable[[str], dict]
    dumps: Callable[[dict], str]
    empty: Callable[[], dict]
    #: Parse failures to catch. Kept explicit so the callers' ``except`` stays narrow.
    errors: tuple[type[Exception], ...]


def _json_dumps(data: dict) -> str:
    """Serialize to JSON in exactly today's shape (indent=2, trailing newline)."""
    return json.dumps(data, indent=2) + "\n"


_JSON_CODEC = Codec(
    loads=json.loads,
    dumps=_json_dumps,
    empty=dict,
    errors=(json.JSONDecodeError,),
)

#: tomlkit round-trips comments and layout, so a hand-maintained config.toml
#: survives a read-merge-write intact.
_TOML_CODEC = Codec(
    loads=tomlkit.parse,
    dumps=tomlkit.dumps,
    empty=tomlkit.document,
    errors=(tomlkit.exceptions.TOMLKitError,),
)

#: Which syntax each format is written in.
_CODEC: dict[ClientFormat, Codec] = {
    "mcpServers": _JSON_CODEC,
    "vscode": _JSON_CODEC,
    "zed": _JSON_CODEC,
    "codex": _TOML_CODEC,
}


def read_config(spec: ClientSpec) -> dict:
    """Parse a client's config file into a mutable mapping.

    An **absent** file yields a fresh empty document — creating it is the point. A
    file that exists but cannot be read or parsed raises :class:`ClientConfigError`,
    so callers refuse to write rather than clobbering something they did not
    understand.
    """
    codec = _CODEC[spec.fmt]
    try:
        text = spec.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return codec.empty()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientConfigError(
            f"{spec.label}: {spec.path} could not be read ({type(exc).__name__})"
        ) from exc
    try:
        return codec.loads(text)
    except codec.errors as exc:
        raise ClientConfigError(
            f"{spec.label}: {spec.path} is not valid {spec.fmt} config ({type(exc).__name__})"
        ) from exc


def write_config(spec: ClientSpec, data: dict) -> None:
    """Serialize ``data`` back to the client's config file in its own syntax."""
    spec.path.write_text(_CODEC[spec.fmt].dumps(data), encoding="utf-8")


def _build_entry(fmt: ClientFormat, command: str, args: list[str]) -> dict:
    """Build a single server entry in the shape the given client format expects."""
    if fmt == "vscode":
        return {"type": "stdio", "command": command, "args": args}
    if fmt == "zed":
        return {"source": "custom", "command": {"path": command, "args": args}}
    return {"command": command, "args": args}  # mcpServers (Claude/Cursor/Windsurf/...)


def inject_entry(
    existing: dict, fmt: ClientFormat, name: str, command: str, args: list[str]
) -> dict:
    """Insert/overwrite the server entry under the format's container key, preserving the rest."""
    existing.setdefault(_CONTAINER_KEY[fmt], {})[name] = _build_entry(fmt, command, args)
    return existing


def remove_entry(existing: dict, fmt: ClientFormat, name: str) -> dict:
    """Delete the server entry from the format's container key (no-op if absent)."""
    container = existing.get(_CONTAINER_KEY[fmt])
    if isinstance(container, dict):
        container.pop(name, None)
    return existing


def client_specs() -> list[ClientSpec]:
    """OS-aware specs for known MCP clients (config files may not exist yet)."""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    if sys.platform == "win32":
        claude = appdata / "Claude" / "claude_desktop_config.json"
        vscode_user = appdata / "Code" / "User"
        zed = appdata / "Zed" / "settings.json"
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        claude = support / "Claude" / "claude_desktop_config.json"
        vscode_user = support / "Code" / "User"
        zed = home / ".config" / "zed" / "settings.json"
    else:
        claude = home / ".config" / "Claude" / "claude_desktop_config.json"
        vscode_user = home / ".config" / "Code" / "User"
        zed = home / ".config" / "zed" / "settings.json"
    cline = (
        vscode_user
        / "globalStorage"
        / "saoudrizwan.claude-dev"
        / "settings"
        / "cline_mcp_settings.json"
    )
    return [
        ClientSpec("claude", "Claude Desktop", claude, "mcpServers"),
        ClientSpec("cursor", "Cursor", home / ".cursor" / "mcp.json", "mcpServers"),
        # Agy (Google Antigravity) — unified MCP config shared by its CLI and IDE.
        ClientSpec(
            "agy",
            "Agy (Antigravity)",
            home / ".gemini" / "config" / "mcp_config.json",
            "mcpServers",
        ),
        ClientSpec(
            "windsurf", "Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"
        ),
        ClientSpec("claude-code", "Claude Code", home / ".claude.json", "mcpServers"),
        ClientSpec("cline", "Cline", cline, "mcpServers"),
        ClientSpec("vscode", "VS Code", vscode_user / "mcp.json", "vscode"),
        ClientSpec("zed", "Zed", zed, "zed"),
    ]


def agent_config_paths() -> dict[str, Path]:
    """Back-compat view: ``{client_key: config_path}`` for all known clients."""
    return {s.key: s.path for s in client_specs()}


def detected_clients() -> list[ClientSpec]:
    """The subset of :func:`client_specs` whose config file actually exists."""
    return [s for s in client_specs() if s.path.is_file()]


def config_readable(spec: ClientSpec) -> bool:
    """Whether this client's config can be safely read-merge-written.

    True when the file is absent (we would create it) or parses. False when it
    exists but does not — the case where writing would destroy user content.
    """
    try:
        read_config(spec)
    except ClientConfigError:
        return False
    return True


def is_injected(spec: ClientSpec, name: str = "db-conn-mcp") -> bool:
    """Whether ``name`` is already registered under ``spec``'s container key.

    Never raises: an unreadable config means we cannot *prove* it is injected.
    """
    try:
        data = read_config(spec)
    except ClientConfigError:
        return False
    container = data.get(_CONTAINER_KEY[spec.fmt])
    return isinstance(container, dict) and name in container


def injected_command(spec: ClientSpec, name: str = "db-conn-mcp") -> str | None:
    """Return the command path of an injected entry, or None if absent/unreadable."""
    try:
        data = read_config(spec)
    except ClientConfigError:
        return None
    container = data.get(_CONTAINER_KEY[spec.fmt])
    if not isinstance(container, dict) or name not in container:
        return None
    entry = container[name]
    if not isinstance(entry, dict):
        return None
    if spec.fmt == "zed":
        command = entry.get("command")
        return command.get("path") if isinstance(command, dict) else None
    return entry.get("command")
