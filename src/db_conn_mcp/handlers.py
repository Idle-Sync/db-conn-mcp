"""The tool service layer — pure logic behind the MCP tools, no transport.

Each method maps 1:1 to an MCP tool (see ``server.py``). Keeping this separate from
the FastMCP wiring makes the behavior unit-testable without a transport or a live DB.

Every method that opens a connection routes failures through
:func:`diagnostics.explain`, so an agent never sees a raw, leaky driver error and the
DSN never appears in any result (Rule 6).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, diagnostics, safety
from .dialects.registry import dialect_for


def _safe_filename_stem(name: str) -> str:
    """Make a connection name safe to embed in a filename (Rule 9 spirit).

    Replaces anything outside ``[A-Za-z0-9._-]`` with ``_`` so a connection name can
    never escape the intended directory or produce an odd path.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "database"


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

    async def get_database_schema(
        self, database: str, output_dir: str | None = None, format: str = "json"
    ) -> dict:
        """Return the whole database's schema in one call, as JSON or runnable SQL DDL.

        ``format="json"`` (default) returns ``{database, generated_utc, table_count,
        tables}`` — every table's columns and PK/FK. ``format="sql"`` returns a
        self-contained DDL script under ``ddl`` that recreates the schema (tables,
        columns, sequences, PK/FK/UNIQUE/CHECK, indexes, trigger functions, triggers).

        With ``output_dir`` set, the payload is instead written to
        ``{database}_schema_{UTC}.{json,sql}`` in that directory and a small summary with
        ``saved_to`` is returned — useful for large schemas too big to return inline. The
        content is deterministic; only ``generated_utc`` (and the filename) vary per run.
        """
        if format not in ("json", "sql"):
            raise ValueError("format must be 'json' or 'sql'.")
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            if format == "sql":
                ddl = await dialect.get_database_ddl(db)
            else:
                schema = await dialect.get_database_schema(db)
        finally:
            await db.close()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        if format == "sql":
            if output_dir is None:
                return {"database": database, "generated_utc": stamp, "format": "sql", "ddl": ddl}
            path = self._write_export(output_dir, database, stamp, "sql", ddl)
            return {
                "saved_to": str(path),
                "database": database,
                "generated_utc": stamp,
                "format": "sql",
            }

        payload = {
            "database": database,
            "generated_utc": stamp,
            "table_count": len(schema["tables"]),
            "tables": schema["tables"],
        }
        if output_dir is None:
            return payload
        path = self._write_export(
            output_dir, database, stamp, "json", json.dumps(payload, indent=2, default=str)
        )
        return {
            "saved_to": str(path),
            "database": database,
            "generated_utc": stamp,
            "table_count": payload["table_count"],
        }

    async def dump_schema_faithful(self, database: str, output_dir: str | None = None) -> dict:
        """Faithful schema dump via the database's native tool (Postgres: ``pg_dump``).

        Returns the DDL inline under ``ddl`` (or writes ``{database}_schema_{UTC}.sql`` and
        returns ``saved_to`` when ``output_dir`` is set). If the native tool isn't
        installed, returns ``{status: "pg_dump_not_found", message}`` with install
        guidance so the caller can offer to install it; failures come back sanitized.
        """
        conn = config.get(self._load(), database)
        dialect = dialect_for(conn.dsn)
        result = await dialect.dump_schema_sql(conn.dsn)
        if result.get("status") != "ok":
            return result  # pg_dump_not_found / pg_dump_failed — already sanitized

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ddl = result["ddl"]
        if output_dir is None:
            return {"status": "ok", "database": database, "generated_utc": stamp, "ddl": ddl}
        path = self._write_export(output_dir, database, stamp, "sql", ddl)
        return {"status": "ok", "saved_to": str(path), "database": database, "generated_utc": stamp}

    @staticmethod
    def _write_export(output_dir: str, database: str, stamp: str, ext: str, content: str) -> Path:
        """Write an export to ``{database}_schema_{stamp}.{ext}`` under ``output_dir``."""
        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{_safe_filename_stem(database)}_schema_{stamp}.{ext}"
        path.write_text(content, encoding="utf-8")
        return path

    async def sample_table_rows(self, database: str, table: str, n: int = 10) -> list[dict]:
        """Return the first ``n`` rows of a table."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.sample_rows(db, table, n)
        finally:
            await db.close()

    async def find_columns(self, database: str, pattern: str) -> list[dict]:
        """Fuzzy-search for columns by name across all tables in a database."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.find_columns(db, pattern)
        finally:
            await db.close()

    async def search_value(
        self,
        database: str,
        value: str,
        tables: list[str] | None = None,
        limit_per_column: int = 5,
    ) -> dict:
        """Fuzzy-search for a value across (optionally scoped) tables' data."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.search_value(db, value, tables, limit_per_column)
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
