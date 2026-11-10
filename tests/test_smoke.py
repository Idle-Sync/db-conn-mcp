"""Scaffolding smoke tests: the package imports and the contract is wired.

Behavioral tests (safety truth table, diagnostics sanitation, config resolution,
dialect read-only enforcement) arrive with their implementations — see design spec §12.
"""

import json
import tomllib
from pathlib import Path

from db_conn_mcp import __version__
from db_conn_mcp.dialects.registry import dialect_for, supported_schemes
from db_conn_mcp.models import Connection

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_present():
    assert __version__


def test_every_published_version_matches_the_package():
    """A release bump edits five places; PyPI, the MCP Registry and the MCPB bundle
    each read a different one, so a missed edit ships two versions under one tag."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server = json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))
    manifest_path = REPO_ROOT / "packaging" / "mcpb" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    published = [
        pyproject["project"]["version"],
        server["version"],
        *(package["version"] for package in server["packages"]),
        manifest["version"],
    ]
    assert published == [__version__] * len(published)


def test_connection_public_view_hides_dsn():
    conn = Connection(name="prod", dsn="postgresql://secret", mode="read")
    view = conn.public_view()
    assert view == {"name": "prod", "mode": "read", "yolo": False}
    assert "dsn" not in view


def test_registry_resolves_postgres_schemes():
    assert "postgresql" in supported_schemes()
    assert dialect_for("postgresql://localhost/db").scheme == "postgresql"
    assert dialect_for("postgres://localhost/db").scheme == "postgresql"


def test_registry_rejects_unknown_scheme():
    import pytest

    with pytest.raises(ValueError, match="Unsupported DSN scheme"):
        dialect_for("mysql://localhost/db")
