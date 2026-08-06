"""db-conn-mcp: a dead-simple, self-hosted MCP server for querying databases.

v1 ships PostgreSQL only, built behind a ``Dialect`` seam (``dialects/``) so adding
MySQL/SQLite later is a single new file. See ``docs/ARCHITECTURE.md`` for the full design.
"""

__version__ = "0.5.0"

# Full git commit hash this build was cut from. Left as "unknown" for editable /
# source installs; the release workflow stamps it with $GITHUB_SHA before building
# the wheel, so `db-conn-mcp -v` reports the exact commit installed on a device.
__commit__ = "unknown"
