"""The GUI web app: guard middleware, summary, and (later tasks) the full API."""

import pytest
from starlette.testclient import TestClient

from db_conn_mcp import __version__
from db_conn_mcp.gui.app import GUI_PORT, TOKEN_HEADER, create_app

TOKEN = "test-token-abcdef"
HOST = f"127.0.0.1:{GUI_PORT}"


@pytest.fixture()
def client():
    app = create_app(TOKEN, config_arg=None)
    return TestClient(app, base_url=f"http://{HOST}")


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
