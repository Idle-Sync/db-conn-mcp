"""The local GUI web app: a token-guarded Starlette app bound to 127.0.0.1.

Security model (spec: 2026-08-11 GUI design): loopback bind, Host-header
allowlist (DNS-rebinding defence), a per-start bearer token checked on every
request including ``/``, no CORS ever, CSP ``default-src 'self'`` on every
response. API responses use a fixed field vocabulary — a DSN cannot appear in
any response by construction (Rule 6).
"""

import asyncio
import hmac
import os
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path
from urllib.parse import quote

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
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
#: Set on the page hand-off only, so a bookmark keeps working for the rest of the
#: server run. It is accepted for *safe* methods alone — see :class:`_Guard`.
SESSION_COOKIE = "db_conn_mcp_gui"
#: The methods the cookie may authenticate. A cookie travels on a request the user
#: did not compose, so it must never be enough to spawn a process or write a file.
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_ALLOWED_HOSTS = frozenset({f"127.0.0.1:{GUI_PORT}", f"localhost:{GUI_PORT}"})
_CSP = "default-src 'self'"
#: Every guarded URL carries ``?token=<secret>`` (the page and its assets), so nothing
#: this app serves may be written to the browser's on-disk cache.
_NO_STORE = "no-store"
#: Replaced with the session token when ``index.html`` is served. A browser does not
#: copy the page's ``?token=`` onto its subresource requests, and ``/static`` sits
#: behind the same guard, so the page's own CSS and JS would 403 without this. The
#: token is not new information in that HTML — it is already in the address bar.
_ASSET_TOKEN_MARKER = "__GUI_TOKEN__"
#: The body an unauthenticated *browser* gets from ``GET /``. It is a 403 like every
#: other refusal — only the representation differs, because the one visitor who
#: navigates here by hand is overwhelmingly the owner, and a bare ``{"error":
#: "forbidden"}`` leaves them with no way in. It names the tool and the command that
#: opens the dashboard and nothing else (Rule 6): the port is documented, so what runs
#: here is already public, and none of it helps without the token. Deliberately
#: unstyled — CSP forbids inline styles and an unauthenticated stylesheet fetch would
#: itself be a 403.
_HINT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>db-conn-mcp dashboard</title>
</head>
<body>
<h1>db-conn-mcp</h1>
<p>This dashboard needs a session token, and this request did not carry one.</p>
<p>Run <code>db-conn-mcp gui</code> in a terminal to open it.</p>
</body>
</html>
"""


def token_path() -> Path:
    """Where the current GUI token is persisted (user-only file, 0600)."""
    return Path.home() / ".db-conn-mcp" / "gui-token"


def _static_dir() -> Path:
    """The packaged static assets (real directory in editable and wheel installs)."""
    return Path(str(resources.files("db_conn_mcp.gui") / "static"))


def _forbidden() -> JSONResponse:
    """A sanitized 403 that still carries the CSP and no-store — no reason is disclosed.

    Both headers are set here rather than relied on from :class:`_Guard`: this
    response is *returned instead of* calling the next app, so it never reaches the
    lines that stamp them on a served response.
    """
    response = JSONResponse({"error": "forbidden"}, status_code=403)
    response.headers["content-security-policy"] = _CSP
    response.headers["cache-control"] = _NO_STORE
    return response


def _hint_response() -> HTMLResponse:
    """The same 403, rendered for a human: :data:`_HINT_PAGE` with the same headers.

    Both headers are set here for the reason :func:`_forbidden` sets them — this
    response replaces the call to the next app, so nothing downstream stamps them.
    """
    response = HTMLResponse(_HINT_PAGE, status_code=403)
    response.headers["content-security-policy"] = _CSP
    response.headers["cache-control"] = _NO_STORE
    return response


def _prefers_html(accept: str) -> bool:
    """Whether the caller asked for HTML ahead of JSON — i.e. a browser navigating.

    Quality values are ignored on purpose (KISS): every browser lists ``text/html``
    first and no programmatic caller asks for it at all, so position is signal
    enough. A missing or ``*/*`` header is not a browser navigation.
    """
    for part in accept.split(","):
        media = part.split(";")[0].strip().lower()
        if media == "text/html":
            return True
        if media == "application/json":
            return False
    return False


def _refuse(request: Request) -> Response:
    """The 403 for one unauthenticated request: the hint page, or the fixed JSON.

    Only a browser navigation to ``/`` earns the readable body. Every other path,
    method and Accept keeps the opaque refusal, so nothing is disclosed to a prober
    that scans routes rather than opening the dashboard.
    """
    if request.method != "GET" or request.url.path != "/":
        return _forbidden()
    if not _prefers_html(request.headers.get("accept", "")):
        return _forbidden()
    return _hint_response()


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


def _unaddressable_name(name: str) -> bool:
    """Whether a name could never be reached again through ``/api/databases/{name}``.

    A name that is empty, that is not its own ``.strip()``, or that carries a ``/``
    saves a row the dashboard can create but never edit, test or delete: the path
    either fails to match the route at all or matches a *different* string, so every
    follow-up 404s and only hand-editing ``connections.json`` can undo it. Refusing
    it up front is the only way the editor stays honest about what it can undo.
    """
    return not name or name != name.strip() or "/" in name


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

    def _supplied(self, request: Request) -> str:
        """The credential this request offers: header, then query, then the cookie.

        The cookie is read for safe methods only. It is *ambient* — the browser
        attaches it to a request the user never composed — so letting it authorize a
        POST would make every mutating route reachable by cross-site request forgery
        the moment a browser's ``SameSite`` handling slipped. Header and query are
        explicit and keep working for every method.
        """
        explicit = request.headers.get(TOKEN_HEADER) or request.query_params.get("token")
        if explicit:
            return explicit
        if request.method in _SAFE_METHODS:
            return request.cookies.get(SESSION_COOKIE, "")
        return ""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject anything not loopback-addressed or not carrying the token."""
        if request.headers.get("host", "") not in _ALLOWED_HOSTS:
            return _forbidden()
        supplied = self._supplied(request)
        # Compared as bytes: compare_digest raises TypeError on non-ASCII *str*, and a
        # crash inside the security boundary would escape to ServerErrorMiddleware —
        # a 500 with no CSP and a traceback on the terminal. surrogateescape keeps any
        # undecodable byte sequence comparable instead of raising a second time.
        if not hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), self._token):
            return _refuse(request)
        response = await call_next(request)
        response.headers["content-security-policy"] = _CSP
        response.headers["cache-control"] = _NO_STORE
        return response


class _Touch(BaseHTTPMiddleware):
    """Report each served request to whoever is watching for idleness.

    Mounted *inside* :class:`_Guard` on purpose: only a request that passed the
    token check counts as activity, so an unauthenticated poker cannot hold a
    standalone GUI open past its idle timeout.
    """

    def __init__(self, app, on_request: Callable[[], None]) -> None:
        super().__init__(app)
        self._on_request = on_request

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Stamp the caller's clock, then serve the request unchanged."""
        self._on_request()
        return await call_next(request)


def create_app(
    token: str,
    config_arg: str | None = None,
    on_request: Callable[[], None] | None = None,
) -> Starlette:
    """Build the GUI app. ``config_arg`` is the CLI's --config passthrough.

    ``on_request`` is called once per authenticated request; :func:`run_standalone`
    uses it to drive its idle-shutdown clock. It is a constructor argument rather
    than a middleware added afterwards because starlette 1.2 dropped the
    ``@app.middleware`` decorator, and ``add_middleware`` after construction is a
    started-app hazard the app has no reason to take.

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

    async def index(request: Request) -> HTMLResponse:
        """Serve the dashboard page, stamping the token into its asset URLs and meta tag.

        The one templated value is :data:`_ASSET_TOKEN_MARKER`; everything else in
        the file is static. It is percent-encoded on the way in so a token can never
        break out of the attribute it lands in.

        This is also the only response that mints the session cookie, so the URL
        survives a reload or a bookmark for the rest of the server run. It is
        ``HttpOnly`` (script cannot read it), ``SameSite=Strict`` (no cross-site
        request carries it), session-lifetime (a restart mints a new token anyway),
        and not ``Secure`` — the dashboard is plain http on loopback, so a ``Secure``
        cookie would simply never be sent back.
        """
        html = (_static_dir() / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(html.replace(_ASSET_TOKEN_MARKER, quote(token, safe="")))
        response.set_cookie(SESSION_COOKIE, token, path="/", httponly=True, samesite="strict")
        return response

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

        This is also the only route a *new* name enters by — the name is the identity
        and is not renameable, so ``edit_database`` merges the stored one and never
        needs the same check.
        """
        parsed = _parse_connection(await _body(request))
        if not isinstance(parsed, Connection):
            return _invalid_connection(parsed)
        if _unaddressable_name(parsed.name):
            return _invalid_connection(["name"])
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
    # Order is outermost-first: the guard runs before anything else sees a request.
    middleware = [Middleware(_Guard, token=token)]
    if on_request is not None:
        middleware.append(Middleware(_Touch, on_request=on_request))
    return Starlette(routes=routes, middleware=middleware)


def _idle_exceeded(last_request: float, now: float, idle_timeout: float) -> bool:
    """Whether the standalone server has been quiet long enough to exit."""
    return (now - last_request) > idle_timeout


def _bind_gui_port() -> socket.socket | None:
    """Claim 127.0.0.1:GUI_PORT, or None if anything already holds it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", GUI_PORT))
        sock.listen(128)
    except OSError:
        sock.close()
        return None
    return sock


def _write_token(token: str) -> None:
    """Persist the session token user-only (0600); the winner of the bind writes it.

    The mode is passed to the *creation* call rather than chmod'ed afterwards: a
    write-then-chmod leaves the secret on disk at the umask default (typically
    0644) for the window in between, readable by every local user. The trailing
    chmod only tightens a file that already existed. The mode is advisory on
    Windows — NTFS ignores POSIX bits — but the file lives under the user's
    profile directory, which is not world-readable there anyway.
    """
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    path.chmod(0o600)


def start_in_thread(config_arg: str | None = None) -> bool:
    """Host the GUI from inside a running MCP server, if the port is free.

    Silent by design: a GUI problem must never crash or pollute the MCP server
    (stdout carries the protocol). Returns True when this process won the port.
    """
    try:
        sock = _bind_gui_port()
        if sock is None:
            return False
        token = secrets.token_urlsafe(32)
        _write_token(token)
        app = create_app(token, config_arg=config_arg)
        # log_config=None keeps uvicorn off stdout (its default access handler
        # writes there); access_log=False keeps the token out of any log at all,
        # since the request line carries ``?token=<secret>`` (Rule 6).
        cfg = uvicorn.Config(app, log_config=None, log_level="critical", access_log=False)
        server = uvicorn.Server(cfg)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="db-conn-mcp-gui", daemon=True
        )
        thread.start()
        return True
    except Exception:  # noqa: BLE001 — the MCP server must survive any GUI failure
        return False


def run_standalone(
    config_arg: str | None = None, open_browser: bool = True, idle_timeout: float = 900.0
) -> int:
    """Run the GUI in the foreground: stderr logs, idle shutdown, browser opened."""
    sock = _bind_gui_port()
    if sock is None:
        return 2  # caller (cmd_gui) probes and reports; this is the fallback path
    token = secrets.token_urlsafe(32)
    _write_token(token)
    state = {"last_request": time.monotonic()}

    def _touched() -> None:
        """Reset the idle clock (called by :class:`_Touch` on every real request)."""
        state["last_request"] = time.monotonic()

    app = create_app(token, config_arg=config_arg, on_request=_touched)
    # access_log=False again: the URL carries ``?token=<secret>`` and uvicorn's
    # access log would print the whole request line (Rule 6). The remaining
    # lifecycle lines go to uvicorn's default handler, which is stderr.
    cfg = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)

    def _watchdog() -> None:
        while not server.should_exit:
            time.sleep(30)
            if _idle_exceeded(state["last_request"], time.monotonic(), idle_timeout):
                server.should_exit = True

    threading.Thread(target=_watchdog, name="db-conn-mcp-gui-idle", daemon=True).start()
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{GUI_PORT}/?token={token}")
    server.run(sockets=[sock])
    return 0
