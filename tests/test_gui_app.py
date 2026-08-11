"""The GUI web app: guard middleware, summary, and (later tasks) the full API."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from db_conn_mcp import __version__
from db_conn_mcp.gui.app import GUI_PORT, TOKEN_HEADER, create_app

TOKEN = "test-token-abcdef"
HOST = f"127.0.0.1:{GUI_PORT}"
SECRET_DSN = "postgresql://user:hunter2-secret@db.internal:5432/prod"


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
