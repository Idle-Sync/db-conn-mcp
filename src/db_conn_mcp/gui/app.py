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

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from .. import clients as clients_mod
from .. import config as config_mod
from ..dialects.registry import dialect_for
from ..doctor import run_checks
from ..handlers import Handlers
from ..models import Config, Connection
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


def _no_config() -> JSONResponse:
    """The fixed 409 for "there is no usable connections.json" — no path echo."""
    return JSONResponse({"error": "no configuration found"}, status_code=409)


def _unknown_client() -> JSONResponse:
    """The fixed 404 for a client key that is not a detected client — no key echo."""
    return JSONResponse({"error": "unknown or undetected client"}, status_code=404)


def _unreadable_client() -> JSONResponse:
    """The fixed 409 for a client config that will not parse.

    Rule 6: the parse failure is described by category only. The file's contents —
    which routinely hold API keys for other MCP servers — never reach a response.
    """
    return JSONResponse(
        {"error": "that client's config could not be parsed; fix it by hand"},
        status_code=409,
    )


def _no_such_connection() -> JSONResponse:
    """The fixed 404 for an unknown connection name — the name is not echoed."""
    return JSONResponse({"error": "no such connection"}, status_code=404)


def _invalid_connection(fields: list[str]) -> JSONResponse:
    """The fixed 400 for a rejected connection body — field NAMES only.

    Rule 6: a pydantic :class:`ValidationError` stringifies the *input values* it
    rejected, and one of those inputs is the DSN. Its text must therefore never
    reach a response; only the names of the offending fields do.
    """
    return JSONResponse({"error": "invalid connection", "fields": fields}, status_code=400)


def _parse_connection(data: object) -> Connection | list[str]:
    """Build a :class:`Connection` from a request body, or list its bad fields.

    Returns the parsed connection on success and a sorted list of field names on
    failure (empty when the body is not even an object). The error text is dropped
    on purpose — see :func:`_invalid_connection`.
    """
    try:
        conn = Connection.model_validate(data)
    except ValidationError as exc:
        return sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
    try:
        dialect_for(conn.dsn)
    except ValueError:
        # Saving a scheme this build cannot dial would write a file that
        # ``config.load`` then rejects — refuse it here instead.
        return ["dsn"]
    return conn


def _load_or_empty(path: Path) -> Config | None:
    """Read the stored config; ``None`` means "exists but unusable".

    A file that is not there yet reads as an empty config so the first add can
    bootstrap it. A file that is there but fails to load must NOT read as empty:
    the save that follows would silently overwrite the user's connections.
    """
    if not path.is_file():
        return Config()
    try:
        return config_mod.load(str(path))
    except config_mod.ConfigError:
        return None


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
    verify_lock = asyncio.Lock()

    # A SECOND lock, deliberately not the verification one: it makes each
    # load-mutate-save of connections.json indivisible. Sharing verify_lock would
    # park every config edit behind a ~30s subprocess, so a user who hit Verify
    # would find the whole editor frozen. The two guard unrelated resources.
    config_lock = asyncio.Lock()

    async def index(request: Request) -> FileResponse:
        """Serve the dashboard page itself (still behind the guard)."""
        return FileResponse(_static_dir() / "index.html")

    async def summary(request: Request) -> JSONResponse:
        """Report the app identity, whether a config was found, and client state."""
        try:
            config_mod.resolve_path(config_arg)
            config_found = True
        except config_mod.ConfigError:
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
            return _unknown_client()
        async with verify_lock:
            result = await verify_client(spec)
        return JSONResponse(result)

    async def verify_http_route(request: Request) -> JSONResponse:
        """Verify this install over the HTTP (SSE) transport — one global check."""
        # Imported here, not at module scope: ``cli`` imports this module to serve
        # the GUI, so a top-level import would close the cycle.
        from ..cli import server_launch

        try:
            path = config_mod.resolve_path(config_arg)
        except config_mod.ConfigError:
            return _no_config()
        command, args = server_launch(path)
        async with verify_lock:
            result = await verify_http(command, args)
        return JSONResponse(result)

    def _config_path_for_writes() -> Path:
        """Resolve like the CLI does; when nothing exists yet, pick where to create."""
        try:
            return config_mod.resolve_path(config_arg)
        except config_mod.ConfigError:
            return Path(config_arg) if config_arg else config_mod.global_config_path()

    async def _body(request: Request) -> object:
        """The JSON body, or ``None`` when it is not JSON (never the raw text)."""
        try:
            return await request.json()
        except ValueError:
            return None

    async def list_databases(request: Request) -> JSONResponse:
        """List the configured connections — ``public_view`` only, never a DSN.

        Deliberately outside ``config_lock``: this only reads, and ``config.save``
        replaces the file atomically, so a reader never observes a partial write.
        """
        cfg = _load_or_empty(_config_path_for_writes())
        if cfg is None:
            return _no_config()
        return JSONResponse([c.public_view() for c in cfg.connections])

    async def add_database(request: Request) -> JSONResponse:
        """Append one connection. This is the only route a DSN may enter by.

        The body is read *before* the config is loaded: an ``await`` between the
        load and the save would let a second request load the same file, and the
        later save would drop the earlier request's connection despite its 201.
        """
        parsed = _parse_connection(await _body(request))
        if not isinstance(parsed, Connection):
            return _invalid_connection(parsed)
        path = _config_path_for_writes()
        async with config_lock:
            cfg = _load_or_empty(path)
            if cfg is None:
                return _no_config()
            if any(c.name == parsed.name for c in cfg.connections):
                return JSONResponse(
                    {"error": "a connection with that name exists"}, status_code=409
                )
            cfg.connections.append(parsed)
            config_mod.save(cfg, path)
        return JSONResponse(parsed.public_view(), status_code=201)

    async def edit_database(request: Request) -> JSONResponse:
        """Merge ``mode``/``yolo``/``fallback_ports``/``dsn`` into one connection.

        A field that is absent, ``null``, or ``""`` keeps its stored value — that is
        how an edit form can submit without ever re-sending the DSN it never saw.
        """
        name = request.path_params["name"]
        body = await _body(request)  # before the load — see add_database
        if not isinstance(body, dict):
            return _invalid_connection([])
        path = _config_path_for_writes()
        async with config_lock:
            cfg = _load_or_empty(path)
            if cfg is None:
                return _no_config()
            existing = next((c for c in cfg.connections if c.name == name), None)
            if existing is None:
                return _no_such_connection()
            merged = existing.model_dump()
            for field in ("mode", "yolo", "fallback_ports", "dsn"):
                if field in body and body[field] not in (None, ""):
                    merged[field] = body[field]
            parsed = _parse_connection(merged)
            if not isinstance(parsed, Connection):
                return _invalid_connection(parsed)
            cfg.connections[cfg.connections.index(existing)] = parsed
            config_mod.save(cfg, path)
        return JSONResponse(parsed.public_view())

    async def delete_database(request: Request) -> JSONResponse:
        """Drop one connection by name (and with it, its stored DSN)."""
        name = request.path_params["name"]
        path = _config_path_for_writes()
        async with config_lock:
            cfg = _load_or_empty(path)
            if cfg is None:
                return _no_config()
            remaining = [c for c in cfg.connections if c.name != name]
            if len(remaining) == len(cfg.connections):
                return _no_such_connection()
            cfg.connections = remaining
            config_mod.save(cfg, path)
        return JSONResponse({"removed": name})

    async def check_database_route(request: Request) -> JSONResponse:
        """Probe one connection; the handler's rows are already sanitized.

        Also outside ``config_lock``: this only reads, and a probe can take tens of
        seconds — holding the write lock across it would freeze the editor.
        """
        name = request.path_params["name"]
        path = _config_path_for_writes()
        cfg = _load_or_empty(path) if path.is_file() else None
        if cfg is None:
            return _no_config()
        if not any(c.name == name for c in cfg.connections):
            return _no_such_connection()
        return JSONResponse(await Handlers(path).check_database(name))

    async def inject_client(request: Request) -> JSONResponse:
        """Register this server in one detected client's config, wizard-identically.

        Goes through ``clients.inject_entry``/``write_config`` — the same seam the CLI
        wizard uses — so the GUI is a second front-end, never a second implementation.
        A config that will not parse is refused rather than clobbered.
        """
        # Imported here, not at module scope: ``cli`` imports this module to serve the
        # GUI, so a top-level import would close the cycle.
        from ..cli import server_launch

        key = request.path_params["key"]
        spec = next((s for s in clients_mod.detected_clients() if s.key == key), None)
        if spec is None:
            return _unknown_client()
        if not clients_mod.config_readable(spec):
            return _unreadable_client()
        try:
            path = config_mod.resolve_path(config_arg)
        except config_mod.ConfigError:
            return _no_config()
        command, args = server_launch(path)
        # A client config is config too: the read-merge-write span takes the same lock
        # for the same lost-update reason connections.json does. The span is
        # synchronous, so the lock is defensive rather than load-bearing.
        async with config_lock:
            try:
                data = clients_mod.read_config(spec)
            except clients_mod.ClientConfigError:
                # Re-checked under the lock: the file may have gone bad since the
                # probe above, and the raised text quotes the source (Rule 6).
                return _unreadable_client()
            clients_mod.write_config(
                spec, clients_mod.inject_entry(data, spec.fmt, "db-conn-mcp", command, args)
            )
        return JSONResponse({"injected": spec.key})

    async def uninject_client(request: Request) -> JSONResponse:
        """Drop this server's entry from one detected client's config."""
        key = request.path_params["key"]
        spec = next((s for s in clients_mod.detected_clients() if s.key == key), None)
        if spec is None:
            return _unknown_client()
        if not clients_mod.config_readable(spec):
            return _unreadable_client()
        async with config_lock:  # see inject_client
            try:
                data = clients_mod.read_config(spec)
            except clients_mod.ClientConfigError:
                return _unreadable_client()
            clients_mod.write_config(spec, clients_mod.remove_entry(data, spec.fmt, "db-conn-mcp"))
        return JSONResponse({"removed": spec.key})

    async def doctor_route(request: Request) -> JSONResponse:
        """Run the doctor checks and return its findings verbatim (Rule 6: already sanitized).

        Deliberately outside both locks: doctor only probes and reports, it writes
        nothing, so there is no shared state here for either lock to protect.
        """
        body = await _body(request)
        if not isinstance(body, dict):
            body = {}
        try:
            path = config_mod.resolve_path(config_arg)
        except config_mod.ConfigError:
            path = None
        findings = await run_checks(path, offline=bool(body.get("offline", False)))
        return JSONResponse(findings)

    routes = [
        Route("/", index),
        Route("/api/summary", summary),
        Route("/api/verify/client/{key}", verify_client_route, methods=["POST"]),
        Route("/api/verify/http", verify_http_route, methods=["POST"]),
        Route("/api/doctor", doctor_route, methods=["POST"]),
        # Two Routes share /api/databases with disjoint methods: Starlette keeps
        # scanning past a path match whose method does not fit (Match.PARTIAL).
        Route("/api/databases", list_databases, methods=["GET"]),
        Route("/api/databases", add_database, methods=["POST"]),
        Route("/api/databases/{name}", edit_database, methods=["PATCH"]),
        Route("/api/databases/{name}", delete_database, methods=["DELETE"]),
        Route("/api/databases/{name}/check", check_database_route, methods=["POST"]),
        Route("/api/clients/{key}/inject", inject_client, methods=["POST"]),
        Route("/api/clients/{key}/uninject", uninject_client, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(_static_dir())), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(_Guard, token=token)])
