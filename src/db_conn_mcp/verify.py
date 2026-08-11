"""Live MCP verification: spawn a server binary and interrogate it over real MCP.

The engine is front-end-agnostic (like ``doctor.py``) and uses the ``mcp`` SDK's
*client* side — the same protocol library real MCP clients embed — so a passing
verdict is evidence about real MCP, never a home-rolled approximation. It always
spawns a separate process; it never answers from this process's own imports.
"""

import asyncio
from typing import Literal, TypedDict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import __version__
from .clients import ClientSpec, injected_launch

#: The tool count the smoke test pins; a live server must agree.
EXPECTED_TOOL_COUNT = 23

Verdict = Literal[
    "answers", "launch_failed", "handshake_failed", "wrong_tool_count", "timeout", "port_in_use"
]


class VerifyResult(TypedDict):
    """One verification outcome, in the fixed vocabulary the GUI may render (Rule 6)."""

    verdict: Verdict
    detail: str
    suggested_action: str
    command: str
    args: list[str]
    server_name: str | None
    server_version: str | None
    gui_version: str
    stale: bool
    tool_count: int | None
    instructions: str | None
    list_databases_text: str | None


def _base(command: str, args: list[str]) -> VerifyResult:
    """A result skeleton; every path fills verdict/detail on top of this."""
    return {
        "verdict": "handshake_failed",
        "detail": "",
        "suggested_action": "",
        "command": command,
        "args": list(args),
        "server_name": None,
        "server_version": None,
        "gui_version": __version__,
        "stale": False,
        "tool_count": None,
        "instructions": None,
        "list_databases_text": None,
    }


async def verify_stdio(command: str, args: list[str], timeout: float = 20.0) -> VerifyResult:
    """Spawn ``command args`` over stdio and complete initialize → tools/list → list_databases.

    Every step shares one hard timeout so a hung binary cannot wedge the caller.
    """
    result = _base(command, args)
    try:
        async with asyncio.timeout(timeout):
            params = StdioServerParameters(command=command, args=list(args))
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                init = await session.initialize()
                result["server_name"] = init.serverInfo.name
                result["server_version"] = init.serverInfo.version
                result["instructions"] = init.instructions
                result["stale"] = init.serverInfo.version != __version__
                tools = await session.list_tools()
                result["tool_count"] = len(tools.tools)
                dbs = await session.call_tool("list_databases", {})
                if dbs.content and hasattr(dbs.content[0], "text"):
                    result["list_databases_text"] = dbs.content[0].text
    except TimeoutError:
        result["verdict"] = "timeout"
        result["detail"] = f"no complete MCP conversation within {timeout:.0f}s"
        result["suggested_action"] = "run the command by hand in a terminal and watch stderr"
        return result
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        result["verdict"] = "launch_failed"
        result["detail"] = f"could not start the process ({type(exc).__name__})"
        result["suggested_action"] = "check the command path in this client's config"
        return result
    except Exception as exc:  # noqa: BLE001 — verdicts, not tracebacks (Rule 6)
        result["verdict"] = "handshake_failed"
        result["detail"] = f"process started but MCP failed ({type(exc).__name__})"
        result["suggested_action"] = "the binary may be broken or not db-conn-mcp; reinstall"
        return result
    if result["tool_count"] != EXPECTED_TOOL_COUNT:
        result["verdict"] = "wrong_tool_count"
        result["detail"] = f"expected {EXPECTED_TOOL_COUNT} tools, got {result['tool_count']}"
        result["suggested_action"] = "upgrade: pipx upgrade db-conn-mcp"
        return result
    result["verdict"] = "answers"
    result["detail"] = "handshake, tools/list and list_databases all answered"
    if result["stale"]:
        result["suggested_action"] = "version differs from this install — pipx upgrade db-conn-mcp"
    return result


async def verify_client(spec: ClientSpec, timeout: float = 20.0) -> VerifyResult:
    """Verify the exact command+args this client's config launches."""
    launch = injected_launch(spec)
    if launch is None:
        result = _base("", [])
        result["verdict"] = "launch_failed"
        result["detail"] = "no db-conn-mcp entry found in this client's config"
        result["suggested_action"] = "inject first (Clients section, or `db-conn-mcp clients`)"
        return result
    command, args = launch
    return await verify_stdio(command, args, timeout=timeout)
