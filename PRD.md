# Product Requirements Document (PRD): db-conn-mcp

> **Companion docs:** see [`ARCHITECTURE.md`](./ARCHITECTURE.md) for component flows/diagrams and [`PLAN.md`](./PLAN.md) for the build roadmap. Per project rules, all three are kept in sync with every decision.

> **v1 scope:** PostgreSQL only. The architecture is built around a `Dialect` seam so MySQL and SQLite can be added later as a single new file each, with no changes above the dialect layer.

## 1. Goal
Provide a dead-simple, self-hosted Model Context Protocol (MCP) server for securely querying databases via AI agents (Claude, Agy, Cursor, etc.). 

## 2. Core Philosophy
Simplicity over complexity. `db-conn-mcp` delegates security and configuration to the simplest possible primitives: static JSON configuration and native database session characteristics.

## 3. Target Audience
Developers and power users looking for a fully open-source, easily self-hostable solution to connect their databases to AI assistants with zero friction.

## 4. Key Features
- **JSON-Driven Configuration:** All database connections are managed via a single `connections.json` file. The server resolves this file in the following order (giving the user maximum flexibility):
  1. Explicit path passed via CLI: `--config /path/to/connections.json`
  2. Repo-scoped: `./connections.json` in the current working directory.
  3. Globally scoped: `~/.db-conn-mcp/connections.json` (in the user's home directory).
- **Explicit Access Controls (Allowlist):** Each database in the JSON config is explicitly marked as `read` or `write`. 
- **Native Security Enforcement:** "Read-only" enforcement is done at the database transaction level (for Postgres: `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;`), implemented inside each dialect.
- **Tiered Write Safety (`yolo`):** Writes to a `write`-mode DB still require **explicit per-operation user consent** by default. An optional per-database `yolo: true` flag (persisted in `connections.json`) lets a trusted DB skip that prompt. The full gate is `mode` → `yolo` → `user_consent`; `yolo`/consent can never make a `read` DB writable.
- **Self-Diagnosing Connections (the doctor):** When a DB is unreachable, the server returns a **sanitized** diagnostic (classified cause + how to fix) instead of a raw error — and never leaks the DSN/host/credentials. Validation is lazy (the server boots even if a DB is down); a `check_database` tool probes proactively and a `troubleshoot_connection` prompt offers the full gotchas checklist.
- **Multi-Transport Support:** `stdio` (local IDEs) and `http` (remote agents).
- **Interactive Setup CLI:** Ships with a `python setup.py` wizard that asks the user for their preferred configuration scope (global vs repo), bootstraps the first database, and **auto-detects installed AI agents** (Claude Desktop, Cursor, etc.) to inject the MCP server directly into their config files without manual JSON editing.

## 5. MCP Tools

To provide the best Agent Experience (AgentX), the server exposes specific tools for safe exploration and execution:

### Exploration Tools (Safe)
1. **`list_databases`**: Reads `connections.json` and returns the available database names and their allowed mode (`read` or `write`).
2. **`list_tables`**: Returns a list of all tables and views in a specified database.
3. **`get_table_schema`**: Returns the exact schema (columns, data types, primary/foreign keys) for a specific table so the AI can write accurate SQL.
4. **`sample_table_rows`**: Fetches the first *N* rows (default 10) of a table. Crucial for the AI to understand formatting (e.g., are dates ISO strings or timestamps? Are enums uppercase or lowercase?).

### Execution Tools
5. **`execute_read_query`**: Runs custom `SELECT` queries. Enforced natively by the database as a read-only transaction.
6. **`execute_write_query`**: Runs `UPDATE`, `INSERT`, `DELETE`, or DDL. Gated server-side, in order:
   - **Security 1 (`mode`):** Immediately rejected if the database is not allowlisted as `"mode": "write"` in JSON. This boundary is absolute.
   - **Security 2 (`yolo`):** If the database has `yolo: true`, the write proceeds without a consent prompt.
   - **Security 3 (`user_consent`):** Otherwise the tool requires a `user_consent: true` parameter and is rejected without it. The tool description strictly instructs the AI: *"First read the table and its schema, then print the exact SQL you plan to run to the user and ask for their explicit permission. Only call again with `user_consent=true` if they say yes."*

### Configuration Tools
7. **`set_yolo_mode`**: Sets `yolo` (`true`/`false`) for one named database and **persists it to `connections.json`**, so the choice survives restarts. Per-database — enabling `yolo` on one DB never affects another.

### Diagnostics Tools
8. **`check_database`**: Tests connectivity for one named database (or all of them) and returns `OK` or a sanitized, classified cause + fix. Never echoes the DSN/credentials.

### MCP Prompts
- **`troubleshoot_connection`**: A discoverable prompt exposing the full connection-gotchas checklist (host/port, firewall, `sslmode`, Docker `localhost` vs container, db-name case, pool limits, …) the agent can pull at any time.

## 6. Technical Stack
- **Language:** Python 3.10+
- **Core Library:** Official `mcp` Python SDK (`pip install mcp`).
- **Database Drivers (v1):** `asyncpg` for PostgreSQL. Future dialects bring their own driver (e.g. `aiomysql`, `aiosqlite`) behind the `Dialect` seam.
- **Validation:** `pydantic` for the `connections.json` models.

### `connections.json` schema
```json
{
  "connections": [
    { "name": "prod", "dsn": "postgresql://…", "mode": "read" },
    { "name": "dev",  "dsn": "postgresql://…", "mode": "write", "yolo": false }
  ]
}
```
`yolo` is optional and defaults to `false`. Resolution order (first match wins): `--config <path>` → `./connections.json` → `~/.db-conn-mcp/connections.json`.

## 7. Distribution & Marketplace Strategy
To make `db-conn-mcp` globally accessible and trivial to install, it will be packaged and distributed across major MCP marketplaces:
1. **PyPI Package:** The project will be published to the Python Package Index so users can install it globally via `pip install db-conn-mcp`.
2. **Official MCP Registry:** We will submit a PR to the official `modelcontextprotocol/servers` GitHub registry.
3. **Community Marketplaces:** The server will be registered on platforms like **Smithery.ai**, **Glama.ai**, and **MCP.so**. These platforms allow users to one-click install MCP servers directly into Claude Desktop or Cursor.
4. **OS-Agnostic Setup:** The CLI wizard (`setup.py`) will automatically detect the host OS (Windows, macOS, Linux) and target the correct config paths for Cursor (`~/.cursor`) and Claude Desktop (`%APPDATA%` on Win, `~/Library/Application Support` on Mac).

## 8. Ultimate Goal
To become the standard, one-click installable open-source plugin for anyone who wants to connect their database to an AI agent locally or on a private network, distributed across all major MCP marketplaces.
