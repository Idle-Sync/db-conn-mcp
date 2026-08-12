"""The tool service layer — pure logic behind the MCP tools, no transport.

Each method maps 1:1 to an MCP tool (see ``server.py``). Keeping this separate from
the FastMCP wiring makes the behavior unit-testable without a transport or a live DB.

Every method that opens a connection routes failures through
:func:`diagnostics.explain`, so an agent never sees a raw, leaky driver error and the
DSN never appears in any result (Rule 6).
"""

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import config, diagnostics, safety
from .dialects.registry import dialect_for

#: Most cursors an agent may hold open at once (each pins a DB connection).
MAX_OPEN_CURSORS = 5

#: Idle cursors are reaped after this many seconds (checked on every cursor call).
CURSOR_IDLE_TTL_SECONDS = 900.0

#: Deadline for a single fallback-port PROBE attempt (issue #10).
#: Applies only to fallback candidates — the primary DSN keeps the driver's own
#: connect behavior. A refused port fails instantly, but a dropped/firewalled one
#: hangs for the driver's default (~60s); without this cap, probing several ports
#: would stack those hangs into minutes.
FALLBACK_CONNECT_TIMEOUT_SECONDS = 5.0

#: How long a successful dry-run authorizes committing the identical statement.
DRY_RUN_GRANT_TTL_SECONDS = 600.0


def _safe_filename_stem(name: str) -> str:
    """Make a connection name safe to embed in a filename (Rule 9 spirit).

    Replaces anything outside ``[A-Za-z0-9._-]`` with ``_`` so a connection name can
    never escape the intended directory or produce an odd path.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "database"


def _dsn_with_port(dsn: str, port: int) -> str | None:
    """Return ``dsn`` with the netloc port replaced (or appended); ``None`` if unparsable.

    Pure string surgery on the URL netloc — credentials pass through untouched and
    are never logged (Rule 6). ``None`` tells the caller to skip port fallbacks and
    behave exactly as if the feature were off.
    """
    try:
        parts = urlsplit(dsn)
        if not parts.scheme or parts.hostname is None:
            return None
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    creds, _, hostport = parts.netloc.rpartition("@")
    # Strip an existing ":port" — including an empty one ("h:") that ``parts.port``
    # reports as None. The ``]`` check keeps bare IPv6 literals (``[::1]``) intact.
    host = hostport.rsplit(":", 1)[0] if hostport.rfind(":") > hostport.rfind("]") else hostport
    netloc = f"{creds}@{host}:{port}" if creds else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _format_diagnostic(diag: dict) -> str:
    """Render a ``diagnostics.explain`` dict into a single sanitized line."""
    fixes = " ".join(diag["fixes"])
    return f"[{diag['category']}] {diag['cause']} Fix: {fixes}"


def _fk_view(fks: list[dict]) -> list[tuple]:
    """Order-insensitive, comparable view of a table's foreign keys."""
    return sorted((fk["column"], fk.get("references") or "") for fk in fks)


def _diff_schemas(schema_a: dict, schema_b: dict) -> dict:
    """Pure structural diff of two ``get_database_schema`` results.

    Compares table presence, then per common table: column presence, column
    attributes (type/nullable/default), primary key, and foreign keys. Output is
    deterministic (sorted) so repeated runs are diffable.
    """

    def key(t: dict) -> str:
        return f"{t['schema']}.{t['name']}"

    a = {key(t): t for t in schema_a["tables"]}
    b = {key(t): t for t in schema_b["tables"]}
    only_in_a = sorted(set(a) - set(b))
    only_in_b = sorted(set(b) - set(a))

    changed: list[dict] = []
    for table in sorted(set(a) & set(b)):
        ta, tb = a[table], b[table]
        cols_a = {c["name"]: c for c in ta["columns"]}
        cols_b = {c["name"]: c for c in tb["columns"]}
        entry: dict[str, Any] = {}
        if set(cols_a) - set(cols_b):
            entry["columns_only_in_a"] = sorted(set(cols_a) - set(cols_b))
        if set(cols_b) - set(cols_a):
            entry["columns_only_in_b"] = sorted(set(cols_b) - set(cols_a))
        changed_cols = [
            {"column": name, "a": cols_a[name], "b": cols_b[name]}
            for name in sorted(set(cols_a) & set(cols_b))
            if cols_a[name] != cols_b[name]
        ]
        if changed_cols:
            entry["changed_columns"] = changed_cols
        if ta["primary_key"] != tb["primary_key"]:
            entry["primary_key"] = {"a": ta["primary_key"], "b": tb["primary_key"]}
        if _fk_view(ta["foreign_keys"]) != _fk_view(tb["foreign_keys"]):
            entry["foreign_keys"] = {"a": ta["foreign_keys"], "b": tb["foreign_keys"]}
        if entry:
            changed.append({"table": table, **entry})

    return {
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "changed_tables": changed,
        "identical": not (only_in_a or only_in_b or changed),
    }


class ConnectionFailedError(Exception):
    """A sanitized connection failure. Carries the diagnostic; message is agent-safe."""

    def __init__(self, diag: dict):
        self.diag = diag
        super().__init__(_format_diagnostic(diag))


class Handlers:
    """Bound to one resolved ``connections.json`` path; loads it fresh per call."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        #: Open server-side cursors: id -> {cursor, database, last_used}.
        self._cursors: dict[str, dict] = {}
        #: Fallback-port memory: connection name -> port that answered (issue #10).
        self._active_ports: dict[str, int] = {}
        #: Dry-run grants: statement fingerprint -> monotonic time it was previewed.
        self._dry_run_grants: dict[str, float] = {}

    def _load(self) -> config.Config:
        return config.load(str(self.config_path))

    def _dsn_candidates(self, conn: Any) -> list[tuple[int | None, str]]:
        """Return ``(port, dsn)`` pairs in probe order; ``None`` port = primary DSN.

        Remembered active port (if any) moves to the front; the rest keep the
        configured order. Duplicates are skipped — including a fallback that merely
        repeats the primary DSN's own port. Without ``fallback_ports`` this is just
        the primary.
        """
        candidates: list[tuple[int | None, str]] = [(None, conn.dsn)]
        try:
            seen: set[int | None] = {urlsplit(conn.dsn).port}
        except ValueError:
            seen = set()
        for port in conn.fallback_ports or []:
            if port in seen:
                continue
            dsn = _dsn_with_port(conn.dsn, port)
            if dsn is not None:
                seen.add(port)
                candidates.append((port, dsn))
        remembered = self._active_ports.get(conn.name)
        if remembered is not None:
            front = [c for c in candidates if c[0] == remembered]
            candidates = front + [c for c in candidates if c[0] != remembered]
        return candidates

    async def _connect(self, conn: Any, *, read_only: bool) -> Any:
        """Open a connection, converting any failure into a sanitized error.

        With ``fallback_ports`` configured, a refused/timed-out primary is retried
        on each fallback port in order (auth/TLS/etc. errors fail immediately —
        never masked by probing). Each fallback probe is capped at
        ``FALLBACK_CONNECT_TIMEOUT_SECONDS`` so a black-holed port cannot stall the
        whole chain. The winning fallback port is remembered for this process; a
        primary success forgets it.
        """
        dialect = dialect_for(conn.dsn)
        candidates = self._dsn_candidates(conn)
        tried_fallbacks: list[int] = []
        last_diag: dict | None = None
        for port, dsn in candidates:
            try:
                if port is None:
                    db = await dialect.connect(dsn, read_only=read_only)
                else:
                    db = await asyncio.wait_for(
                        dialect.connect(dsn, read_only=read_only),
                        timeout=FALLBACK_CONNECT_TIMEOUT_SECONDS,
                    )
            except TimeoutError:
                # A probe that runs out its deadline means "nothing answered here" —
                # keep probing rather than abort the chain. Classified locally so the
                # outcome never depends on how ``diagnostics`` happens to bucket a
                # timeout; ``explain`` of a builtin ``TimeoutError`` yields the canned,
                # sanitized HOST_UNREACHABLE advice (no DSN, host, or user).
                last_diag = diagnostics.explain(TimeoutError())
                if port is not None:
                    tried_fallbacks.append(port)
                continue
            except Exception as exc:  # noqa: BLE001 — intentional: classify & sanitize all
                diag = diagnostics.explain(exc)
                if diag["category"] != "HOST_UNREACHABLE":
                    if port is not None:
                        # Say which probed port produced it — port number only (Rule 6).
                        # ``failed_port`` is the structured form of the same fact, so
                        # callers can tell "failed on a fallback" from "failed on the
                        # primary" without parsing the prose.
                        diag = dict(diag)
                        diag["cause"] += f" (on fallback port {port})"
                        diag["failed_port"] = port
                    raise ConnectionFailedError(diag) from None
                last_diag = diag
                if port is not None:
                    tried_fallbacks.append(port)
                continue
            if port is None:
                self._active_ports.pop(conn.name, None)
            else:
                self._active_ports[conn.name] = port
            return dialect, db
        assert last_diag is not None  # loop ran at least once (primary)
        if tried_fallbacks:
            last_diag = dict(last_diag)
            ports = ", ".join(str(p) for p in tried_fallbacks)
            last_diag["cause"] += f" (also tried fallback port(s): {ports})"
        raise ConnectionFailedError(last_diag) from None

    # ---- Exploration tools ---------------------------------------------------

    async def list_databases(self) -> list[dict]:
        """Return configured databases as ``{name, mode, yolo, ...}`` — never the DSN."""
        rows = []
        for c in self._load().connections:
            view = c.public_view()
            if c.name in self._active_ports:
                view["active_port"] = self._active_ports[c.name]
            rows.append(view)
        return rows

    async def list_tables(
        self, database: str, pattern: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Return tables and views for one database.

        ``pattern`` keeps only names containing it (case-insensitive, the same match
        :meth:`find_columns` uses); ``limit`` returns at most that many rows. The shape is
        always a plain list — a bounded answer is simply shorter.
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.list_tables(db, pattern, limit)
        finally:
            await db.close()

    async def get_table_schema(
        self, database: str, table: str, include_indexes: bool = False
    ) -> dict:
        """Return columns/types and PK/FK for one table.

        ``include_indexes=True`` adds an ``indexes`` key (name, columns, unique,
        method) from the same connection. The default answer is unchanged and does
        not carry the key at all — purely additive for existing consumers.
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            schema = await dialect.get_schema(db, table)
            if include_indexes:
                schema["indexes"] = await dialect.table_indexes(db, table)
            return schema
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

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

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

        Uses the remembered fallback port when one is active, so the native tool dials
        the same port the driver did. The subprocess never probes — one port, one shot.
        """
        conn = config.get(self._load(), database)
        dialect = dialect_for(conn.dsn)
        active_port = self._active_ports.get(conn.name)
        dsn = conn.dsn
        if active_port is not None:
            dsn = _dsn_with_port(conn.dsn, active_port) or conn.dsn
        result = await dialect.dump_schema_sql(dsn)
        if result.get("status") != "ok":
            return result  # pg_dump_not_found / pg_dump_failed — already sanitized

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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

    async def find_columns(
        self, database: str, pattern: str, limit: int | None = None
    ) -> list[dict]:
        """Fuzzy-search for columns by name across all tables in a database.

        ``limit`` returns at most that many rows; the shape stays a plain list.
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.find_columns(db, pattern, limit)
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

    async def execute_read_query(
        self,
        database: str,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run a single read-only statement inside a native read-only transaction.

        The SQL is validated before any connection opens, so a write-ish or
        session-flipping query (``SET ... READ WRITE``, a piggy-backed ``; DELETE``)
        is rejected without touching the database. ``params`` bind via the driver
        (Postgres ``$1``/``$2``); ``timeout_ms`` cancels an overrunning query.
        """
        conn = config.get(self._load(), database)
        dialect_for(conn.dsn).validate_read_only(sql)  # raises ValueError if not read-only
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.execute(db, sql, params, timeout_ms)
        finally:
            await db.close()

    @staticmethod
    def _grant_key(database: str, sql: str, params: list[Any] | None) -> str:
        """Fingerprint one exact statement; any change to sql/params misses the grant."""
        payload = json.dumps([database, sql, params], default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _sweep_grants(self) -> None:
        """Drop dry-run grants older than the TTL."""
        now = time.monotonic()
        for key in [
            k for k, t in self._dry_run_grants.items() if now - t > DRY_RUN_GRANT_TTL_SECONDS
        ]:
            del self._dry_run_grants[key]

    async def execute_write_query(
        self,
        database: str,
        sql: str,
        params: list[Any] | None = None,
        user_consent: bool = False,
        dry_run: bool = True,
        skip_dry_run: bool = False,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run a mutation, gated by ``safety`` (mode → dry-run-first → yolo → consent).

        ``dry_run=True`` (the default) executes inside a transaction that is always
        ROLLED BACK and records a grant for this exact statement. A commit
        (``dry_run=False``) requires an unexpired grant for the identical
        (database, sql, params); the commit ATTEMPT consumes it, whether it
        succeeds or raises, so a failed commit needs a fresh preview before it may
        be retried. ``skip_dry_run=True`` attests the user explicitly asked to skip
        the preview. Neither yolo nor consent can bypass the preview stage; nothing
        can bypass ``mode``.
        """
        conn = config.get(self._load(), database)
        self._sweep_grants()
        key = self._grant_key(database, sql, params)
        if dry_run:
            safety.authorize_dry_run(conn)  # mode gate only — nothing commits
        else:
            safety.authorize_commit(
                conn,
                user_consent,
                has_grant=key in self._dry_run_grants,
                skip_dry_run=skip_dry_run,
            )
        dialect, db = await self._connect(conn, read_only=False)
        try:
            if dry_run:
                result = await dialect.execute_dry_run(db, sql, params, timeout_ms)
                self._dry_run_grants[key] = time.monotonic()
                return result
            # One preview authorizes exactly ONE commit attempt. Consuming the grant
            # BEFORE the attempt means a failed commit — which may still have applied
            # server-side — forces a fresh preview instead of a blind, double-applying
            # retry.
            self._dry_run_grants.pop(key, None)
            return await dialect.execute(db, sql, params, timeout_ms)
        finally:
            await db.close()

    async def explain_query(
        self,
        database: str,
        sql: str,
        analyze: bool = False,
        params: list[Any] | None = None,
    ) -> dict:
        """Return the query plan for a read-only statement (optionally ANALYZE).

        The SQL is validated read-only first; ``analyze=True`` really executes the
        statement to gather timings, which the read-only session keeps safe.
        ``params`` bind through the same driver mechanism ``execute_read_query``
        uses (Postgres ``$1``/``$2``), so the plan explained is the plan the query
        an agent is about to run will actually get.
        """
        conn = config.get(self._load(), database)
        dialect_for(conn.dsn).validate_read_only(sql)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.explain(db, sql, analyze, params)
        finally:
            await db.close()

    async def cancel_query(self, database: str, pid: int) -> dict:
        """Cancel the statement running in server backend ``pid`` (native cancel).

        Find candidate pids with ``show_activity``. Cancels the statement only —
        the target session survives. ``cancelled=False`` means no such backend or
        no permission (non-superusers can only cancel their own user's backends).
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            return await dialect.cancel_backend(db, pid)
        finally:
            await db.close()

    # ---- Cursor tools (large result sets) --------------------------------------

    def _reap_idle_cursors(self) -> list:
        """Collect cursors idle past the TTL; returns their states for closing."""
        now = time.monotonic()
        expired = [
            cid
            for cid, state in self._cursors.items()
            if now - state["last_used"] > CURSOR_IDLE_TTL_SECONDS
        ]
        return [self._cursors.pop(cid) for cid in expired]

    async def _close_cursor_state(self, state: dict) -> None:
        # Best-effort cleanup — the connection may already be dead.
        with contextlib.suppress(Exception):
            await state["cursor"].close()

    async def open_query_cursor(
        self, database: str, sql: str, params: list[Any] | None = None
    ) -> dict:
        """Open a server-side cursor over a read-only query; returns a ``cursor_id``.

        The cursor holds one dedicated read-only connection until closed. Fetch
        batches with ``fetch_rows``; always ``close_cursor`` when done (idle
        cursors are auto-reaped after 15 minutes, and a fully drained cursor
        closes itself).
        """
        for state in self._reap_idle_cursors():
            await self._close_cursor_state(state)
        if len(self._cursors) >= MAX_OPEN_CURSORS:
            raise ValueError(
                f"Too many open cursors (max {MAX_OPEN_CURSORS}). "
                "Close one with close_cursor before opening another."
            )
        conn = config.get(self._load(), database)
        dialect_for(conn.dsn).validate_read_only(sql)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            cursor = await dialect.open_cursor(db, sql, params)
        except Exception:
            await db.close()
            raise
        cursor_id = uuid.uuid4().hex[:12]
        self._cursors[cursor_id] = {
            "cursor": cursor,
            "database": database,
            "last_used": time.monotonic(),
        }
        return {"cursor_id": cursor_id, "database": database}

    async def fetch_rows(self, cursor_id: str, n: int = 100) -> dict:
        """Fetch the next ``n`` rows from an open cursor.

        Returns ``{rows, row_count, exhausted, cursor_closed}``; the cursor is
        closed automatically once exhausted.
        """
        for state in self._reap_idle_cursors():
            await self._close_cursor_state(state)
        state = self._cursors.get(cursor_id)
        if state is None:
            raise ValueError(
                f"Unknown cursor_id {cursor_id!r} — it may have expired "
                "(15 min idle limit) or already been closed."
            )
        state["last_used"] = time.monotonic()
        rows = await state["cursor"].fetch(n)
        exhausted = len(rows) < max(1, int(n))
        if exhausted:
            del self._cursors[cursor_id]
            await self._close_cursor_state(state)
        return {
            "rows": rows,
            "row_count": len(rows),
            "exhausted": exhausted,
            "cursor_closed": exhausted,
        }

    async def close_cursor(self, cursor_id: str) -> dict:
        """Close an open cursor and release its connection. Idempotent."""
        state = self._cursors.pop(cursor_id, None)
        if state is not None:
            await self._close_cursor_state(state)
        return {"cursor_id": cursor_id, "closed": True, "was_open": state is not None}

    # ---- Object-definition tool ------------------------------------------------

    async def get_object_definition(self, database: str, object_type: str, name: str) -> dict:
        """Faithful definition(s) of a view/function/trigger/sequence/index by name."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            matches = await dialect.get_object_definition(db, object_type, name)
        finally:
            await db.close()
        return {"object_type": object_type, "name": name, "matches": matches}

    # ---- Operational insight tools ----------------------------------------------

    async def diff_schemas(self, database_a: str, database_b: str) -> dict:
        """Structural schema diff between two configured databases (tables/columns/keys)."""
        schemas: list[dict] = []
        for name in (database_a, database_b):
            conn = config.get(self._load(), name)
            dialect, db = await self._connect(conn, read_only=True)
            try:
                schemas.append(await dialect.get_database_schema(db))
            finally:
                await db.close()
        return {
            "database_a": database_a,
            "database_b": database_b,
            **_diff_schemas(schemas[0], schemas[1]),
        }

    async def check_sequences(self, database: str, behind_only: bool = True) -> dict:
        """Report sequences whose next value would collide with existing rows.

        By default only the problem sequences are listed; ``behind_only=False`` returns
        the full census. Either way ``total_sequences`` says how many were inspected, so
        ``behind_count: 0`` is an affirmative "nothing is stale" answer rather than an
        empty one.
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            report = await dialect.check_sequences(db, behind_only)
        finally:
            await db.close()
        sequences = report["sequences"]
        return {
            "database": database,
            "sequences": sequences,
            "behind_count": sum(1 for s in sequences if s["behind"]),
            "total_sequences": report["total_sequences"],
        }

    async def table_stats(
        self,
        database: str,
        limit: int | None = None,
        min_size_bytes: int | None = None,
        table: str | None = None,
    ) -> dict:
        """Approximate row counts and disk/index sizes per table, largest first.

        ``table`` narrows to one table (optionally schema-qualified), ``min_size_bytes``
        drops anything smaller, and ``limit`` returns only the top N by total size. When
        ``limit`` is set the result also carries ``truncated`` — ``True`` when more tables
        matched than were returned. Without ``limit`` the payload is exactly as before.
        """
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            tables = await dialect.table_stats(db, limit, min_size_bytes, table)
        finally:
            await db.close()
        if limit is None:
            return {"database": database, "tables": tables}
        # The dialect fetched one row past the limit; its presence is the truncation signal.
        capped = max(0, int(limit))
        return {
            "database": database,
            "tables": tables[:capped],
            "truncated": len(tables) > capped,
        }

    async def show_activity(self, database: str, include_query: bool = False) -> dict:
        """Sanitized server activity — what is holding connections right now."""
        conn = config.get(self._load(), database)
        dialect, db = await self._connect(conn, read_only=True)
        try:
            activity = await dialect.show_activity(db, include_query)
        finally:
            await db.close()
        return {"database": database, "activity": activity}

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
                row = {"database": conn.name, "status": "OK"}
                if conn.name in self._active_ports:
                    row["active_port"] = self._active_ports[conn.name]
                report.append(row)
            except ConnectionFailedError as exc:
                row = {
                    "database": conn.name,
                    "status": "UNREACHABLE",
                    "category": exc.diag["category"],
                    "detail": str(exc),
                }
                # Present only when the failure came from a probed fallback port.
                if "failed_port" in exc.diag:
                    row["failed_port"] = exc.diag["failed_port"]
                report.append(row)
        return report
