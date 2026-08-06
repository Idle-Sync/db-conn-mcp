"""Pydantic models for ``connections.json`` — the single source of config truth.

These types are database-agnostic: they describe *what* connections exist and how
they may be used, never *how* a specific database is queried (that lives in
``dialects/``). The ``dsn`` is a secret and must never be logged or returned by any
tool (Rule 6); use :meth:`Connection.public_view` for anything agent-facing.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: A connection's allowed access level. ``read`` is an absolute, native boundary.
Mode = Literal["read", "write"]


class Connection(BaseModel):
    """One database connection as declared in ``connections.json``."""

    name: str
    dsn: str  # secret — never logged, never returned by a tool
    mode: Mode
    yolo: bool = False  # optional per-database write-consent bypass; defaults False
    #: Optional ports probed (in order) when the primary refuses — for DSNs behind
    #: tunnels that don't always land on the same local port. None = feature off.
    fallback_ports: list[int] | None = Field(default=None)

    @field_validator("fallback_ports")
    @classmethod
    def _ports_in_range(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(not 1 <= p <= 65535 for p in v):
            raise ValueError("fallback_ports entries must be integers 1-65535")
        return v

    def public_view(self) -> dict:
        """Return the agent-safe projection of this connection (no ``dsn``)."""
        view = {"name": self.name, "mode": self.mode, "yolo": self.yolo}
        if self.fallback_ports is not None:
            view["fallback_ports"] = list(self.fallback_ports)
        return view


class Config(BaseModel):
    """The whole ``connections.json`` document: ``{"connections": [...]}``."""

    connections: list[Connection] = Field(default_factory=list)
