"""Scaffolding smoke tests: the package imports and the contract is wired.

Behavioral tests (safety truth table, diagnostics sanitation, config resolution,
dialect read-only enforcement) arrive with their implementations — see design spec §12.
"""

from db_conn_mcp import __version__
from db_conn_mcp.dialects.registry import dialect_for, supported_schemes
from db_conn_mcp.models import Connection


def test_version_present():
    assert __version__


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
