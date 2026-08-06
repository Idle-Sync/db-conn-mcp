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
import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import __commit__, __version__, config, server
from .dialects.registry import dialect_for
from .handlers import Handlers
from .models import Config, Connection

Scope = Literal["global", "repo"]

#: How an MCP client stores server entries. Each value maps to a container key +
#: entry shape in :func:`inject_entry`.
ClientFormat = Literal["mcpServers", "vscode", "zed"]

#: The console-script name declared in pyproject (its absolute path is what we inject).
SERVER_SCRIPT = "db-conn-mcp"

#: Public repository — surfaced as a one-line star nudge after a successful setup.
REPO_URL = "https://github.com/Idle-Sync/db-conn-mcp"


def server_launch(config_path: Path) -> tuple[str, list[str]]:
    """Return a ``(command, args)`` an MCP client can use to launch this server.

    Prefers the **absolute path** of the installed console script (``shutil.which``),
    which resolves whether the install is in a project ``.venv`` or a global/pipx
    environment — so the client never needs the script on its own PATH. Falls back to
    the current interpreter running the package as a module (``python -m db_conn_mcp``).
    """
    exe = shutil.which(SERVER_SCRIPT)
    if exe:
        return exe, ["--config", str(config_path)]
    return sys.executable, ["-m", "db_conn_mcp", "--config", str(config_path)]


def build_parser() -> argparse.ArgumentParser:
    """Build the parser: server flags plus the management subcommands.

    ``--config`` is shared (via a parent parser) so it works both before a
    subcommand (``db-conn-mcp --config X status``) and after it
    (``db-conn-mcp status --config X``).
    """
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS (not None) so that when --config is omitted on the subparser it does
    # not clobber a value supplied before the subcommand (a known argparse gotcha).
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Explicit path to connections.json (overrides repo/global lookup).",
    )

    parser = argparse.ArgumentParser(
        prog="db-conn-mcp",
        parents=[common],
        description="A dead-simple, self-hosted MCP server for querying databases.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"db-conn-mcp {__version__} ({__commit__})",
        help="Show the installed version and exit.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport to launch when no subcommand is given (default: stdio).",
    )

    sub = parser.add_subparsers(dest="command")

    def _add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    _add("setup", "Guided setup; shows status if already configured.")
    _add("status", "Show configured databases and MCP client injection state.")
    _add("add", "Add another database connection.")
    p_clients = _add("clients", "Inject (or with --remove, uninject) db-conn-mcp into MCP clients.")
    p_clients.add_argument(
        "--remove", action="store_true", help="Remove db-conn-mcp from clients instead of adding."
    )
    _add("check", "Probe connectivity for one database (or all); sanitized result.").add_argument(
        "name", nargs="?", default=None, help="Database to check; omit to check all."
    )
    _add("remove", "Remove a database connection by name.").add_argument(
        "name", help="The connection name to remove."
    )
    _add("reset", "Remove ALL connections (delete connections.json) for a fresh slate.")
    p_yolo = _add("yolo", "Enable/disable yolo (write-consent bypass) for one database.")
    p_yolo.add_argument("name", help="The database name.")
    p_yolo.add_argument("state", choices=["on", "off"], help="Turn yolo on or off.")
    return parser


# ---- Pure helpers (tested) ---------------------------------------------------


def scope_to_path(scope: Scope) -> Path:
    """Resolve a setup scope to its ``connections.json`` path."""
    return config.global_config_path() if scope == "global" else config.repo_config_path()


def _load_scope(scope: Scope) -> tuple[Path, Config]:
    """Return the scoped config path and its current contents (empty if absent)."""
    path = scope_to_path(scope)
    cfg = config.load(str(path)) if path.is_file() else Config()
    return path, cfg


def _ensure_new_connection(cfg: Config, name: str, dsn: str) -> None:
    """Validate a candidate connection against ``cfg``; raise ``ValueError`` if invalid.

    Checks: known DSN scheme, unique name, and unique connection string. The DSN is
    never echoed in any error (Rule 6) — duplicate-DSN errors name the existing entry.
    """
    dialect_for(dsn)  # validates the scheme; raises ValueError naming supported schemes
    if any(c.name == name for c in cfg.connections):
        raise ValueError(f"A connection named {name!r} already exists.")
    existing = next((c for c in cfg.connections if c.dsn == dsn), None)
    if existing is not None:
        raise ValueError(
            f"That connection string is already registered as {existing.name!r}. "
            "Use that name, or remove it first if you want to re-add it."
        )


def register_database(scope: Scope, name: str, dsn: str, mode: str, yolo: bool = False) -> Path:
    """Validate and append a connection to the scoped config; return its path.

    Raises ``ValueError`` for an unknown DSN scheme, a duplicate name, or a duplicate DSN.
    """
    path, cfg = _load_scope(scope)
    _ensure_new_connection(cfg, name, dsn)
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


def is_injected(spec: ClientSpec, name: str = "db-conn-mcp") -> bool:
    """Whether ``name`` is already registered under ``spec``'s container key."""
    try:
        data = json.loads(spec.path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    container = data.get(_CONTAINER_KEY[spec.fmt])
    return isinstance(container, dict) and name in container


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


# ---- Shared interactive building blocks --------------------------------------


def _existing_config(config_arg: str | None) -> Path | None:
    """Return the config path that resolves (explicit/repo/global), or None."""
    try:
        return config.resolve_path(config_arg)
    except config.ConfigError:
        return None


def _ask_scope() -> Scope:
    """Prompt for where a brand-new config should live."""
    scope = input("Config scope - [g]lobal or [r]epo? (g/r): ").strip().lower() or "g"
    return "repo" if scope.startswith("r") else "global"


def _choose_injection_targets() -> list[ClientSpec]:
    """List detected MCP clients (flagging already-injected) and return the picks."""
    detected = detected_clients()
    if not detected:
        print(
            "\nNo MCP client configs detected (looked for: "
            + ", ".join(s.label for s in client_specs())
            + ")."
        )
        return []

    print("\nDetected MCP client config(s):")
    for i, spec in enumerate(detected, 1):
        marker = " [already injected]" if is_injected(spec) else ""
        print(f"  {i}. {spec.label} [{spec.fmt}]{marker}: {spec.path}")
    raw = input("Inject db-conn-mcp into which? (e.g. 1,3 or 'all'; Enter to skip): ")
    chosen = [detected[i] for i in parse_client_selection(raw, len(detected))]
    if not chosen:
        print("No clients selected.")
    return chosen


def _inject_into_client(spec: ClientSpec, command: str, args: list[str]) -> None:
    """Read-merge-write a server entry into one client's config, in its own format."""
    try:
        existing = json.loads(spec.path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        existing = {}
    updated = inject_entry(existing, spec.fmt, "db-conn-mcp", command, args)
    spec.path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(f"  Updated {spec.label} config.")


def _inject_clients_interactive(path: Path) -> None:
    """Choose clients and inject the server (pointed at ``path``) into each."""
    chosen = _choose_injection_targets()
    command, args = server_launch(path)
    for spec in chosen:
        _inject_into_client(spec, command, args)


def _parse_fallback_ports(raw: str) -> list[int] | None:
    """Parse '5433, 15432' -> [5433, 15432]; empty input -> None (key omitted)."""
    if not raw.strip():
        return None
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit() or not 1 <= int(part) <= 65535:
            raise ValueError(f"Invalid port {part!r}: ports are integers 1-65535.")
        ports.append(int(part))
    return ports


def _add_connection_interactive(path: Path) -> bool:
    """Prompt for one database, validate, gather injection, then commit.

    Transactional: nothing is written until the end, so Ctrl+C saves nothing.
    Returns True if a connection was added, False on a validation error.
    """
    cfg = config.load(str(path)) if path.is_file() else Config()
    name = input("Connection name: ").strip()
    dsn = input("DSN (e.g. postgresql://user:pass@host:5432/db): ").strip()
    mode = input("Mode - [r]ead or [w]rite? (r/w): ").strip().lower()
    mode_name = "write" if mode.startswith("w") else "read"
    ports_raw = input("Fallback ports, for tunnels that move (comma-separated, Enter to skip): ")
    try:
        fallback_ports = _parse_fallback_ports(ports_raw)
    except ValueError as exc:
        print(f"Could not add: {exc}")
        return False

    try:
        _ensure_new_connection(cfg, name, dsn)
    except ValueError as exc:
        print(f"Could not add: {exc}")
        return False

    chosen = _choose_injection_targets()
    cfg.connections.append(
        Connection(name=name, dsn=dsn, mode=mode_name, fallback_ports=fallback_ports)
    )
    config.save(cfg, path)
    # Echo only the name — never the DSN (Rule 6).
    print(f"Registered {name!r} ({mode_name}) -> {path}")
    command, args = server_launch(path)
    for spec in chosen:
        _inject_into_client(spec, command, args)
    return True


def _print_status(path: Path) -> None:
    """Print configured databases (no DSN) and per-client injection state."""
    cfg = config.load(str(path))
    print(f"Config: {path}")
    print("Databases:")
    if not cfg.connections:
        print("  (none)")
    for c in cfg.connections:
        view = c.public_view()
        extra = f", fallback_ports={view['fallback_ports']}" if "fallback_ports" in view else ""
        print(f"  - {view['name']}  (mode={view['mode']}, yolo={view['yolo']}{extra})")

    print("\nMCP clients:")
    detected = detected_clients()
    if not detected:
        print("  (none detected)")
    for spec in detected:
        print(f"  - {spec.label}: {'injected' if is_injected(spec) else 'not injected'}")


# ---- Command handlers --------------------------------------------------------


def run_setup_wizard(config_arg: str | None = None) -> int:
    """Guided setup. First run registers a database; if already configured, shows
    status and offers follow-up actions. Ctrl+C / EOF cancels cleanly (nothing saved)."""
    try:
        existing = _existing_config(config_arg)
        if existing is not None:
            print("db-conn-mcp is already configured.\n")
            _print_status(existing)
            rc = _setup_menu(existing)
            _print_star_cta()
            return rc
        print("db-conn-mcp setup")
        path = scope_to_path(_ask_scope())
        if _add_connection_interactive(path):
            _print_star_cta()
            return 0
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was saved.")
        return 130


def _print_star_cta() -> None:
    """Print a one-line star nudge after setup finishes successfully.

    The honest version of "grow the repo": ask at the moment the tool just
    worked for someone. Never gates, blocks, or touches their GitHub account.
    """
    print(f"⭐ Find db-conn-mcp useful? A star helps others find it: {REPO_URL}")


def _setup_menu(path: Path) -> int:
    """Offer follow-up actions when a config already exists."""
    while True:
        choice = input("\nNext: [a]dd database, [c]lients, [q]uit? (a/c/q): ").strip().lower()
        if choice.startswith("a"):
            _add_connection_interactive(path)
        elif choice.startswith("c"):
            _inject_clients_interactive(path)
        else:
            return 0


def cmd_status(config_arg: str | None = None) -> int:
    """Show configured databases and MCP client injection state."""
    path = _existing_config(config_arg)
    if path is None:
        print("No configuration found. Run `db-conn-mcp setup` to create one.")
        return 1
    _print_status(path)
    return 0


def cmd_add(config_arg: str | None = None) -> int:
    """Add another database connection (creating the config if none exists yet)."""
    try:
        path = _existing_config(config_arg) or scope_to_path(_ask_scope())
        return 0 if _add_connection_interactive(path) else 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was saved.")
        return 130


def cmd_clients(config_arg: str | None = None, remove: bool = False) -> int:
    """Inject db-conn-mcp into chosen MCP clients, or uninject with ``remove=True``."""
    try:
        if remove:
            return _uninject_interactive()
        path = _existing_config(config_arg)
        if path is None:
            print("No configuration found. Run `db-conn-mcp setup` first.")
            return 1
        _inject_clients_interactive(path)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130


def _uninject_interactive() -> int:
    """List clients that currently have db-conn-mcp and remove it from the chosen ones."""
    injected = [s for s in detected_clients() if is_injected(s)]
    if not injected:
        print("db-conn-mcp is not injected into any detected MCP client.")
        return 0
    print("Clients with db-conn-mcp injected:")
    for i, spec in enumerate(injected, 1):
        print(f"  {i}. {spec.label} [{spec.fmt}]: {spec.path}")
    raw = input("Remove db-conn-mcp from which? (e.g. 1,3 or 'all'; Enter to skip): ")
    chosen = [injected[i] for i in parse_client_selection(raw, len(injected))]
    if not chosen:
        print("Nothing removed.")
        return 0
    for spec in chosen:
        _uninject_from_client(spec)
    return 0


def _uninject_from_client(spec: ClientSpec) -> None:
    """Remove the db-conn-mcp entry from one client's config, preserving the rest."""
    try:
        existing = json.loads(spec.path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    remove_entry(existing, spec.fmt, "db-conn-mcp")
    spec.path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"  Removed from {spec.label} config.")


def cmd_check(config_arg: str | None = None, name: str | None = None) -> int:
    """Probe connectivity for one database (or all) and print a sanitized result.

    Exit code: 0 if every probed database is reachable, 2 if any is unreachable.
    """
    path = _existing_config(config_arg)
    if path is None:
        print("No configuration found. Run `db-conn-mcp setup` first.")
        return 1
    try:
        results = asyncio.run(Handlers(path).check_database(name))
    except config.ConfigError as exc:
        print(exc)
        return 1
    all_ok = True
    for entry in results:
        if entry["status"] == "OK":
            print(f"  {entry['database']}: OK")
        else:
            all_ok = False
            print(f"  {entry['database']}: {entry.get('detail', entry['status'])}")
    return 0 if all_ok else 2


def cmd_remove(config_arg: str | None, name: str) -> int:
    """Remove a database connection by name."""
    path = _existing_config(config_arg)
    if path is None:
        print("No configuration found.")
        return 1
    cfg = config.load(str(path))
    if not any(c.name == name for c in cfg.connections):
        available = ", ".join(c.name for c in cfg.connections) or "(none)"
        print(f"No connection named {name!r}. Available: {available}.")
        return 1
    cfg.connections = [c for c in cfg.connections if c.name != name]
    config.save(cfg, path)
    print(f"Removed {name!r} from {path}.")
    return 0


def cmd_reset(config_arg: str | None = None) -> int:
    """Delete the whole ``connections.json`` (remove ALL connections) for a fresh slate."""
    path = _existing_config(config_arg)
    if path is None:
        print("No configuration found — already fresh.")
        return 0
    try:
        confirm = input(f"Delete ALL connections by removing {path}? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing deleted.")
        return 130
    if not confirm.startswith("y"):
        print("Cancelled. Nothing deleted.")
        return 0
    path.unlink()
    print(f"Removed all connections (deleted {path}). Run `db-conn-mcp setup` to start fresh.")
    return 0


def cmd_yolo(config_arg: str | None, name: str, state: str) -> int:
    """Enable/disable yolo (write-consent bypass) for one database, persisted."""
    path = _existing_config(config_arg)
    if path is None:
        print("No configuration found.")
        return 1
    enabled = state == "on"
    try:
        updated = config.set_yolo(path, name, enabled)
    except config.ConfigError as exc:
        print(exc)
        return 1
    print(f"Set yolo={'on' if enabled else 'off'} for {name!r} (persisted to {path}).")
    if enabled and config.get(updated, name).mode != "write":
        print("Note: yolo only affects write-mode databases; this one is read-only.")
    return 0


_COMMANDS = {
    "setup": lambda a: run_setup_wizard(a.config),
    "status": lambda a: cmd_status(a.config),
    "add": lambda a: cmd_add(a.config),
    "clients": lambda a: cmd_clients(a.config, remove=a.remove),
    "check": lambda a: cmd_check(a.config, a.name),
    "remove": lambda a: cmd_remove(a.config, a.name),
    "reset": lambda a: cmd_reset(a.config),
    "yolo": lambda a: cmd_yolo(a.config, a.name, a.state),
}


def _explain_stdio_misuse() -> int:
    """Explain the bare stdio server to a human who launched it from a terminal.

    The stdio MCP server reads JSON-RPC from stdin and would block forever with no
    output — indistinguishable from a hang — when there is nothing on the other end.
    A real MCP client launches it with stdin wired to a pipe (not a TTY), so we only
    reach here when a person ran ``db-conn-mcp`` by hand. Point them at the commands
    they almost certainly meant. Guidance goes to stderr so it never pollutes the
    stdout protocol channel.
    """
    print(
        "db-conn-mcp speaks the MCP protocol over stdio and is meant to be launched by "
        "an MCP client (Claude Desktop, Cursor, VS Code, ...), not run directly in a "
        "terminal — doing so just blocks waiting for JSON-RPC on stdin.\n\n"
        "Did you mean one of these?\n"
        "  db-conn-mcp setup     Guided setup (register a database, wire up clients)\n"
        "  db-conn-mcp status    Show configured databases and client injection state\n"
        "  db-conn-mcp --help    See all commands\n\n"
        "To run the server yourself anyway, pipe MCP JSON-RPC into it, or use "
        "`--transport http` for an HTTP/SSE server.\n\n"
        f"⭐ Find db-conn-mcp useful? A star helps others find it: {REPO_URL}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (declared in ``pyproject.toml`` as ``db-conn-mcp``)."""
    args = build_parser().parse_args(argv)
    args.config = getattr(args, "config", None)  # SUPPRESS means it may be absent
    if args.command in _COMMANDS:
        return _COMMANDS[args.command](args)
    # No subcommand -> run the server. An interactive terminal means a human ran it by
    # hand; the stdio server would hang silently on stdin, so explain instead (http is a
    # legitimate long-running foreground server and never reads stdin).
    if args.transport == "stdio" and sys.stdin.isatty():
        return _explain_stdio_misuse()
    try:
        server.run(transport=args.transport, config_path=args.config)
    except KeyboardInterrupt:
        return 130
    except config.ConfigError as exc:
        # Server-path diagnostics go to stderr — stdout is the MCP protocol channel.
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
