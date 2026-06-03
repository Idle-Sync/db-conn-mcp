"""The tool service layer — pure logic behind the MCP tools, no transport.

Each method maps 1:1 to an MCP tool (see ``server.py``). Keeping this separate from
the FastMCP wiring makes the behavior unit-testable without a transport or a live DB.

Every method that opens a connection routes failures through
:func:`diagnostics.explain`, so an agent never sees a raw, leaky driver error and the
DSN never appears in any result (Rule 6).
"""

from pathlib import Path
from typing import Any

from . import config, diagnostics, safety
from .dialects.registry import dialect_for


def _format_diagnostic(diag: dict) -> str:
    """Render a ``diagnostics.explain`` dict into a single sanitized line."""
    fixes = " ".join(diag["fixes"])
    return f"[{diag['category']}] {diag['cause']} Fix: {fixes}"


class ConnectionFailedError(Exception):
    """A sanitized connection failure. Carries the diagnostic; message is agent-safe."""

    def __init__(self, diag: dict):
        self.diag = diag
        super().__init__(_format_diagnostic(diag))


class Handlers:
    """Bound to one resolved ``connections.json`` path; loads it fresh per call."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)

    def _load(self) -> config.Config:
        return config.load(str(self.config_path))

    async def _connect(self, conn, *, read_only: bool) -> Any:
        """Open a connection, converting any failure into a sanitized error."""
        dialect = dialect_for(conn.dsn)
        try:
            db = await dialect.connect(conn.dsn, read_only=read_only)
        except Exception as exc:  # noqa: BLE001 — intentional: classify & sanitize all
            raise ConnectionFailedError(diagnostics.explain(exc)) from None
        return dialect, db

    # ---- Exploration tools ---------------------------------------------------

    async def list_databases(self) -> list[dict]:
        """Return configured databases as ``{name, mode, yolo}`` — never the DSN."""
        return [c.public_view() for c in self._load().connections]

    async def list_tables(self, database: str) -> list[dict]:
        """Return tables and views for one database."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.list_tables(db)
        finally:
            await db.close()

    async def get_table_schema(self, database: str, table: str) -> dict:
        """Return columns/types and PK/FK for one table."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.get_schema(db, table)
        finally:
            await db.close()

    async def sample_table_rows(self, database: str, table: str, n: int = 10) -> list[dict]:
        """Return the first ``n`` rows of a table."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.sample_rows(db, table, n)
        finally:
            await db.close()

    # ---- Execution tools -----------------------------------------------------

    async def execute_read_query(self, database: str, sql: str) -> dict:
        """Run a single read-only statement inside a native read-only transaction.

        The SQL is validated before any connection opens, so a write-ish or
        session-flipping query (``SET ... READ WRITE``, a piggy-backed ``; DELETE``)
        is rejected without touching the database.
        """
        conn = config.get(self._load(), database)
        dialect_for(conn.dsn).validate_read_only(sql)  # raises ValueError if not read-only
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.execute(db, sql)
        finally:
            await db.close()

    async def execute_write_query(
        self, database: str, sql: str, user_consent: bool = False
    ) -> dict:
        """Run a mutation, gated by ``safety.authorize_write`` (mode → yolo → consent)."""
        conn = config.get(self._load(), database)
        safety.authorize_write(conn, user_consent)  # raises WriteRejected if blocked
        dialect, db = await self._connect(conn, read_only=False)
        try:
            return await dialect.execute(db, sql)
        finally:
            await db.close()

    # ---- Configuration tool --------------------------------------------------

    async def set_yolo_mode(self, database: str, enabled: bool) -> dict:
        """Persist the ``yolo`` flag for one database to ``connections.json``."""
        config.set_yolo(self.config_path, database, enabled)
        return {"database": database, "yolo": enabled, "persisted": True}

    # ---- Diagnostics tool ----------------------------------------------------

    async def check_database(self, database: str | None = None) -> list[dict]:
        """Probe one database (or all) and report ``OK`` or a sanitized diagnostic."""
        cfg = self._load()
        targets = [config.get(cfg, database)] if database else cfg.connections
        report: list[dict] = []
        for conn in targets:
            try:
                _, db = await self._connect(conn, read_only=True)
                await db.close()
                report.append({"database": conn.name, "status": "OK"})
            except ConnectionFailedError as exc:
                report.append(
                    {
                        "database": conn.name,
                        "status": "UNREACHABLE",
                        "category": exc.diag["category"],
                        "detail": str(exc),
                    }
                )
        return report
