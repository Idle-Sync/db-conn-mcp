"""Command-line entry point: argparse wiring for the server and the setup wizard.

Two responsibilities, no DB knowledge:
  * ``db-conn-mcp [--transport stdio|http] [--config PATH]`` — launch the server.
  * ``db-conn-mcp setup`` — interactive wizard to register the first DB and
    (optionally) inject the server into detected MCP client configs. Each client
    carries its own config path, entry format and file syntax (``mcpServers`` for
    Claude Desktop/Cursor/Agy/Windsurf/Claude Code/Cline, ``servers`` for VS Code,
    ``context_servers`` for Zed, ``mcp_servers`` in TOML for Codex) via
    :class:`db_conn_mcp.clients.ClientSpec` — those helpers live in ``clients.py``
    and are re-imported here.

The wizard's *logic* (path resolution, DB registration, config injection) lives in
pure helpers so it stays testable; the interactive loop is a thin shell over them.
Only the connection *name* is ever echoed — never the DSN (Rule 6).
"""

import argparse
import asyncio
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

from . import __commit__, __version__, config, doctor, server
from .clients import (
    ClientConfigError,
    ClientSpec,
    agent_config_paths,  # noqa: F401  re-exported for back-compat; unused in this module
    client_specs,
    config_readable,
    detected_clients,
    inject_entry,
    is_injected,
    read_config,
    remove_entry,
    write_config,
)
from .dialects.registry import dialect_for
from .handlers import Handlers
from .models import Config, Connection

Scope = Literal["global", "repo"]

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
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Do not host the local dashboard on 127.0.0.1:31415 alongside the server.",
    )

    sub = parser.add_subparsers(dest="command")

    def _add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    _add("gui", "Open the local dashboard (starts it if not already running).")
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
    p_doctor = _add(
        "doctor", "Diagnose the whole setup: versions, processes, config, secrets, clients, DBs."
    )
    p_doctor.add_argument(
        "--offline",
        action="store_true",
        help="Skip the PyPI version lookup; database probes still run.",
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
        if not config_readable(spec):
            marker = " [config unreadable — fix by hand]"
        else:
            marker = " [already injected]" if is_injected(spec) else ""
        print(f"  {i}. {spec.label} [{spec.fmt}]{marker}: {spec.path}")
    raw = input("Inject db-conn-mcp into which? (e.g. 1,3 or 'all'; Enter to skip): ")
    chosen = [detected[i] for i in parse_client_selection(raw, len(detected))]
    if not chosen:
        print("No clients selected.")
    return chosen


def _inject_into_client(spec: ClientSpec, command: str, args: list[str]) -> None:
    """Read-merge-write a server entry into one client's config, in its own format.

    A config that exists but does not parse is left untouched: overwriting it would
    destroy the user's other servers, comments and settings.
    """
    try:
        existing = read_config(spec)
    except ClientConfigError as exc:
        print(f"  Skipped {spec.label}: {exc}")
        print("    Fix that file by hand, then re-run `db-conn-mcp clients`.")
        return
    updated = inject_entry(existing, spec.fmt, "db-conn-mcp", command, args)
    write_config(spec, updated)
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
        if not config_readable(spec):
            state = "config unreadable"
        else:
            state = "injected" if is_injected(spec) else "not injected"
        print(f"  - {spec.label}: {state}")


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
    """Print the star nudge and the dashboard tip after setup finishes successfully.

    The honest version of "grow the repo": ask at the moment the tool just
    worked for someone. Never gates, blocks, or touches their GitHub account.
    The tip rides along here — right where a first-time user has just learned
    the CLI and would otherwise never discover the dashboard. Plain ASCII: this
    line also has to survive a cp1252 Windows console.
    """
    print(f"⭐ Find db-conn-mcp useful? A star helps others find it: {REPO_URL}")
    print("Tip: `db-conn-mcp gui` opens a browser dashboard for setup and verification.")


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
    detected = detected_clients()
    # Reported but never numbered: db-conn-mcp may well be in a config we could not parse,
    # yet rewriting that file would destroy it. is_injected is False for those, so the
    # numbered list below stays exactly the clients we can safely rewrite.
    unreadable = [s for s in detected if not config_readable(s)]
    if unreadable:
        print("Not checked — config unreadable, fix by hand then re-run:")
        for spec in unreadable:
            print(f"  - {spec.label} [{spec.fmt}]: {spec.path}")
    injected = [s for s in detected if is_injected(s)]
    if not injected:
        # Only hedge when there is something we could not check: an unreadable config may
        # well hold an entry, so claiming "any detected client" would overclaim.
        scope = "any client it could check" if unreadable else "any detected MCP client"
        print(f"db-conn-mcp is not injected into {scope}.")
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
        existing = read_config(spec)
    # Unreachable from `clients --remove`, which only offers configs that parsed
    # (is_injected is False for the rest). Kept as the guard for any other caller.
    except ClientConfigError as exc:
        print(f"  Skipped {spec.label}: {exc}")
        return
    remove_entry(existing, spec.fmt, "db-conn-mcp")
    write_config(spec, existing)
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
            port_note = f" (active_port={entry['active_port']})" if "active_port" in entry else ""
            print(f"  {entry['database']}: OK{port_note}")
        else:
            all_ok = False
            print(f"  {entry['database']}: {entry.get('detail', entry['status'])}")
    return 0 if all_ok else 2


#: CLI icons per finding status (the MCP tool returns the raw status strings).
_DOCTOR_ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skipped": "⏭"}

#: ASCII stand-ins for streams that cannot encode the icons (e.g. a redirected
#: Windows stdout, which falls back to cp1252). A diagnostic command must never be
#: the thing that crashes.
_DOCTOR_ASCII = {"ok": "[ok]", "warn": "[warn]", "fail": "[FAIL]", "skipped": "[skip]"}


def _status_markers() -> dict[str, str]:
    """Return the icons, or the ASCII fallbacks when stdout cannot encode them."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "".join(_DOCTOR_ICONS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _DOCTOR_ASCII
    return _DOCTOR_ICONS


def cmd_doctor(config_arg: str | None = None, offline: bool = False) -> int:
    """Run every diagnostic and print one line per finding.

    Exit code 0 only if no finding is a ``fail`` (warnings and skips don't fail
    the run — per issue #12).
    """
    path = _existing_config(config_arg)
    findings = asyncio.run(doctor.run_checks(path, offline=offline))
    markers = _status_markers()
    for entry in findings:
        print(f"{markers[entry['status']]}  {entry['check']}: {entry['detail']}")
    failed = sum(1 for entry in findings if entry["status"] == "fail")
    if failed:
        print(f"\n{failed} problem(s) found.")
        return 2
    print("\nNo blocking problems found.")
    return 0


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


def cmd_gui(config_arg: str | None = None) -> int:
    """Show the dashboard: reuse the one a running server already hosts, else start it.

    The imports are local so ``import db_conn_mcp.cli`` never drags in starlette,
    uvicorn and httpx — the CLI's other commands (and the MCP server's own launch
    path) have no use for them.
    """
    import webbrowser

    import httpx

    from .gui.app import GUI_PORT, TOKEN_HEADER, run_standalone, token_path

    url = f"http://127.0.0.1:{GUI_PORT}"
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        try:
            # The token goes in a HEADER, never the query string: a URL is what ends
            # up in logs and process listings (Rule 6). It is only ever put in a URL
            # for the browser hand-off below, which is the one consumer that needs it.
            r = httpx.get(f"{url}/api/summary", headers={TOKEN_HEADER: token}, timeout=2.0)
            # Anything but our own summary means the port is somebody else's.
            if r.status_code == 200 and r.json().get("app") == "db-conn-mcp-gui":
                webbrowser.open(f"{url}/?token={token}")
                print(f"Dashboard already running - opened {url}")
                return 0
        except httpx.HTTPError:
            pass
    code = run_standalone(config_arg)
    if code == 2:
        print(f"Port {GUI_PORT} is in use by something that is not the db-conn-mcp dashboard.")
        print("Free the port, then re-run `db-conn-mcp gui`.")
    return code


_COMMANDS = {
    "gui": lambda a: cmd_gui(a.config),
    "setup": lambda a: run_setup_wizard(a.config),
    "status": lambda a: cmd_status(a.config),
    "add": lambda a: cmd_add(a.config),
    "clients": lambda a: cmd_clients(a.config, remove=a.remove),
    "check": lambda a: cmd_check(a.config, a.name),
    "doctor": lambda a: cmd_doctor(a.config, offline=a.offline),
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
        server.run(transport=args.transport, config_path=args.config, gui=not args.no_gui)
    except KeyboardInterrupt:
        return 130
    except config.ConfigError as exc:
        # Server-path diagnostics go to stderr — stdout is the MCP protocol channel.
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
