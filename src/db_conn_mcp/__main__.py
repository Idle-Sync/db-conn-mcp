"""Enable ``python -m db_conn_mcp`` as a launch path (used as an injection fallback).

This lets an MCP client start the server via an absolute interpreter path even when
the ``db-conn-mcp`` console script is not on its PATH (e.g. a project-venv install).
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
