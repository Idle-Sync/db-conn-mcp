"""db-conn-mcp: a dead-simple, self-hosted MCP server for querying databases.

v1 ships PostgreSQL only, built behind a ``Dialect`` seam (``dialects/``) so adding
MySQL/SQLite later is a single new file. See ``ARCHITECTURE.md`` for the full design.
"""

__version__ = "0.2.1"
