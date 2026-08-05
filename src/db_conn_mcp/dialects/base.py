"""The ``Dialect`` ABC — the extensibility contract every database must satisfy.

A dialect is one database family and the only place DB-specific SQL/behavior lives,
*including* native read-only enforcement (which must never leak upward).
"""

from abc import ABC, abstractmethod
from typing import Any


class Dialect(ABC):
    """One database family. Implement this + register it to add a new database."""

    #: The DSN scheme this dialect handles, e.g. ``"postgresql"``.
    scheme: str

    @abstractmethod
    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        """Open a connection.

        When ``read_only=True``, enforce it *natively* before returning the
        connection (Postgres: ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY``).
        """

    @abstractmethod
    async def list_tables(self, conn: Any) -> list[dict]:
        """Return tables and views as ``[{schema, name, kind}]``."""

    @abstractmethod
    async def get_schema(self, conn: Any, table: str) -> dict:
        """Return columns, types, nullability, and PK/FK for one table."""

    @abstractmethod
    async def get_database_schema(self, conn: Any) -> dict:
        """Return the whole database's schema in one deterministic pass.

        Returns ``{"tables": [{schema, name, columns, primary_key, foreign_keys}]}``
        covering every non-system base table, with columns ordered by position and
        tables ordered by ``schema, name`` so repeated runs are byte-identical. No
        user-supplied identifiers are involved, so there is no injection surface.
        """

    @abstractmethod
    async def get_database_ddl(self, conn: Any) -> str:
        """Return a runnable, self-contained DDL script recreating the whole schema.

        Assembled entirely from native catalog functions (Postgres: ``pg_get_*def``),
        ordered so the script runs top-to-bottom: schemas → sequences → tables →
        constraints (PK/UNIQUE/CHECK/FK, FKs last so references resolve) → indexes →
        trigger functions → triggers. No external tooling; covers the common cases.
        Identifiers are quoted by the database, so there is no injection surface.
        """

    @abstractmethod
    async def dump_schema_sql(self, dsn: str) -> dict:
        """Faithful schema dump via the database's own native dump tool (Postgres: ``pg_dump``).

        Takes the ``dsn`` rather than a live connection because the native tool manages
        its own connection. Returns one of:
        ``{"status": "ok", "ddl": str}`` on success,
        ``{"status": "pg_dump_not_found", "message": str}`` if the tool isn't installed,
        ``{"status": "pg_dump_failed", "message": str}`` on a (sanitized) failure.
        The message is always agent-safe — it never echoes the DSN, host, user, or
        password (Rule 6).
        """

    @abstractmethod
    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        """Return the first ``n`` rows. The identifier MUST be safely quoted (Rule 9)."""

    @abstractmethod
    async def execute(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run raw SQL; return ``{columns, rows}`` or ``{rows_affected}``.

        ``params`` are bound via the driver's native parameterization (Postgres:
        ``$1``/``$2`` placeholders) — never interpolated into the SQL text. A
        ``timeout_ms`` cancels the statement and raises a sanitized ``ValueError``
        when exceeded.
        """

    @abstractmethod
    async def execute_dry_run(
        self,
        conn: Any,
        sql: str,
        params: list[Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Run ``sql`` inside a transaction, report the outcome, then ROLL BACK.

        Returns the same shape as :meth:`execute` plus ``{"dry_run": True,
        "rolled_back": True}``. Nothing commits, but the statement really executes
        first (native transaction semantics): it briefly takes locks, may advance
        sequences, and fires triggers before the rollback discards their effects.
        """

    @abstractmethod
    async def open_cursor(self, conn: Any, sql: str, params: list[Any] | None = None) -> Any:
        """Open a native server-side cursor for a read-only query.

        Returns a cursor handle owning ``conn`` and its transaction, exposing
        ``await fetch(n) -> list[dict]`` and ``await close()`` (which must also
        close the underlying connection). The caller keeps the handle alive
        across tool calls; the dialect never stores it.
        """

    @abstractmethod
    async def get_object_definition(self, conn: Any, object_type: str, name: str) -> list[dict]:
        """Return faithful definitions for non-table objects by name.

        ``object_type`` is one of ``view``/``function``/``trigger``/``sequence``/
        ``index``; ``name`` may be schema-qualified. Returns every match (same
        name in several schemas, overloaded functions) as
        ``[{schema, name, kind, definition}]`` — definitions produced by the
        database's own ``pg_get_*def``-style functions, never hand-assembled.
        Raises ``ValueError`` for an unknown ``object_type``.
        """

    @abstractmethod
    async def cancel_backend(self, conn: Any, pid: int) -> dict:
        """Ask the server to cancel the query running in backend ``pid`` (native).

        Returns ``{"pid": pid, "cancelled": bool}`` — ``False`` means no such
        backend or no permission (only same-user backends can be cancelled
        without superuser). Cancels the *statement*; the session survives.
        """

    @abstractmethod
    async def explain(self, conn: Any, sql: str, analyze: bool = False) -> dict:
        """Return the query plan as ``{"plan": [str, ...]}``.

        With ``analyze=True`` the statement is actually executed to collect real
        timings — callers must only pass validated read-only SQL, and the
        read-only session blocks any write natively as defense-in-depth.
        """

    @abstractmethod
    async def check_sequences(self, conn: Any) -> list[dict]:
        """Report every column-owned sequence vs. the max value in its column.

        Returns ``[{sequence_schema, sequence, table_schema, table, column,
        last_value, is_called, max_id, behind}]`` where ``behind=True`` means the
        next sequence value would collide with existing data (the classic
        post-migration stale-sequence problem).
        """

    @abstractmethod
    async def table_stats(self, conn: Any) -> list[dict]:
        """Approximate row counts and on-disk sizes per user table.

        Returns ``[{schema, table, approx_rows, table_bytes, index_bytes,
        total_bytes}]`` ordered largest-first. Row counts come from the
        database's own statistics (fast, approximate) — no table scans.
        """

    @abstractmethod
    async def show_activity(self, conn: Any, include_query: bool = False) -> list[dict]:
        """Sanitized view of current server activity (who holds connections).

        Never includes user names or client addresses; query text only when
        ``include_query=True`` (truncated). Returns ``[{pid, state, wait_event_type,
        wait_event, backend_type, application_name, query_age, transaction_age[, query]}]``.
        """

    @abstractmethod
    def validate_read_only(self, sql: str) -> None:
        """Raise ``ValueError`` if ``sql`` is not a single read-only statement.

        Defense-in-depth for the read tool: the native read-only transaction is a
        *session* setting an attacker-influenced query could flip (Postgres: ``SET
        SESSION CHARACTERISTICS ... READ WRITE``) or smuggle a second statement past.
        This pure check rejects anything that isn't one read-only, row-returning
        statement, *before* a connection is opened.
        """

    @abstractmethod
    async def find_columns(self, conn: Any, pattern: str) -> list[dict]:
        """Fuzzy (case-insensitive substring) search for columns by name across tables.

        Returns ``[{schema, table, column, type, nullable}]`` for columns whose name
        matches ``pattern``.
        """

    @abstractmethod
    async def search_value(
        self, conn: Any, value: str, tables: list[str] | None = None, limit_per_column: int = 5
    ) -> dict:
        """Fuzzy search for ``value`` in the data across (optionally scoped) tables.

        When ``tables`` is given, only those are scanned; otherwise all non-system,
        non-junk base tables. Returns
        ``{"results": [{schema, table, column, matches, samples}], "truncated": bool}``.
        """
