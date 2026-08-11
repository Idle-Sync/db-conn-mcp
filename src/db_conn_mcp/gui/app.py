"""The local GUI web app: a token-guarded Starlette app bound to 127.0.0.1.

Security model (spec: 2026-08-11 GUI design): loopback bind, Host-header
allowlist (DNS-rebinding defence), a per-start bearer token checked on every
request including ``/``, no CORS ever, CSP ``default-src 'self'`` on every
response. API responses use a fixed field vocabulary — a DSN cannot appear in
any response by construction (Rule 6).
"""

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from .. import clients as clients_mod
from ..verify import verify_client, verify_http

GUI_PORT = 31415
TOKEN_HEADER = "X-GUI-Token"
_ALLOWED_HOSTS = frozenset({f"127.0.0.1:{GUI_PORT}", f"localhost:{GUI_PORT}"})
_CSP = "default-src 'self'"


def token_path() -> Path:
    """Where the current GUI token is persisted (user-only file, 0600)."""
    return Path.home() / ".db-conn-mcp" / "gui-token"


def _static_dir() -> Path:
    """The packaged static assets (real directory in editable and wheel installs)."""
    return Path(str(resources.files("db_conn_mcp.gui") / "static"))


def _forbidden() -> JSONResponse:
    """A sanitized 403 that still carries the CSP — no reason is disclosed."""
    response = JSONResponse({"error": "forbidden"}, status_code=403)
    response.headers["content-security-policy"] = _CSP
    return response


class _Guard(BaseHTTPMiddleware):
    """Host allowlist + constant-time token check on every request, then CSP."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token.encode("utf-8", "surrogateescape")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject anything not loopback-addressed or not carrying the token."""
        if request.headers.get("host", "") not in _ALLOWED_HOSTS:
            return _forbidden()
        supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
        # Compared as bytes: compare_digest raises TypeError on non-ASCII *str*, and a
        # crash inside the security boundary would escape to ServerErrorMiddleware —
        # a 500 with no CSP and a traceback on the terminal. surrogateescape keeps any
        # undecodable byte sequence comparable instead of raising a second time.
        if not hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), self._token):
            return _forbidden()
        response = await call_next(request)
        response.headers["content-security-policy"] = _CSP
        return response


def create_app(token: str, config_arg: str | None = None) -> Starlette:
    """Build the GUI app. ``config_arg`` is the CLI's --config passthrough.

    Raises :class:`ValueError` on an empty token: an empty credential compares equal
    to a request that supplies none, so such an app would serve everything to anyone.
    The boundary refuses to be built unarmed rather than trusting its caller.
    """
    if not token:
        raise ValueError("A non-empty GUI token is required.")

    # Per-app, not module-global: one verification subprocess at a time, so two
    # browser tabs cannot race spawns. A verification's wall clock is its own
    # timeout (~20-30s) plus a couple of seconds of client cleanup; the routes
    # just await the engine — do not wrap them in a shorter HTTP timeout.
    lock = asyncio.Lock()

    async def index(request: Request) -> FileResponse:
        """Serve the dashboard page itself (still behind the guard)."""
        return FileResponse(_static_dir() / "index.html")

    async def summary(request: Request) -> JSONResponse:
        """Report the app identity, whether a config was found, and client state."""
        from .. import config

        try:
            config.resolve_path(config_arg)
            config_found = True
        except config.ConfigError:
            config_found = False
        rows = []
        for spec in clients_mod.detected_clients():
            readable = clients_mod.config_readable(spec)
            launch = clients_mod.injected_launch(spec) if readable else None
            rows.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "unreadable": not readable,
                    "injected": clients_mod.is_injected(spec) if readable else False,
                    "command": launch[0] if launch else None,
                    "args": launch[1] if launch else None,
                }
            )
        return JSONResponse(
            {
                "app": "db-conn-mcp-gui",
                "version": __version__,
                "config_found": config_found,
                "clients": rows,
            }
        )

    async def verify_client_route(request: Request) -> JSONResponse:
        """Verify the launch line one detected client actually stores, over real MCP."""
        key = request.path_params["key"]
        spec = next((s for s in clients_mod.detected_clients() if s.key == key), None)
        if spec is None:
            return JSONResponse({"error": "unknown or undetected client"}, status_code=404)
        async with lock:
            result = await verify_client(spec)
        return JSONResponse(result)

    async def verify_http_route(request: Request) -> JSONResponse:
        """Verify this install over the HTTP (SSE) transport — one global check."""
        from .. import config
        from ..cli import server_launch

        try:
            path = config.resolve_path(config_arg)
        except config.ConfigError:
            return JSONResponse({"error": "no configuration found"}, status_code=409)
        command, args = server_launch(path)
        async with lock:
            result = await verify_http(command, args)
        return JSONResponse(result)

    routes = [
        Route("/", index),
        Route("/api/summary", summary),
        Route("/api/verify/client/{key}", verify_client_route, methods=["POST"]),
        Route("/api/verify/http", verify_http_route, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(_static_dir())), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(_Guard, token=token)])
