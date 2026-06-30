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
    async def execute(self, conn: Any, sql: str) -> dict:
        """Run raw SQL; return ``{columns, rows}`` or ``{rows_affected}``."""

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
