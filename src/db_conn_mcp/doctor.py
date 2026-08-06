"""The doctor engine: whole-setup diagnostics behind one check registry (issue #12).

Each check is a function taking a :class:`CheckContext` and returning
``list[Finding]`` (sync or async). :func:`run_checks` runs them all and NEVER
raises: a crashing check becomes a ``fail`` finding naming only the exception
*type* — driver messages can embed hosts, so the message is never copied (Rule 6).

Findings may contain file paths, port numbers, PIDs, version strings, and client
labels — never DSNs, hosts, usernames, or passwords.
"""

import difflib
import inspect
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import ValidationError

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
        mode = path.stat().st_mode & 0o777
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
    inside = subprocess.run(
        [git, "-C", parent, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        findings.append(finding(name, "ok", f"{path.name} is not inside a git work tree"))
        return findings
    ignored = subprocess.run(
        [git, "-C", parent, "check-ignore", "-q", path.name], capture_output=True, check=False
    )
    if ignored.returncode == 0:
        findings.append(
            finding(name, "ok", f"{path.name} is inside a git work tree but git-ignored")
        )
    else:
        findings.append(
            finding(
                name,
                "fail",
                f"{path.name} is inside a git work tree and NOT git-ignored — plaintext DSNs "
                "are committable; add it to .gitignore",
                "fix_config",
            )
        )
    return findings


#: The registry: (check_name, callable(ctx) -> list[Finding] | awaitable of it).
#: Order is presentation order in the CLI.
_CHECKS: list[tuple[str, Callable]] = [
    ("config_schema", check_config_schema),
    ("secrets_exposure", check_secrets_exposure),
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
