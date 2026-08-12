"""The GUI web app: guard middleware, summary, and (later tasks) the full API."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from db_conn_mcp import __version__
from db_conn_mcp.gui.app import GUI_PORT, TOKEN_HEADER, create_app

TOKEN = "test-token-abcdef"
HOST = f"127.0.0.1:{GUI_PORT}"
SECRET_DSN = "postgresql://user:hunter2-secret@db.internal:5432/prod"
OTHER_DSN = "postgresql://user:other-secret@db.internal:5432/other"


@pytest.fixture()
def client():
    app = create_app(TOKEN, config_arg=None)
    return TestClient(app, base_url=f"http://{HOST}")


@pytest.fixture()
def cfg_client(tmp_path):
    cfg = tmp_path / "connections.json"
    app = create_app(TOKEN, config_arg=str(cfg))
    return TestClient(app, base_url=f"http://{HOST}"), cfg


def _hdr():
    return {TOKEN_HEADER: TOKEN}


def _get(client, path, **kw):
    kw.setdefault("headers", {})[TOKEN_HEADER] = TOKEN
    return client.get(path, **kw)


def test_no_token_is_403_everywhere(client):
    assert client.get("/").status_code == 403
    assert client.get("/api/summary").status_code == 403


def test_wrong_host_is_403_even_with_token(client):
    r = client.get("/api/summary", headers={TOKEN_HEADER: TOKEN, "host": "evil.example:31415"})
    assert r.status_code == 403


def test_token_in_query_serves_the_page(client):
    r = client.get(f"/?token={TOKEN}")
    assert r.status_code == 200
    assert "db-conn-mcp" in r.text


def test_csp_header_on_every_response(client):
    for r in (_get(client, "/api/summary"), client.get("/")):
        assert r.headers.get("content-security-policy") == "default-src 'self'"


def test_summary_shape(client, monkeypatch):
    from db_conn_mcp import clients as clients_mod

    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [])
    r = _get(client, "/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "db-conn-mcp-gui"
    assert body["version"] == __version__
    assert "config_found" in body
    assert body["clients"] == []


def test_summary_never_contains_a_dsn(client, tmp_path, monkeypatch):
    """Rule 6 canary for the shell; Task 6 extends the sweep to the whole API."""
    r = _get(client, "/api/summary")
    assert "postgresql://" not in r.text


def test_non_ascii_token_is_a_clean_403(client):
    """A non-ASCII credential must be rejected, not crash the guard (no 500)."""
    r = client.get("/?token=%C3%B6")
    assert r.status_code == 403
    assert r.headers.get("content-security-policy") == "default-src 'self'"


def test_empty_token_is_rejected_at_construction():
    """An empty configured token would authenticate everyone; refuse to build."""
    with pytest.raises(ValueError):
        create_app("")


def test_static_files_are_behind_the_guard(client):
    """Assets are not a bypass: no token, no file."""
    assert client.get("/static/style.css").status_code == 403
    assert _get(client, "/static/style.css").status_code == 200


def test_no_cors_headers_anywhere(client):
    """The GUI is same-origin only — nothing may hand out cross-origin access."""
    for r in (_get(client, "/api/summary"), _get(client, "/"), client.get("/")):
        assert not [k for k in r.headers if k.lower().startswith("access-control-")]


def test_forbidden_body_discloses_nothing(client):
    """The 403 body is a fixed token — never a reason, path, or credential echo."""
    r = client.get("/api/summary", headers={"host": "evil.example:31415"})
    assert r.json() == {"error": "forbidden"}


def test_verify_client_endpoint_calls_engine(client, monkeypatch):
    from db_conn_mcp.gui import app as gui_app

    async def fake_verify_client(spec, timeout=20.0):
        return {"verdict": "answers", "detail": "ok", "command": "x", "args": []}

    monkeypatch.setattr(gui_app, "verify_client", fake_verify_client)
    r = client.post("/api/verify/client/claude", headers={TOKEN_HEADER: TOKEN})
    # 'claude' may not be detected on CI; unknown key must 404, detected key 200.
    assert r.status_code in (200, 404)


def test_verify_client_returns_the_engine_result(client, monkeypatch):
    """Pin the 200 path deterministically: a detected client's result is the body."""
    from db_conn_mcp import clients as clients_mod
    from db_conn_mcp.clients import ClientSpec
    from db_conn_mcp.gui import app as gui_app

    spec = ClientSpec(key="fake", label="Fake", path=Path("nowhere.json"), fmt="mcpServers")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [spec])
    payload = {"verdict": "answers", "detail": "ok", "command": "x", "args": []}

    async def fake_verify_client(target, timeout=20.0):
        assert target is spec
        return payload

    monkeypatch.setattr(gui_app, "verify_client", fake_verify_client)
    r = client.post("/api/verify/client/fake", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 200
    assert r.json() == payload


def test_verify_unknown_client_404(client):
    r = client.post("/api/verify/client/nope", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown or undetected client"}


def test_verify_is_post_only(client):
    assert client.get("/api/verify/http", headers={TOKEN_HEADER: TOKEN}).status_code == 405


def test_verify_http_without_a_config_is_409(client, monkeypatch):
    """No config means nothing to launch — a fixed 409, never a path echo (Rule 6)."""
    from db_conn_mcp import config

    def boom(explicit=None):
        raise config.ConfigError("none")

    monkeypatch.setattr(config, "resolve_path", boom)
    r = client.post("/api/verify/http", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 409
    assert r.json() == {"error": "no configuration found"}


def test_verify_http_calls_engine(client, monkeypatch, tmp_path):
    from db_conn_mcp import config
    from db_conn_mcp.gui import app as gui_app

    monkeypatch.setattr(config, "resolve_path", lambda explicit=None: tmp_path / "connections.json")
    payload = {"verdict": "answers", "detail": "ok", "command": "x", "args": []}

    async def fake_verify_http(command, args, port=8000, timeout=30.0):
        return payload

    monkeypatch.setattr(gui_app, "verify_http", fake_verify_http)
    r = client.post("/api/verify/http", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 200
    assert r.json() == payload


# ---- Task 6: the databases API (CRUD + check), DSN inbound-only ----


def test_database_crud_roundtrip(cfg_client):
    client, cfg = cfg_client
    r = client.post(
        "/api/databases",
        json={"name": "staging", "dsn": SECRET_DSN, "mode": "read", "yolo": False},
        headers=_hdr(),
    )
    assert r.status_code == 201
    listing = client.get("/api/databases", headers=_hdr()).json()
    assert listing == [{"name": "staging", "mode": "read", "yolo": False}]
    r = client.patch("/api/databases/staging", json={"mode": "write"}, headers=_hdr())
    assert r.status_code == 200
    assert client.get("/api/databases", headers=_hdr()).json()[0]["mode"] == "write"
    assert client.delete("/api/databases/staging", headers=_hdr()).status_code == 200
    assert client.get("/api/databases", headers=_hdr()).json() == []


def test_edit_without_dsn_keeps_the_stored_one(cfg_client):
    client, cfg = cfg_client
    client.post(
        "/api/databases",
        json={"name": "s", "dsn": SECRET_DSN, "mode": "read"},
        headers=_hdr(),
    )
    client.patch("/api/databases/s", json={"mode": "write"}, headers=_hdr())
    stored = cfg.read_text(encoding="utf-8")
    assert SECRET_DSN in stored  # still on disk...


def test_no_api_response_ever_contains_the_dsn(cfg_client):
    """The Rule 6 sweep: create, list, edit, duplicate-error, validation-error,
    delete-missing, edit-missing, malformed body, and both check failure paths —
    no response body from any route may carry the DSN."""
    client, cfg = cfg_client
    add = {"name": "s", "dsn": SECRET_DSN, "mode": "read"}
    bad_mode = {"name": "bad", "dsn": SECRET_DSN, "mode": "banana"}
    # The expected status is pinned alongside each call so the sweep cannot rot
    # into a wall of 404s that trivially carries no DSN.
    responses = [
        # The check route before any config file exists (the 409 path).
        (409, client.post("/api/databases/s/check", headers=_hdr())),
        (201, client.post("/api/databases", json=add, headers=_hdr())),
        (200, client.get("/api/databases", headers=_hdr())),
        (200, client.patch("/api/databases/s", json={"yolo": True}, headers=_hdr())),
        (200, client.patch("/api/databases/s", json={"dsn": SECRET_DSN}, headers=_hdr())),
        (400, client.patch("/api/databases/s", json={"mode": "banana"}, headers=_hdr())),
        (404, client.patch("/api/databases/ghost", json={"mode": "write"}, headers=_hdr())),
        (409, client.post("/api/databases", json=add, headers=_hdr())),
        (400, client.post("/api/databases", json=bad_mode, headers=_hdr())),
        (400, client.post("/api/databases", content=SECRET_DSN, headers=_hdr())),
        (404, client.delete("/api/databases/ghost", headers=_hdr())),
        # The check route with a config present but an unknown name (the 404 path).
        (404, client.post("/api/databases/ghost/check", headers=_hdr())),
        (200, client.get("/api/summary", headers=_hdr())),
    ]
    for expected, r in responses:
        assert r.status_code == expected, r.request.url
        assert "hunter2-secret" not in r.text, r.request.url
        assert "postgresql://" not in r.text, r.request.url
    # ...and the DSN did reach the disk, so the sweep was checking a live secret.
    assert SECRET_DSN in cfg.read_text(encoding="utf-8")


def test_validation_error_names_fields_only(cfg_client):
    client, _ = cfg_client
    r = client.post(
        "/api/databases",
        json={"name": "x", "dsn": SECRET_DSN, "mode": "banana"},
        headers=_hdr(),
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid connection"
    assert "mode" in body["fields"]
    assert "hunter2-secret" not in r.text


def test_duplicate_name_409(cfg_client):
    client, _ = cfg_client
    payload = {"name": "s", "dsn": SECRET_DSN, "mode": "read"}
    client.post("/api/databases", json=payload, headers=_hdr())
    assert client.post("/api/databases", json=payload, headers=_hdr()).status_code == 409


def test_malformed_body_is_a_sanitized_400(cfg_client):
    """A non-JSON (or non-object) body must not reach the parser as a 500."""
    client, _ = cfg_client
    r = client.post("/api/databases", content=SECRET_DSN, headers=_hdr())
    assert r.status_code == 400
    assert r.json() == {"error": "invalid connection", "fields": []}
    r = client.post("/api/databases", json=[SECRET_DSN], headers=_hdr())
    assert r.status_code == 400
    assert "hunter2-secret" not in r.text


def test_edit_missing_connection_404(cfg_client):
    client, _ = cfg_client
    r = client.patch("/api/databases/ghost", json={"mode": "write"}, headers=_hdr())
    assert r.status_code == 404
    assert r.json() == {"error": "no such connection"}


def test_delete_missing_connection_404(cfg_client):
    client, _ = cfg_client
    r = client.delete("/api/databases/ghost", headers=_hdr())
    assert r.status_code == 404
    assert r.json() == {"error": "no such connection"}


def test_edit_can_replace_the_dsn(cfg_client):
    """A supplied DSN replaces the stored one; the response still omits both."""
    client, cfg = cfg_client
    client.post(
        "/api/databases", json={"name": "s", "dsn": SECRET_DSN, "mode": "read"}, headers=_hdr()
    )
    other = "postgresql://user:other-pass@host:5432/db"
    r = client.patch("/api/databases/s", json={"dsn": other}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"name": "s", "mode": "read", "yolo": False}
    stored = cfg.read_text(encoding="utf-8")
    assert other in stored
    assert SECRET_DSN not in stored


def test_empty_dsn_on_edit_keeps_the_stored_one(cfg_client):
    """An empty string means "unchanged" — the form sends one for an untouched field."""
    client, cfg = cfg_client
    client.post(
        "/api/databases", json={"name": "s", "dsn": SECRET_DSN, "mode": "read"}, headers=_hdr()
    )
    r = client.patch("/api/databases/s", json={"dsn": "", "yolo": True}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["yolo"] is True
    assert SECRET_DSN in cfg.read_text(encoding="utf-8")


def test_databases_are_only_mutable_by_post_patch_delete(cfg_client):
    """No mutation is reachable by GET — a link or a prefetch must never write."""
    client, cfg = cfg_client
    assert client.get("/api/databases/s", headers=_hdr()).status_code == 405
    assert client.get("/api/databases/s/check", headers=_hdr()).status_code == 405
    assert client.put("/api/databases", json={}, headers=_hdr()).status_code == 405
    assert not cfg.exists()


def test_databases_api_is_behind_the_guard(cfg_client):
    """Every new route is a 403 without the token — including the mutating ones."""
    client, _ = cfg_client
    assert client.get("/api/databases").status_code == 403
    assert client.post("/api/databases", json={}).status_code == 403
    assert client.patch("/api/databases/s", json={}).status_code == 403
    assert client.delete("/api/databases/s").status_code == 403
    assert client.post("/api/databases/s/check").status_code == 403


def test_check_without_a_config_is_409(cfg_client):
    client, _ = cfg_client
    r = client.post("/api/databases/s/check", headers=_hdr())
    assert r.status_code == 409
    assert r.json() == {"error": "no configuration found"}


def test_check_unknown_name_is_404(cfg_client):
    client, _ = cfg_client
    client.post(
        "/api/databases", json={"name": "s", "dsn": SECRET_DSN, "mode": "read"}, headers=_hdr()
    )
    r = client.post("/api/databases/ghost/check", headers=_hdr())
    assert r.status_code == 404
    assert r.json() == {"error": "no such connection"}


def test_check_returns_the_handler_rows(cfg_client, monkeypatch):
    """The route is a thin pass-through of the already-sanitized handler rows."""
    from db_conn_mcp.gui import app as gui_app

    client, _ = cfg_client
    client.post(
        "/api/databases", json={"name": "s", "dsn": SECRET_DSN, "mode": "read"}, headers=_hdr()
    )
    rows = [{"database": "s", "status": "OK"}]

    class FakeHandlers:
        def __init__(self, path):
            self.path = path

        async def check_database(self, name):
            assert name == "s"
            return rows

    monkeypatch.setattr(gui_app, "Handlers", FakeHandlers)
    r = client.post("/api/databases/s/check", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == rows


def test_fallback_ports_round_trip(cfg_client):
    client, _ = cfg_client
    r = client.post(
        "/api/databases",
        json={"name": "s", "dsn": SECRET_DSN, "mode": "read", "fallback_ports": [5433, 5434]},
        headers=_hdr(),
    )
    assert r.status_code == 201
    assert r.json()["fallback_ports"] == [5433, 5434]
    r = client.patch("/api/databases/s", json={"fallback_ports": [6000]}, headers=_hdr())
    assert r.json()["fallback_ports"] == [6000]


def test_unsupported_dsn_scheme_is_a_sanitized_400(cfg_client):
    """config.save writes it, but the next load would reject it — refuse up front."""
    client, cfg = cfg_client
    r = client.post(
        "/api/databases",
        json={"name": "s", "dsn": "mysql://user:hunter2-secret@h/db", "mode": "read"},
        headers=_hdr(),
    )
    assert r.status_code == 400
    assert r.json() == {"error": "invalid connection", "fields": ["dsn"]}
    assert "hunter2-secret" not in r.text


# ---- Task 6 review round 1: concurrency and the no-clobber guarantee ----


def _async_client(cfg: Path) -> httpx.AsyncClient:
    """A real ASGI client — TestClient serializes, so it cannot overlap requests."""
    app = create_app(TOKEN, config_arg=str(cfg))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")


def _stored_names(cfg: Path) -> list[str]:
    return sorted(c["name"] for c in json.loads(cfg.read_text(encoding="utf-8"))["connections"])


async def test_concurrent_adds_do_not_lose_a_connection(tmp_path):
    """Two overlapping POSTs must both persist.

    The read-modify-write span (load the file, mutate, save it) has to be
    indivisible. When it was not, both requests loaded the same empty config and
    the second save overwrote the first — a 201 whose DSN silently vanished.
    """
    cfg = tmp_path / "connections.json"
    async with _async_client(cfg) as client:
        first, second = await asyncio.gather(
            client.post(
                "/api/databases",
                json={"name": "a", "dsn": SECRET_DSN, "mode": "read"},
                headers=_hdr(),
            ),
            client.post(
                "/api/databases",
                json={"name": "b", "dsn": OTHER_DSN, "mode": "read"},
                headers=_hdr(),
            ),
        )
    assert [first.status_code, second.status_code] == [201, 201]
    assert _stored_names(cfg) == ["a", "b"]


async def test_concurrent_duplicate_adds_leave_exactly_one(tmp_path):
    """The duplicate guard must survive overlap: one 201, one 409, one row."""
    cfg = tmp_path / "connections.json"
    payload = {"name": "same", "dsn": SECRET_DSN, "mode": "read"}
    async with _async_client(cfg) as client:
        results = await asyncio.gather(
            client.post("/api/databases", json=payload, headers=_hdr()),
            client.post("/api/databases", json=payload, headers=_hdr()),
        )
    assert sorted(r.status_code for r in results) == [201, 409]
    assert _stored_names(cfg) == ["same"]


async def test_concurrent_edits_do_not_lose_a_sibling(tmp_path):
    """An overlapping PATCH of two rows must not roll the other one back."""
    cfg = tmp_path / "connections.json"
    async with _async_client(cfg) as client:
        for name in ("a", "b"):
            r = await client.post(
                "/api/databases",
                json={"name": name, "dsn": SECRET_DSN, "mode": "read"},
                headers=_hdr(),
            )
            assert r.status_code == 201
        results = await asyncio.gather(
            client.patch("/api/databases/a", json={"mode": "write"}, headers=_hdr()),
            client.patch("/api/databases/b", json={"yolo": True}, headers=_hdr()),
        )
        assert [r.status_code for r in results] == [200, 200]
        listing = (await client.get("/api/databases", headers=_hdr())).json()
    by_name = {row["name"]: row for row in listing}
    assert by_name["a"]["mode"] == "write"
    assert by_name["b"]["yolo"] is True


async def test_concurrent_delete_and_add_keep_both_effects(tmp_path):
    """A delete overlapping an add must not resurrect the deleted row."""
    cfg = tmp_path / "connections.json"
    async with _async_client(cfg) as client:
        await client.post(
            "/api/databases",
            json={"name": "old", "dsn": SECRET_DSN, "mode": "read"},
            headers=_hdr(),
        )
        results = await asyncio.gather(
            client.delete("/api/databases/old", headers=_hdr()),
            client.post(
                "/api/databases",
                json={"name": "new", "dsn": OTHER_DSN, "mode": "read"},
                headers=_hdr(),
            ),
        )
    assert [r.status_code for r in results] == [200, 201]
    assert _stored_names(cfg) == ["new"]


# ---- Task 7: the clients API (inject / uninject) ----


def test_inject_and_uninject_roundtrip(cfg_client, tmp_path, monkeypatch):
    import json as jsonlib

    from db_conn_mcp import clients as clients_mod
    from db_conn_mcp.clients import ClientSpec

    client, cfg = cfg_client
    cfg.write_text(jsonlib.dumps({"connections": []}), encoding="utf-8")
    target = ClientSpec("fake", "Fake", tmp_path / "fake.json", "mcpServers")
    (tmp_path / "fake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clients_mod, "client_specs", lambda: [target])
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [target])

    assert client.post("/api/clients/fake/inject", headers=_hdr()).status_code == 200
    stored = jsonlib.loads((tmp_path / "fake.json").read_text(encoding="utf-8"))
    assert "db-conn-mcp" in stored["mcpServers"]
    assert client.post("/api/clients/fake/uninject", headers=_hdr()).status_code == 200
    stored = jsonlib.loads((tmp_path / "fake.json").read_text(encoding="utf-8"))
    assert "db-conn-mcp" not in stored["mcpServers"]


def test_inject_refuses_unreadable_config(cfg_client, tmp_path, monkeypatch):
    from db_conn_mcp import clients as clients_mod
    from db_conn_mcp.clients import ClientSpec

    client, _ = cfg_client
    broken = ClientSpec("fake", "Fake", tmp_path / "broken.json", "mcpServers")
    original = "{ not json — secret-token-abc123"
    (tmp_path / "broken.json").write_text(original, encoding="utf-8")
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [broken])
    r = client.post("/api/clients/fake/inject", headers=_hdr())
    assert r.status_code == 409
    assert (tmp_path / "broken.json").read_text(encoding="utf-8") == original  # no clobber
    assert "secret-token-abc123" not in r.text  # Rule 6


def test_clients_api_is_post_only_and_guarded(cfg_client):
    """No client config may be rewritten by a GET, or without the token."""
    client, _ = cfg_client
    assert client.get("/api/clients/fake/inject", headers=_hdr()).status_code == 405
    assert client.get("/api/clients/fake/uninject", headers=_hdr()).status_code == 405
    assert client.post("/api/clients/fake/inject").status_code == 403
    assert client.post("/api/clients/fake/uninject").status_code == 403


def test_unknown_client_is_a_404_that_does_not_echo_the_key(cfg_client, monkeypatch):
    from db_conn_mcp import clients as clients_mod

    client, _ = cfg_client
    monkeypatch.setattr(clients_mod, "detected_clients", lambda: [])
    for path in ("/api/clients/ghost-key/inject", "/api/clients/ghost-key/uninject"):
        r = client.post(path, headers=_hdr())
        assert r.status_code == 404
        assert r.json() == {"error": "unknown or undetected client"}
        assert "ghost-key" not in r.text


#: Valid JSON that ``config.load`` still refuses: names must be unique.
_DUPLICATE_NAMES = json.dumps(
    {
        "connections": [
            {"name": "dup", "dsn": SECRET_DSN, "mode": "read"},
            {"name": "dup", "dsn": OTHER_DSN, "mode": "write"},
        ]
    },
    indent=2,
)
_NOT_JSON = '{"connections": [ this is not json'


@pytest.mark.parametrize("broken", [_DUPLICATE_NAMES, _NOT_JSON], ids=["duplicate", "not-json"])
def test_an_unloadable_config_is_never_overwritten(cfg_client, broken):
    """A config file that exists but will not load must be left byte-identical.

    Treating "cannot load" as "empty" would make the very next save wipe every
    connection the user had. Every route reports the fixed 409 instead, and the
    file on disk is untouched.
    """
    client, cfg = cfg_client
    cfg.write_text(broken, encoding="utf-8")
    before = cfg.read_bytes()
    add = {"name": "new", "dsn": OTHER_DSN, "mode": "read"}
    responses = [
        client.get("/api/databases", headers=_hdr()),
        client.post("/api/databases", json=add, headers=_hdr()),
        client.patch("/api/databases/dup", json={"mode": "write"}, headers=_hdr()),
        client.delete("/api/databases/dup", headers=_hdr()),
        client.post("/api/databases/dup/check", headers=_hdr()),
    ]
    for r in responses:
        assert r.status_code == 409, r.request.url
        assert r.json() == {"error": "no configuration found"}
        assert "hunter2-secret" not in r.text
    assert cfg.read_bytes() == before


def test_page_has_all_three_sections_and_no_inline_handlers(client):
    html = client.get(f"/?token={TOKEN}").text
    for anchor in ('id="databases"', 'id="clients"', 'id="verify"'):
        assert anchor in html
    assert "onclick=" not in html  # all behaviour lives in app.js listeners


def test_static_js_never_uses_innerhtml(client):
    js = client.get(f"/static/app.js?token={TOKEN}").text
    assert "innerHTML" not in js


def test_doctor_endpoint_returns_findings(cfg_client, monkeypatch):
    from db_conn_mcp.gui import app as gui_app

    async def fake_run_checks(config_path, *, offline=False):
        return [{"check": "x", "status": "ok", "detail": "d", "suggested_action": "none"}]

    monkeypatch.setattr(gui_app, "run_checks", fake_run_checks)
    client, _ = cfg_client
    r = client.post("/api/doctor", json={"offline": True}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()[0]["status"] == "ok"
