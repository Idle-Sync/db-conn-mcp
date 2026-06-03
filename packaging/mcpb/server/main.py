# /// script
# requires-python = ">=3.10"
# dependencies = ["db-conn-mcp"]
# ///
"""MCPB launcher: `uv run` reads the inline deps above, installs db-conn-mcp from
PyPI into an ephemeral environment, then hands off to its CLI (stdio transport)."""

from db_conn_mcp.cli import main

raise SystemExit(main())
