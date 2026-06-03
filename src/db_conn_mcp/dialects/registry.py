"""Map a DSN scheme to its :class:`~db_conn_mcp.dialects.base.Dialect`.

Adding a dialect = implement the ABC + add one entry here. No code above this layer
changes.
"""

from urllib.parse import urlsplit

from .base import Dialect
from .postgres import SCHEMES as POSTGRES_SCHEMES
from .postgres import PostgresDialect

#: Scheme -> Dialect instance. One line per supported database.
_REGISTRY: dict[str, Dialect] = {scheme: PostgresDialect() for scheme in POSTGRES_SCHEMES}


def supported_schemes() -> list[str]:
    """Return the sorted list of DSN schemes the registry can resolve."""
    return sorted(_REGISTRY)


def dialect_for(dsn: str) -> Dialect:
    """Return the dialect for ``dsn``'s scheme.

    Raises :class:`ValueError` (naming the supported schemes) on an unknown scheme.
    """
    scheme = urlsplit(dsn).scheme.lower()
    try:
        return _REGISTRY[scheme]
    except KeyError:
        raise ValueError(
            f"Unsupported DSN scheme {scheme!r}. Supported: {', '.join(supported_schemes())}."
        ) from None
