<!-- mcp-name: io.github.Idle-Sync/db-conn-mcp -->

# db-conn-mcp

[![GitHub stars](https://img.shields.io/github/stars/Idle-Sync/db-conn-mcp?style=social)](https://github.com/Idle-Sync/db-conn-mcp/stargazers)

A dead-simple, self-hosted **Model Context Protocol (MCP) server for querying your databases** with AI agents (Claude, Cursor, Windsurf, VS Code, Zed, and more).

It does one thing well: let an agent **safely explore and query** a database you point it at — with security delegated to the simplest possible primitives (a static JSON file and your database's own read-only transactions), not custom auth servers or fragile SQL parsing.

> **v1 ships PostgreSQL only.** All database-specific code lives behind a `Dialect` seam, so adding MySQL/SQLite later is a single new file.

---

## Why

- **Read stays read.** A `read` database runs every query in a native read-only transaction, *and* the read tool only accepts a single read-only statement (`SELECT`/`WITH`/`VALUES`/`TABLE`/`SHOW`/`EXPLAIN`) — so an agent can't slip in a write or a `SET … READ WRITE` to flip the session. For a hard, privilege-level guarantee that holds no matter what, point the DSN at a **read-only database role** (see [Use a read-only role](#use-a-read-only-role-strongest-guarantee)).
- **No secret leaks.** DSNs/passwords are never logged or returned by any tool. Connection failures come back as **sanitized diagnostics** (a category + fix), never a raw traceback with your host and credentials in it.
- **Tiered write safety.** Writes are gated server-side: `mode` (hard, native) → `yolo` (per-database trust) → `user_consent` (explicit per-operation approval).
- **Zero-friction setup.** An interactive wizard registers your database and injects the server into your AI client's config for you — across 8 popular clients, each in its own format.

---

## Install

Requires **Python 3.10+**.

```bash
# Recommended: isolated but globally available on your PATH
pipx install db-conn-mcp

# or plain pip
pip install db-conn-mcp
```

This installs the `db-conn-mcp` command.

---

## Quick start

```bash
db-conn-mcp setup
```

The wizard asks for:

1. **Scope** — global (`~/.db-conn-mcp/connections.json`) or repo (`./connections.json`).
2. **Connection name** — e.g. `prod`.
3. **DSN** — e.g. `postgresql://user:pass@host:5432/dbname`.
4. **Mode** — `read` (recommended) or `write`.
5. **Client injection** — pick which detected MCP clients to wire up (e.g. `1,3` or `all`).

It then writes your config and (optionally) registers the server in your chosen AI clients. Restart/reconnect the client and the tools are available.

> **Cancelling is safe.** Press Ctrl+C at any prompt and nothing is written.

---

## Configuration

The single source of truth is **`connections.json`**, resolved in this order (first match wins):

1. `--config /path/to/connections.json`
2. `./connections.json` (repo-scoped)
3. `~/.db-conn-mcp/connections.json` (global-scoped)

```json
{
  "connections": [
    { "name": "prod", "dsn": "postgresql://…", "mode": "read" },
    { "name": "dev",  "dsn": "postgresql://…", "mode": "write", "yolo": false }
  ]
}
```

| Field  | Required | Meaning |
|--------|----------|---------|
| `name` | yes | Unique identifier the agent uses to pick a database. |
| `dsn`  | yes | Connection string. **Secret** — never shown by any tool. |
| `mode` | yes | `read` or `write`. An absolute, native security boundary. |
| `yolo` | no (default `false`) | If `true`, skip the per-write consent prompt for this database. |

> `connections.json` is git-ignored by this project's `.gitignore` — never commit real DSNs.

---

## The security model

Writes pass through three gates, **in order**:

1. **`mode` (hard, native).** If the database isn't `"mode": "write"`, the write is rejected — and the connection is opened read-only at the PostgreSQL session level regardless, so it's blocked twice over. `yolo` and `user_consent` can **never** make a `read` database writable.
2. **`yolo` (persisted trust).** On a `write` database with `yolo: true`, writes proceed without prompting.
3. **`user_consent` (per-operation).** Otherwise the agent must first read the schema, show you the exact SQL, get your "yes", and re-call with `user_consent=true`.

Reads always run inside a native read-only transaction, **and** `execute_read_query` accepts only a single read-only statement (`SELECT`/`WITH`/`VALUES`/`TABLE`/`SHOW`/`EXPLAIN`). That allowlist is what stops an agent from sending `SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE` to flip the session, or piggy-backing a `; DELETE …` onto a read — there's no SQL parsing involved, just a leading-keyword check plus the driver's single-command protocol.

### Use a read-only role (strongest guarantee)

The application-level checks above are defense-in-depth. The **hardest** boundary is a privilege one: connect with a PostgreSQL role that simply *cannot* write, so a write fails even if every layer above were bypassed. Create one per database and use its DSN for `read` connections:

```sql
CREATE ROLE agent_ro LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE mydb TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;
```

This is the recommended setup for any database that holds data you care about.

---

## MCP tools

The server exposes **12 tools** and **2 prompts**:

| Tool | Kind | Description |
|------|------|-------------|
| `list_databases` | explore | Configured databases (name, mode, yolo — **no DSN**). |
| `list_tables` | explore | Tables and views in a database. |
| `get_table_schema` | explore | Columns, types, primary/foreign keys for a table. |
| `get_database_schema` | explore | The whole database's schema in one deterministic call. `format="json"` (default) returns every table's columns/types/PK/FK; `format="sql"` returns a **self-contained, runnable DDL script** (tables, sequences, PK/FK/UNIQUE/CHECK, indexes, trigger functions, triggers) — no extra tools required. Pass `output_dir` to write `{database}_schema_{UTC}.{json,sql}` instead of returning it inline (recommended for large DBs). |
| `dump_schema_faithful` | export | **Byte-faithful** schema dump via the database's own `pg_dump --schema-only` — the most complete/runnable export. Requires the `pg_dump` binary on the server host; if missing, returns `pg_dump_not_found` with install guidance (see the `faithful_schema_export` prompt). |
| `sample_table_rows` | explore | First N rows of a table (default 10). |
| `find_columns` | search | Find columns by name across all tables (fuzzy, case-insensitive). |
| `search_value` | search | Find **where** a value appears across tables (fuzzy); returns table/column hits + samples. Pass `tables=[…]` to scope it. |
| `execute_read_query` | execute | Run a single read-only statement (`SELECT`/`WITH`/…) inside a read-only transaction. |
| `execute_write_query` | execute | Run a mutation — gated by the safety model above. |
| `set_yolo_mode` | config | Enable/disable `yolo` for one database (persisted). |
| `check_database` | doctor | Test one database (or all) → `OK` or a sanitized diagnostic. |

**Prompts:**
- `troubleshoot_connection` — a discoverable, full connection-gotchas checklist (host/port, firewall, `sslmode`, Docker `localhost`, db-name case, pool limits, …).
- `faithful_schema_export` — how to choose between the self-contained SQL export and the faithful `pg_dump` one, including how to offer installing `pg_dump`.

---

## CLI reference

`db-conn-mcp` is both the server and a management tool.

| Command | What it does |
|---------|--------------|
| `db-conn-mcp` | Run the server over **stdio** (the default an MCP client uses). Run directly in a terminal it prints guidance and exits — it does not hang. |
| `db-conn-mcp --transport http` | Run over **HTTP (SSE)** instead. |
| `db-conn-mcp setup` | Guided setup; shows status + an action menu if already configured. |
| `db-conn-mcp status` | List configured databases and which clients have the server injected. |
| `db-conn-mcp add` | Add another database connection. |
| `db-conn-mcp clients` | Inject the server into detected MCP clients. |
| `db-conn-mcp clients --remove` | Uninject the server from chosen clients. |
| `db-conn-mcp check [name]` | Probe connectivity (exit `0` all-OK, `2` if any unreachable). |
| `db-conn-mcp remove <name>` | Remove one connection. |
| `db-conn-mcp reset` | Remove **all** connections (delete `connections.json`) — fresh slate. |
| `db-conn-mcp yolo <name> on\|off` | Toggle `yolo` for one database. |
| `db-conn-mcp -v` / `--version` | Print the installed version and the exact build commit, then exit. |

`--config <path>` works before or after any subcommand.

---

## Connecting an AI client

`db-conn-mcp setup` (or `db-conn-mcp clients`) auto-detects and writes the right config for:

**Claude Desktop · Cursor · Windsurf · Agy (Antigravity) · Claude Code · Cline · VS Code · Zed**

Prefer to wire it manually? Use the absolute path the wizard would (so the client can find it regardless of PATH). For a `mcpServers`-style client (Claude Desktop, Cursor, Windsurf, …):

```json
{
  "mcpServers": {
    "db-conn-mcp": {
      "command": "db-conn-mcp",
      "args": ["--config", "/absolute/path/to/connections.json"]
    }
  }
}
```

> If `db-conn-mcp` isn't on the client's PATH (e.g. a project-venv install), use the interpreter form instead: `"command": "/abs/path/to/python", "args": ["-m", "db_conn_mcp", "--config", "…"]`. The `setup`/`clients` commands figure this out for you automatically.

VS Code (`servers` key, `"type": "stdio"`) and Zed (`context_servers`, nested `command`) use different shapes — the wizard handles those too.

---

## Provider notes

- **Railway / managed Postgres over a public proxy:** use the **public** connection URL (e.g. Railway's `DATABASE_PUBLIC_URL`, not the internal `*.railway.internal` one) and append **`?sslmode=require`** — these proxies require SSL with a self-signed cert, which `sslmode=require` accepts without verification.

---

## Development

```bash
git clone https://github.com/Idle-Sync/db-conn-mcp
cd db-conn-mcp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

ruff check . && ruff format --check .
pytest -q
```

`pyproject.toml` is the single source of dependency truth. The codebase is split into single-purpose layers (`config`, `models`, `dialects/`, `safety`, `diagnostics`, `handlers`, `server`, `cli`); only the dialect layer knows a specific database exists. See [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`PRD.md`](./PRD.md), and [`PLAN.md`](./PLAN.md).

---

## Star this repo

If db-conn-mcp saved you time, a ⭐ helps other people find it — it's the only signal that surfaces a small self-hosted tool. [Star it here.](https://github.com/Idle-Sync/db-conn-mcp/stargazers)

---

## License

MIT — see [`LICENSE`](./LICENSE).
