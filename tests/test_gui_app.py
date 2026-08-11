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
