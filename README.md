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
- **Tiered write safety.** Writes are gated server-side: `mode` (hard, native) → **dry-run first** (a commit is refused unless that exact statement was previewed) → `yolo` (per-database trust) → `user_consent` (explicit per-operation approval).
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

### Upgrade

**Not sure how you installed it?** Each manager keeps its own registry — ask them:

```bash
pipx list                 # db-conn-mcp listed here → pipx
uv tool list              # listed here → uv tool
pip show db-conn-mcp      # found in the current Python env → pip
```

If none of those know it, your MCP client config is likely launching it via **`uvx`** (check the `command` in the client's config entry — that entry is the source of truth for what actually runs, and `which db-conn-mcp` / `Get-Command db-conn-mcp` shows the binary on your PATH).

Then use the matching upgrade command:

```bash
pipx upgrade db-conn-mcp          # pipx
pip install --upgrade db-conn-mcp # pip
uv tool upgrade db-conn-mcp       # uv tool
```

Running via **`uvx`** (e.g. in an MCP client config)? There's nothing installed to upgrade, but uvx caches resolved versions — use `db-conn-mcp@latest` as the command to always resolve the newest release, or run `uv cache clean db-conn-mcp` to force a re-resolve.

After upgrading, **restart/reconnect your AI client** so it picks up the new version (and any new tools). Verify with `db-conn-mcp -v`.

> PyPI's index can lag a release by a minute or two. If your upgrade reports "already at latest" right after a release, retry with `pipx upgrade db-conn-mcp --pip-args="--no-cache-dir"` (or `pip install --upgrade --no-cache-dir db-conn-mcp`).

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
5. **Fallback ports** *(optional)* — comma-separated extra ports to try if the primary one refuses; press Enter to skip.
6. **Client injection** — pick which detected MCP clients to wire up (e.g. `1,3` or `all`).

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
| `yolo` | no (default `false`) | If `true`, skip the per-write consent prompt for this database. The dry-run preview still applies. |
| `fallback_ports` | no | Extra ports to probe, in order, when the primary port refuses or times out. See below. |

- **`fallback_ports`** *(optional)* — extra ports probed **in order** when the
  primary port refuses or times out (auth/TLS errors fail immediately and are never
  masked). For DSNs behind SSH tunnels that don't always land on the same port.
  The winning port is remembered for the server's lifetime and shown as
  `active_port` in `list_databases` / `check_database`. Hand-edit the JSON (agents
  may too) or answer the wizard prompt. Example:
  `"fallback_ports": [5433, 15432]`

Probing is strictly opt-in and bounded: only the ports you list are ever tried (no
scanning), each **fallback** probe is capped at 5 seconds, and a connection without the key behaves
exactly as it always has. Existing `connections.json` files keep working untouched —
the server never adds the key to a file that doesn't have it.

> `connections.json` is git-ignored by this project's `.gitignore` — never commit real DSNs.

---

## The security model

Writes pass through four gates, **in order**:

1. **`mode` (hard, native).** If the database isn't `"mode": "write"`, the write is rejected — and the connection is opened read-only at the PostgreSQL session level regardless, so it's blocked twice over. Nothing — not `yolo`, not `user_consent`, not `skip_dry_run` — can **ever** make a `read` database writable.
2. **Dry-run first (server-enforced).** `execute_write_query` defaults to `dry_run=true`. A commit is **rejected** unless the identical statement was dry-run first. The one escape hatch is `skip_dry_run=true`, which the agent passes to *attest* that you explicitly asked to skip the preview — the server can't verify that claim, so it's the same trust model as `user_consent` (and, like it, useless on a `read` database). `yolo` does **not** waive this stage.
3. **`yolo` (persisted trust).** On a `write` database with `yolo: true`, the previewed write commits without prompting.
4. **`user_consent` (per-operation).** Otherwise the agent must first read the schema, show you the exact SQL, get your "yes", and re-call with `user_consent=true`.

**What the dry-run does:** it executes the statement in a transaction and **always rolls back**, returning the rows it *would* have affected — so only the `mode` gate applies, nothing commits, and you see the real impact before saying yes. The resulting permission to commit is scoped to that exact statement (same database, SQL, and params), **expires after 10 minutes**, and is **consumed** by the commit it authorizes — running the same statement twice means previewing it twice. (A dry-run does still execute server-side until rollback: brief locks, sequence advancement, trigger side effects.)

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

The server exposes **23 tools** and **2 prompts**:

| Tool | Kind | Description |
|------|------|-------------|
| `list_databases` | explore | Configured databases (name, mode, yolo, and `active_port` when a fallback port is in use — **no DSN**). |
| `list_tables` | explore | Tables and views in a database. |
| `get_table_schema` | explore | Columns, types, primary/foreign keys for a table. |
| `get_database_schema` | explore | The whole database's schema in one deterministic call. `format="json"` (default) returns every table's columns/types/PK/FK; `format="sql"` returns a **self-contained, runnable DDL script** (tables, sequences, PK/FK/UNIQUE/CHECK, indexes, trigger functions, triggers) — no extra tools required. Pass `output_dir` to write `{database}_schema_{UTC}.{json,sql}` instead of returning it inline (recommended for large DBs). |
| `dump_schema_faithful` | export | **Byte-faithful** schema dump via the database's own `pg_dump --schema-only` — the most complete/runnable export. Requires the `pg_dump` binary on the server host; if missing, returns `pg_dump_not_found` with install guidance (see the `faithful_schema_export` prompt). |
| `sample_table_rows` | explore | First N rows of a table (default 10). |
| `find_columns` | search | Find columns by name across all tables (fuzzy, case-insensitive). |
| `search_value` | search | Find **where** a value appears across tables (fuzzy); returns table/column hits + samples. Pass `tables=[…]` to scope it. |
| `get_object_definition` | explore | Faithful SQL definition of a **view / function / trigger / sequence / index** by name (native `pg_get_*def`; overloads and all schemas returned). |
| `execute_read_query` | execute | Run a single read-only statement (`SELECT`/`WITH`/…) inside a read-only transaction. Optional `params` (**real bind parameters** via `$1`/`$2` — no quoting pitfalls) and `timeout_ms`. |
| `execute_write_query` | execute | Run a mutation — gated by the safety model above. **Defaults to `dry_run=true`**: execute in a transaction, report would-be `rows_affected`, always ROLL BACK — show the user real impact *before* consenting to the real write. Committing (`dry_run=false`) requires a prior preview of the identical statement, which expires after 10 minutes and is consumed by its commit; pass `skip_dry_run=true` only when the user explicitly asks to skip the preview. Also takes `params`/`timeout_ms`. |
| `explain_query` | execute | `EXPLAIN` (optionally `ANALYZE`) a validated read-only query — confirm index usage without any write access. |
| `cancel_query` | execute | Cancel the statement in a given backend `pid` (native `pg_cancel_backend`); the session survives. Find pids via `show_activity`. |
| `open_query_cursor` | cursor | Open a server-side cursor over a read-only query for **large result sets**; returns a `cursor_id`. Max 5 open; 15-min idle auto-reap. |
| `fetch_rows` | cursor | Fetch the next N rows from an open cursor; auto-closes when drained. |
| `close_cursor` | cursor | Close a cursor and release its connection (idempotent). |
| `diff_schemas` | insight | Structural schema diff between two configured databases (tables, columns, types, defaults, PK/FK) — verify a migrated copy matches its source. |
| `check_sequences` | insight | Find sequences **behind** their column's max value (the silent post-migration breakage); fix with `setval()`. |
| `table_stats` | insight | Approximate row counts + disk/index sizes per table, largest first (statistics only, no scans). |
| `show_activity` | insight | Sanitized `pg_stat_activity`: pid, state, wait events, query age — **no user names, client addresses, or query text** (text is opt-in and truncated). |
| `set_yolo_mode` | config | Enable/disable `yolo` for one database (persisted). |
| `check_database` | doctor | Test one database (or all) → `OK` or a sanitized diagnostic; reports `active_port` when a fallback port answered, and `failed_port` on an `UNREACHABLE` row when the failure that ended the probe chain (auth, TLS, DB-not-found, …) came from a probed fallback port rather than the primary. |
| `doctor` | doctor | Diagnose the **whole setup**, not just connectivity: stale running server processes, a newer PyPI release, `connections.json` key typos/wrong types, secrets exposure (file permissions, git), MCP client entries pointing at dead paths, and per-database connectivity with a credential-free fallback-port identity probe. Returns `{check, status, detail, suggested_action}` rows; `offline=true` skips the PyPI lookup. |

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
| `db-conn-mcp status` | List configured databases (including `fallback_ports` where configured) and which clients have the server injected. |
| `db-conn-mcp add` | Add another database connection. |
| `db-conn-mcp clients` | Inject the server into detected MCP clients. |
| `db-conn-mcp clients --remove` | Uninject the server from chosen clients. |
| `db-conn-mcp check [name]` | Probe connectivity (exit `0` all-OK, `2` if any unreachable). |
| `db-conn-mcp doctor` | Diagnose the **whole setup** — stale processes, a newer release, config-schema typos, secrets exposure, client entries, connectivity (exit `0` if nothing failed, `2` if any check fails). `--offline` skips **only** the PyPI version lookup; the database probes still run. |
| `db-conn-mcp remove <name>` | Remove one connection. |
| `db-conn-mcp reset` | Remove **all** connections (delete `connections.json`) — fresh slate. |
| `db-conn-mcp yolo <name> on\|off` | Toggle `yolo` for one database. |
| `db-conn-mcp -v` / `--version` | Print the installed version and the exact build commit, then exit. |

`--config <path>` works before or after any subcommand.

> **Optional dependency:** the doctor's `process_staleness` check needs [`psutil`](https://pypi.org/project/psutil/) to inspect running processes. It is **not** required — without it that one check reports `skipped` and every other check runs normally. To enable it, install the `doctor` extra — `pip install "db-conn-mcp[doctor]"` (or `pipx install "db-conn-mcp[doctor]"`) — or, for an install you already have via pipx, `pipx inject db-conn-mcp psutil`.

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

`pyproject.toml` is the single source of dependency truth. The codebase is split into single-purpose layers (`config`, `models`, `dialects/`, `safety`, `diagnostics`, `doctor`, `clients`, `handlers`, `server`, `cli`); only the dialect layer knows a specific database exists. See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md), [`docs/PRD.md`](./docs/PRD.md), and [`docs/PLAN.md`](./docs/PLAN.md).

---

## Star this repo

If db-conn-mcp saved you time, a ⭐ helps other people find it — it's the only signal that surfaces a small self-hosted tool. [Star it here.](https://github.com/Idle-Sync/db-conn-mcp/stargazers)

---

## License

MIT — see [`LICENSE`](./LICENSE).
