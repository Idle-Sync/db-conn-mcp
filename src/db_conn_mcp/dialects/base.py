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
