"""Command-line entry point: argparse wiring for the server and the setup wizard.

Two responsibilities, no DB knowledge:
  * ``db-conn-mcp [--transport stdio|http] [--config PATH]`` — launch the server.
  * ``db-conn-mcp setup`` — interactive wizard to register the first DB and
    (optionally) inject the server into Claude Desktop / Cursor configs.

The wizard's *logic* (path resolution, DB registration, config injection) lives in
pure helpers so it stays testable; the interactive loop is a thin shell over them.
Only the connection *name* is ever echoed — never the DSN (Rule 6).
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal

from . import config, server
from .dialects.registry import dialect_for
from .models import Config, Connection

Scope = Literal["global", "repo"]


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
    cfg.connections.append(Connection(name=name, dsn=dsn, mode=mode, yolo=yolo))
    config.save(cfg, path)
    return path


def mcp_server_entry(config_path: Path) -> dict:
    """The MCP-client entry that launches this server against a given config."""
    return {"command": "db-conn-mcp", "args": ["--config", str(config_path)]}


def inject_server(existing: dict, name: str, entry: dict) -> dict:
    """Insert/overwrite an ``mcpServers[name]`` entry in a client config dict."""
    existing.setdefault("mcpServers", {})[name] = entry
    return existing


def agent_config_paths() -> dict[str, Path]:
    """OS-aware config locations for known MCP clients (may not exist yet)."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        claude = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        claude = home / ".config" / "Claude" / "claude_desktop_config.json"
    return {
        "claude": claude,
        "cursor": home / ".cursor" / "mcp.json",
        # Agy (Google Antigravity) — unified MCP config shared by its CLI and IDE.
        "agy": home / ".gemini" / "config" / "mcp_config.json",
    }


def detected_agent_configs() -> dict[str, Path]:
    """Like :func:`agent_config_paths`, but only clients whose config file exists."""
    return {client: path for client, path in agent_config_paths().items() if path.is_file()}


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
    """Show which MCP client configs were detected, then offer to inject per client."""
    detected = detected_agent_configs()
    if not detected:
        print("No MCP client configs detected (looked for Claude Desktop, Cursor). Skipping.")
        return

    print("\nDetected MCP client config(s):")
    for client, path in detected.items():
        print(f"  - {client}: {path}")
    if not input("Inject db-conn-mcp into them? (y/N): ").strip().lower().startswith("y"):
        return

    entry = mcp_server_entry(config_path)
    for client, path in detected.items():
        if not input(f"  Add to {client}? (y/N): ").strip().lower().startswith("y"):
            continue
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        path.write_text(
            json.dumps(inject_server(existing, "db-conn-mcp", entry), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  Updated {client} config.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (declared in ``pyproject.toml`` as ``db-conn-mcp``)."""
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        return run_setup_wizard()
    server.run(transport=args.transport, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
