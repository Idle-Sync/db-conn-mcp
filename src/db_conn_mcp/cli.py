"""Command-line entry point: argparse wiring for the server and the setup wizard.

Two responsibilities, no DB knowledge:
  * ``db-conn-mcp [--transport stdio|http] [--config PATH]`` — launch the server.
  * ``db-conn-mcp setup`` — interactive wizard to register the first DB and
    (optionally) inject the server into detected MCP client configs. Each client
    carries its own config path and entry format (``mcpServers`` for Claude
    Desktop/Cursor/Agy/Windsurf/Claude Code/Cline, ``servers`` for VS Code,
    ``context_servers`` for Zed) via :class:`ClientSpec`.

The wizard's *logic* (path resolution, DB registration, config injection) lives in
pure helpers so it stays testable; the interactive loop is a thin shell over them.
Only the connection *name* is ever echoed — never the DSN (Rule 6).
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import config, server
from .dialects.registry import dialect_for
from .models import Config, Connection

Scope = Literal["global", "repo"]

#: How an MCP client stores server entries. Each value maps to a container key +
#: entry shape in :func:`inject_entry`.
ClientFormat = Literal["mcpServers", "vscode", "zed"]

#: The executable an MCP client launches to start this server.
SERVER_COMMAND = "db-conn-mcp"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser (server flags + ``setup`` subcommand)."""
    parser = argparse.ArgumentParser(
        prog="db-conn-mcp",
        description="A dead-simple, self-hosted MCP server for querying databases.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport to launch (default: stdio).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Explicit path to connections.json (overrides repo/global lookup).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("setup", help="Interactive wizard to register a database.")
    return parser


# ---- Pure helpers (tested) ---------------------------------------------------


def scope_to_path(scope: Scope) -> Path:
    """Resolve a setup scope to its ``connections.json`` path."""
    return config.global_config_path() if scope == "global" else config.repo_config_path()


def register_database(scope: Scope, name: str, dsn: str, mode: str, yolo: bool = False) -> Path:
    """Validate and append a connection to the scoped config; return its path.

    Raises ``ValueError`` for an unknown DSN scheme or a duplicate name.
    """
    dialect_for(dsn)  # validates the scheme; raises ValueError naming supported schemes
    path = scope_to_path(scope)
    cfg = config.load(str(path)) if path.is_file() else Config()
    if any(c.name == name for c in cfg.connections):
        raise ValueError(f"A connection named {name!r} already exists in {path}.")
    # Guard against the same database registered twice. Note: only `name` is named in
    # the error — never echo the DSN (Rule 6).
    existing = next((c for c in cfg.connections if c.dsn == dsn), None)
    if existing is not None:
        raise ValueError(
            f"That connection string is already registered as {existing.name!r}. "
            "Use that name, or remove it first if you want to re-add it."
        )
    cfg.connections.append(Connection(name=name, dsn=dsn, mode=mode, yolo=yolo))
    config.save(cfg, path)
    return path


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


def server_args(config_path: Path) -> list[str]:
    """The CLI args an MCP client passes to launch this server for a given config."""
    return ["--config", str(config_path)]


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


def parse_client_selection(raw: str, count: int) -> list[int]:
    """Parse a user selection string into sorted, unique 0-based indices.

    Accepts ``"all"`` (case-insensitive), comma/space-separated 1-based numbers
    (e.g. ``"1,3"`` or ``"1 3"``), or empty for none. Out-of-range and non-numeric
    tokens are ignored so a typo never injects into the wrong client.
    """
    text = raw.strip().lower()
    if not text:
        return []
    if text == "all":
        return list(range(count))
    chosen: set[int] = set()
    for token in re.split(r"[,\s]+", text):
        if token.isdigit() and 1 <= int(token) <= count:
            chosen.add(int(token) - 1)
    return sorted(chosen)


# ---- Interactive wizard (thin shell over the helpers) ------------------------


def run_setup_wizard() -> int:
    """Interactively register the first database and offer agent injection."""
    print("db-conn-mcp setup")
    scope = input("Config scope - [g]lobal or [r]epo? (g/r): ").strip().lower() or "g"
    scope_name: Scope = "repo" if scope.startswith("r") else "global"

    name = input("Connection name: ").strip()
    dsn = input("DSN (e.g. postgresql://user:pass@host:5432/db): ").strip()
    mode = input("Mode - [r]ead or [w]rite? (r/w): ").strip().lower()
    mode_name = "write" if mode.startswith("w") else "read"

    try:
        path = register_database(scope_name, name, dsn, mode_name)
    except ValueError as exc:
        print(f"Could not register: {exc}")
        return 1
    # Echo only the name — never the DSN (Rule 6).
    print(f"Registered {name!r} ({mode_name}) -> {path}")

    _offer_injection(path)
    return 0


def _offer_injection(config_path: Path) -> None:
    """List the detected MCP clients, then offer to inject per client (right format each)."""
    detected = detected_clients()
    if not detected:
        print(
            "\nNo MCP client configs detected (looked for: "
            + ", ".join(s.label for s in client_specs())
            + "). Skipping injection."
        )
        return

    print("\nDetected MCP client config(s):")
    for i, spec in enumerate(detected, 1):
        print(f"  {i}. {spec.label} [{spec.fmt}]: {spec.path}")
    raw = input("Inject db-conn-mcp into which? (e.g. 1,3 or 'all'; Enter to skip): ")
    chosen = [detected[i] for i in parse_client_selection(raw, len(detected))]
    if not chosen:
        print("No clients selected. Skipping injection.")
        return

    args = server_args(config_path)
    for spec in chosen:
        try:
            existing = json.loads(spec.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        updated = inject_entry(existing, spec.fmt, "db-conn-mcp", SERVER_COMMAND, args)
        spec.path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        print(f"  Updated {spec.label} config.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (declared in ``pyproject.toml`` as ``db-conn-mcp``)."""
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        return run_setup_wizard()
    server.run(transport=args.transport, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
