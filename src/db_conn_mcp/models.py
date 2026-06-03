"""Pydantic models for ``connections.json`` — the single source of config truth.

These types are database-agnostic: they describe *what* connections exist and how
they may be used, never *how* a specific database is queried (that lives in
``dialects/``). The ``dsn`` is a secret and must never be logged or returned by any
tool (Rule 6); use :meth:`Connection.public_view` for anything agent-facing.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: A connection's allowed access level. ``read`` is an absolute, native boundary.
Mode = Literal["read", "write"]


class Connection(BaseModel):
    """One database connection as declared in ``connections.json``."""

    name: str
    dsn: str  # secret — never logged, never returned by a tool
    mode: Mode
    yolo: bool = False  # optional per-database write-consent bypass; defaults False

    def public_view(self) -> dict:
        """Return the agent-safe projection of this connection (no ``dsn``)."""
        return {"name": self.name, "mode": self.mode, "yolo": self.yolo}


class Config(BaseModel):
    """The whole ``connections.json`` document: ``{"connections": [...]}``."""

    connections: list[Connection] = Field(default_factory=list)
