"""The doctor engine: whole-setup diagnostics behind one check registry (issue #12).

Each check is a function taking a :class:`CheckContext` and returning
``list[Finding]`` (sync or async). :func:`run_checks` runs them all and NEVER
raises: a crashing check becomes a ``fail`` finding naming only the exception
*type* — driver messages can embed hosts, so the message is never copied (Rule 6).

Findings may contain file paths, port numbers, PIDs, version strings, and client
labels — never DSNs, hosts, usernames, or passwords.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

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


#: The registry: (check_name, callable(ctx) -> list[Finding] | awaitable of it).
#: Order is presentation order in the CLI.
_CHECKS: list[tuple[str, Callable]] = []


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
