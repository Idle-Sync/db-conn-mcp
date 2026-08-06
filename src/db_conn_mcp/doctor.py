"""The doctor engine: whole-setup diagnostics behind one check registry (issue #12).

Each check is a function taking a :class:`CheckContext` and returning
``list[Finding]`` (sync or async). :func:`run_checks` runs them all and NEVER
raises: a crashing check becomes a ``fail`` finding naming only the exception
*type* — driver messages can embed hosts, so the message is never copied (Rule 6).

Findings may contain file paths, port numbers, PIDs, version strings, and client
labels — never DSNs, hosts, usernames, or passwords.
"""

import difflib
import importlib.metadata
import inspect
import json
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import __version__, clients, config
from .dialects.registry import dialect_for
from .handlers import Handlers
from .models import Config, Connection

Status = Literal["ok", "warn", "fail", "skipped"]


class Finding(TypedDict):
    """One diagnostic result row (agent-facing shape from issue #12)."""

    check: str
    status: Status
    detail: str
    suggested_action: str


def finding(check: str, status: Status, detail: str, suggested_action: str = "none") -> Finding:
    """Build a :class:`Finding` (keeps call sites one-liners)."""
    return {
        "check": check,
        "status": status,
        "detail": detail,
        "suggested_action": suggested_action,
    }


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may need; ``config_path`` is None when no config exists."""

    config_path: Path | None
    offline: bool


def _installed_at() -> float | None:
    """Best-effort install timestamp: mtime of the dist's RECORD (written at install).

    Wheel members keep archive timestamps, but RECORD is generated during install,
    so its mtime is the true install/upgrade moment. None for editable/source
    installs where no dist-info can be located.
    """
    try:
        dist = importlib.metadata.distribution("db-conn-mcp")
        dist_path = getattr(dist, "_path", None)  # dist-info dir on the std backend
        if dist_path is None:
            return None
        record = Path(dist_path) / "RECORD"
        target = record if record.is_file() else Path(dist_path)
        return target.stat().st_mtime
    except Exception:  # noqa: BLE001 — any metadata oddity just disables the check
        return None


def check_process_staleness(ctx: CheckContext) -> list[Finding]:
    """Use case 1: a client's long-lived server process still running an old version."""
    name = "process_staleness"
    try:
        import psutil
    except ImportError:
        return [
            finding(
                name,
                "skipped",
                "psutil not installed — cannot inspect running processes "
                "(fix: pipx inject db-conn-mcp psutil)",
            )
        ]
    installed_at = _installed_at()
    if installed_at is None:
        return [
            finding(
                name,
                "skipped",
                "could not determine when db-conn-mcp was installed (editable/source install?)",
            )
        ]
    findings: list[Finding] = []
    own_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            info = proc.info
            cmdline = " ".join(info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if info["pid"] == own_pid:
            continue
        if "db-conn-mcp" not in cmdline and "db_conn_mcp" not in cmdline:
            continue
        if info["create_time"] < installed_at:
            findings.append(
                finding(
                    name,
                    "warn",
                    f"a db-conn-mcp process (pid {info['pid']}) started before "
                    f"v{__version__} was installed — restart/reconnect that MCP client "
                    "to load the new version",
                    "reconnect_client",
                )
            )
    if not findings:
        findings.append(finding(name, "ok", "no stale db-conn-mcp processes found"))
    return findings


#: Cache-bypassed version lookup (use case 2: pip's cached index hid a fresh release).
PYPI_JSON_URL = "https://pypi.org/pypi/db-conn-mcp/json"


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse '0.5.2' -> (0, 5, 2); None for anything non-numeric (pre-releases)."""
    parts = version.split(".")
    if parts and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return None


def check_pypi_latest(ctx: CheckContext) -> list[Finding]:
    """Use case 2: is a newer release published that a cached index would hide?"""
    name = "pypi_latest"
    if ctx.offline:
        return [finding(name, "skipped", "offline mode — PyPI version lookup skipped")]
    request = urllib.request.Request(
        PYPI_JSON_URL, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            latest = str(json.load(response)["info"]["version"])
    except Exception:  # noqa: BLE001 — no internet is not a broken setup
        return [finding(name, "skipped", "PyPI could not be reached — freshness not checked")]
    installed, remote = _version_tuple(__version__), _version_tuple(latest)
    if installed is not None and remote is not None and remote > installed:
        return [
            finding(
                name,
                "warn",
                f"v{latest} is on PyPI (installed: v{__version__}) — run: "
                "pipx upgrade db-conn-mcp --pip-args='--no-cache-dir'",
                "upgrade_package",
            )
        ]
    return [finding(name, "ok", f"v{__version__} is the latest published version")]


_TOP_LEVEL_KEYS = set(Config.model_fields)
_CONNECTION_KEYS = set(Connection.model_fields)


def check_config_schema(ctx: CheckContext) -> list[Finding]:
    """Use case 4: silently-ignored keys, typos, and wrong value types.

    Values are NEVER echoed (a value could be a DSN) — only key names, pydantic's
    value-free messages, and did-you-mean hints for near-miss key names.
    """
    name = "config_schema"
    if ctx.config_path is None:
        return [finding(name, "skipped", "no configuration found — run `db-conn-mcp setup`")]
    try:
        raw = json.loads(ctx.config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [
            finding(
                name,
                "fail",
                f"connections.json is not valid JSON: {exc.msg} (line {exc.lineno})",
                "fix_config",
            )
        ]
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError — catch it explicitly
        # so a non-UTF-8 file is diagnosed here instead of crashing the check.
        return [
            finding(
                name,
                "fail",
                "connections.json exists but could not be read (not readable, or not UTF-8)",
                "fix_config",
            )
        ]

    findings: list[Finding] = []

    def _flag_unknown(keys: dict[str, object], known: set[str], where: str) -> None:
        """Warn about every key in ``keys`` that pydantic would silently ignore."""
        for key in keys:
            if key in known:
                continue
            close = difflib.get_close_matches(key, known, n=1)
            hint = f" — did you mean {close[0]!r}?" if close else ""
            detail = f"unrecognized key {key!r} in {where}{hint}"
            findings.append(
                finding(name, "warn", f"{detail} (unknown keys are silently ignored)", "fix_config")
            )

    if isinstance(raw, dict):
        _flag_unknown(raw, _TOP_LEVEL_KEYS, "the top-level object")
        connections = raw.get("connections")
        if isinstance(connections, list):
            for i, entry in enumerate(connections):
                if isinstance(entry, dict):
                    _flag_unknown(entry, _CONNECTION_KEYS, f"connection #{i + 1}")

    try:
        Config.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors(include_input=False, include_url=False):
            loc = ".".join(str(p) for p in err["loc"]) or "the top-level document"
            findings.append(
                finding(name, "fail", f"invalid value for {loc}: {err['msg']}", "fix_config")
            )

    if not findings:
        findings.append(finding(name, "ok", "connections.json is valid — no unknown keys"))
    return findings


def check_secrets_exposure(ctx: CheckContext) -> list[Finding]:
    """Use case 5: is the plaintext-DSN config file exposed (mode bits, git)?"""
    name = "secrets_exposure"
    if ctx.config_path is None:
        return [finding(name, "skipped", "no configuration found — nothing to check")]
    path = ctx.config_path
    findings: list[Finding] = []

    if os.name == "posix":
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            # Deleted or unreadable between discovery and now — diagnose, don't crash.
            findings.append(finding(name, "skipped", "config file could not be stat'd"))
        else:
            if mode & 0o077:
                findings.append(
                    finding(
                        name,
                        "warn",
                        f"{path.name} is readable by other users (mode {mode:03o}) — "
                        f"run: chmod 600 {path}",
                        "fix_permissions",
                    )
                )
            else:
                findings.append(finding(name, "ok", f"{path.name} permissions are owner-only"))
    else:
        findings.append(
            finding(
                name,
                "skipped",
                "file-permission check is POSIX-only (Windows ACLs not analyzed)",
            )
        )

    git = shutil.which("git")
    if git is None:
        findings.append(
            finding(name, "skipped", "git not found — cannot verify the config is git-ignored")
        )
        return findings
    parent = str(path.parent)
    timed_out = finding(name, "skipped", "git did not respond within 5s — git exposure not checked")
    try:
        inside = subprocess.run(
            [git, "-C", parent, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        findings.append(timed_out)
        return findings
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        findings.append(finding(name, "ok", f"{path.name} is not inside a git work tree"))
        return findings
    try:
        ignored = subprocess.run(
            [git, "-C", parent, "check-ignore", "-q", path.name],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        findings.append(timed_out)
        return findings
    if ignored.returncode == 0:
        findings.append(
            finding(name, "ok", f"{path.name} is inside a git work tree but git-ignored")
        )
    elif ignored.returncode == 1:
        findings.append(
            finding(
                name,
                "fail",
                f"{path.name} is inside a git work tree and NOT git-ignored — plaintext DSNs "
                "are committable; add it to .gitignore",
                "fix_config",
            )
        )
    else:
        # rc 128 = corrupt repo, dubious ownership, etc. An error is NOT evidence of
        # exposure — never raise a false security alarm on it.
        findings.append(finding(name, "skipped", "git could not evaluate ignore rules"))
    return findings


def check_client_paths(ctx: CheckContext) -> list[Finding]:
    """Use case 6: injected client entries whose command path no longer exists."""
    name = "client_paths"
    findings: list[Finding] = []
    for spec in clients.detected_clients():
        if not clients.is_injected(spec):
            continue
        command = clients.injected_command(spec)
        if command is None:
            findings.append(
                finding(
                    name,
                    "warn",
                    f"{spec.label}: db-conn-mcp entry has no readable command — "
                    "run `db-conn-mcp clients` to re-inject",
                    "repair_client_config",
                )
            )
        elif Path(command).is_file() or shutil.which(command):
            findings.append(finding(name, "ok", f"{spec.label}: injected, command exists"))
        else:
            findings.append(
                finding(
                    name,
                    "warn",
                    f"{spec.label}: db-conn-mcp entry points at a command that no longer "
                    "exists — run `db-conn-mcp clients` to re-inject",
                    "repair_client_config",
                )
            )
    if not findings:
        findings.append(finding(name, "ok", "no detected MCP client has db-conn-mcp injected"))
    return findings


def _auth_failed_with_fallbacks(entry: dict, conn: Connection) -> bool:
    """The port-identity trigger: auth failed on the primary AND fallbacks exist.

    ``failed_port`` means the rejection came from a probed *fallback* — that port
    demonstrably speaks the protocol, so "swap the primary port to it" would be both
    a false statement and a no-op fix. Only a primary auth failure qualifies.
    """
    return (
        entry.get("category") == "AUTH_FAILED"
        and entry.get("failed_port") is None
        and bool(conn.fallback_ports)
    )


async def _probe_port_identity(conn: Connection) -> list[Finding]:
    """Use case 3: is a *different* server answering the primary port?

    Credential-free probes only (`Dialect.probe_listener`); findings name port
    numbers, never the host (Rule 6).
    """
    try:
        parts = urlsplit(conn.dsn)
        host, primary_port = parts.hostname, parts.port
    except ValueError:
        return []
    if host is None:
        return []
    dialect = dialect_for(conn.dsn)
    findings: list[Finding] = []
    for port in conn.fallback_ports or []:
        if port == primary_port:
            continue
        if await dialect.probe_listener(host, port):
            findings.append(
                finding(
                    "port_identity",
                    "warn",
                    f"{conn.name}: auth failed on the primary port, but port {port} also has "
                    "a matching database server listening — if your target moved (e.g. a "
                    f"tunnel), swap the DSN's primary port to {port}",
                    "swap_primary_port",
                )
            )
    return findings


async def check_connectivity(ctx: CheckContext) -> list[Finding]:
    """Per-database reachability (sanitized), plus the port-identity special case."""
    name = "connectivity"
    if ctx.config_path is None:
        return [finding(name, "skipped", "no configuration found — no databases to probe")]
    try:
        cfg = config.load(str(ctx.config_path))
        results = await Handlers(ctx.config_path).check_database(None)
    except config.ConfigError:
        return [
            finding(name, "skipped", "connections.json could not be loaded — see config_schema")
        ]
    findings: list[Finding] = []
    for entry in results:
        db_name = entry["database"]
        if entry["status"] == "OK":
            port_note = f" (active_port={entry['active_port']})" if "active_port" in entry else ""
            findings.append(finding(name, "ok", f"{db_name}: reachable{port_note}"))
            continue
        findings.append(finding(name, "fail", f"{db_name}: {entry.get('detail', entry['status'])}"))
        conn = config.get(cfg, db_name)
        if _auth_failed_with_fallbacks(entry, conn):
            findings.extend(await _probe_port_identity(conn))
    return findings


#: The registry: (check_name, callable(ctx) -> list[Finding] | awaitable of it).
#: Order is presentation order in the CLI.
_CHECKS: list[tuple[str, Callable]] = [
    ("process_staleness", check_process_staleness),
    ("pypi_latest", check_pypi_latest),
    ("config_schema", check_config_schema),
    ("secrets_exposure", check_secrets_exposure),
    ("client_paths", check_client_paths),
    ("connectivity", check_connectivity),
]


async def run_checks(config_path: Path | None, *, offline: bool = False) -> list[Finding]:
    """Run every registered check; a crashing check reports ``fail``, never raises."""
    ctx = CheckContext(config_path=Path(config_path) if config_path else None, offline=offline)
    findings: list[Finding] = []
    for name, check in _CHECKS:
        try:
            result = check(ctx)
            if inspect.isawaitable(result):
                result = await result
            findings.extend(result)
        except Exception as exc:  # noqa: BLE001 — the engine must never crash the caller
            detail = f"check crashed with {type(exc).__name__} — please report this"
            findings.append(finding(name, "fail", detail))
    return findings
