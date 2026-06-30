# Implementation Plan: db-conn-mcp

This document outlines the step-by-step roadmap to build the `db-conn-mcp` server, ensuring we strictly follow the [`PRD.md`](./PRD.md), the architecture in [`ARCHITECTURE.md`](./ARCHITECTURE.md), and the SOLID/KISS principles in [`AGENT_RULES.md`](./AGENT_RULES.md).

> **v1 scope:** PostgreSQL only, built behind a `Dialect` seam so adding MySQL/SQLite later is a single new file (see Phase 7). Per project rules, keep PRD/PLAN/ARCHITECTURE in sync with every decision.

## Phase 1: Project Scaffolding & Tooling
- [x] **Virtual Environment:** Create and use a project-local `.venv` (`python -m venv .venv`) for all work (git-ignored).
- [x] **Repository Structure:** Set up the standard Python package layout (`src/db_conn_mcp/`) per the module map in `ARCHITECTURE.md`.
- [x] **Code Quality:** Initialize `ruff` configuration for aggressive, consistent linting and formatting.
- [x] **Dependencies:** Declare all packages in `pyproject.toml` (the single source of truth — `mcp`, `asyncpg`, `pydantic`); install with `pip install -e .`. No `requirements.txt`.
- [x] **.gitignore:** Python caches, `.venv`, build artifacts, and secrets (`connections.json`, `.env`).

## Phase 2: Configuration Management
- [x] **Models:** `models.py` — pydantic `Connection{name, dsn, mode, yolo}` and `Config{connections: [...]}`.
- [x] **Config Parser:** `config.py` — read and validate `connections.json` (top-level `{"connections": [...]}` object; `yolo` optional, defaults `false`).
- [x] **Fallback Resolution Strategy:** Implement the 3-tier lookup:
  1. `--config` argument
  2. `./connections.json` (repo-scoped)
  3. `~/.db-conn-mcp/connections.json` (global-scoped)
- [x] **Validation:** Strictly enforce the `read`/`write` mode and a known DSN scheme.
- [x] **Save support:** `config.py` can rewrite `connections.json` (needed by `set_yolo_mode`).

## Phase 3: Database Interaction Layer (Dialect Seam)
- [x] **Dialect ABC:** `dialects/base.py` — the contract: `connect(dsn, *, read_only)`, `list_tables`, `get_schema`, `sample_rows`, `execute`.
- [x] **Registry:** `dialects/registry.py` — map DSN scheme → `Dialect`; clear error for an unknown scheme.
- [x] **Postgres Dialect:** `dialects/postgres.py` — `asyncpg` connection/pool, introspection via `information_schema`.
- [x] **Read-Only Guard:** Inside `PostgresDialect.connect(read_only=True)`, force `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;`. Read-only enforcement is the dialect's own responsibility — it must never leak upward.

## Phase 4: Safety & Diagnostics
- [x] **Write Gate:** `safety.py` — a pure decision function: `mode` (hard reject if not `write`) → `yolo` (proceed) → `user_consent` (proceed if `true`, else reject with the "show SQL, get consent" instruction).
- [x] **Diagnostics:** `diagnostics.py` — `explain(error)` classifies driver exceptions into `AUTH_FAILED` / `HOST_UNREACHABLE` / `DB_NOT_FOUND` / `DNS_FAILURE` / `SSL_REQUIRED` / `POOL_EXHAUSTED` / `UNKNOWN`, each with a **sanitized** cause + fix. Must receive only the exception, never the DSN/connection.

## Phase 5: MCP Tool & Prompt Implementation
- [x] **Initialize Server:** Set up the server in `server.py` using the SDK's high-level `FastMCP` API (simpler than the low-level `mcp.server.Server`; Rule 1). Tool logic lives in `handlers.py` so it is unit-testable without a transport.
- [x] **Exploration Tools:**
  - `list_databases`: Return configured databases (name + mode + yolo).
  - `list_tables`: Query the DB catalog for tables/views.
  - `get_table_schema`: Query the DB catalog for column definitions.
  - `get_database_schema`: Query the catalog for every table's columns + PK/FK in one deterministic pass. `format="sql"` instead emits a self-contained, runnable DDL script (sequences, tables, constraints, indexes, trigger functions, triggers) from native `pg_get_*def` functions.
  - `dump_schema_faithful`: Byte-faithful schema via the DB's own `pg_dump --schema-only`; DSN passed through `PG*` env (never argv), errors sanitized; `pg_dump_not_found` with install guidance when the binary is absent.
  - `sample_table_rows`: Fetch the first 10 rows (default).
- [x] **Execution Tools:**
  - `execute_read_query`: Raw SELECT execution inside a read-only transaction.
  - `execute_write_query`: Raw mutation execution, routed through the `safety.py` gate (`mode` → `yolo` → `user_consent`).
- [x] **Configuration Tool:**
  - `set_yolo_mode(database, enabled)`: Persist the `yolo` flag for one DB via `config.py`.
- [x] **Diagnostics Tool:**
  - `check_database(database?)`: Test one DB or all; return `OK` or sanitized diagnostic.
- [x] **Prompts:**
  - `troubleshoot_connection`: Expose the full connection-gotchas checklist as an MCP prompt.
  - `faithful_schema_export`: Guide choosing the self-contained vs. `pg_dump` schema export, including offering to install `pg_dump`.
- [x] **Error Wrapping:** Every tool that connects routes failures through `diagnostics.explain()` so agents never see raw/leaky errors.
- [x] **Transports:** Entry points for both `--transport stdio` and `--transport http` (SSE).

## Phase 6: The Setup Wizard (CLI)
- [x] **Interactive Prompts:** `cli.py` flow asking for scope (Global vs Repo) and the first database DSN + mode. `register_database` validates the DSN scheme and refuses both a **duplicate name** and a **duplicate connection string** (the latter naming the existing connection, never echoing the DSN). `config.load` stays permissive so the same DSN under two names/modes (e.g. a read + a write entry) remains a hand-editable advanced pattern.
- [x] **Transactional & interruptible:** the wizard gathers *all* answers (DB details + injection choices) before writing anything, then commits. **Ctrl+C / EOF at any prompt cancels cleanly** ("Setup cancelled. Nothing was saved.", exit 130) — no partial state, no traceback. Injection target is a numbered multi-select (`1,3` / `all` / Enter to skip), scaling to N detected clients.
- [x] **OS-Agnostic Auto-Discovery:** Locate config files for 8 MCP clients via `ClientSpec` (key, path, format): Claude Desktop, Cursor, Agy/Antigravity (`~/.gemini/config/mcp_config.json`), Windsurf (`~/.codeium/windsurf/mcp_config.json`), Claude Code (`~/.claude.json`), Cline, VS Code, and Zed. The wizard lists the **detected** clients (config file exists) before offering to inject. *(Gemini CLI deliberately excluded — retired 2026-06-18, superseded by Agy.)*
- [x] **Auto-Injection:** Safely read-merge-write each client's JSON in its **own format** — `mcpServers` (Claude Desktop/Cursor/Agy/Windsurf/Claude Code/Cline), `servers` + `type:stdio` (VS Code), `context_servers` + nested `command:{path,args}` + `source:custom` (Zed) — asking per client and preserving existing entries.
- [x] **Resolvable launch command:** injected entries use `server_launch()` — the **absolute path** of the installed console script (`shutil.which`), or a fallback of the current interpreter + `python -m db_conn_mcp` (via `__main__.py`). This works whether installed in a project `.venv` or globally/pipx, so the client never needs `db-conn-mcp` on its own PATH (a bare command caused `-32000` failures).
- [x] **Management subcommands:** beyond first-run `setup` (which now shows status + an action menu when a config already exists), the CLI exposes `status` (list DBs + per-client injection state, no DSN), `add` (append another connection), `clients` (inject) / `clients --remove` (uninject), `check [name]` (connectivity doctor from the CLI — sanitized, exit 0 all-OK / 2 if any unreachable), `remove <name>`, and `yolo <name> on|off`. `--config` works before or after the subcommand. All interactive flows are Ctrl+C-safe.

## Phase 7: Future Dialects (Post-v1, Extensibility Payoff)
> Not part of v1. Listed to prove the seam works — each is a single new file + one registry line, no changes above the dialect layer.
- [ ] **MySQL:** `dialects/mysql.py` (`aiomysql`/`asyncmy`), `START TRANSACTION READ ONLY`, `information_schema`.
- [ ] **SQLite:** `dialects/sqlite.py` (`aiosqlite`), open file with `?mode=ro`, introspection via `sqlite_master` / `PRAGMA`.

## Phase 8: Distribution & Marketplaces
- [x] **Package Build:** Standard Python wheel (`.whl`) + sdist build via `python -m build` (hatchling). Verified the wheel ships all modules and the `db-conn-mcp` console-script entry point.
- [ ] **PyPI Publishing:** Upload so users can run `pip install db-conn-mcp`. *(Deferred — requires a PyPI account/API token; run `python -m build` then `twine upload dist/*`.)*
- [ ] **Marketplace Submissions:** Official `modelcontextprotocol/servers` registry, Smithery.ai, Glama.ai, MCP.so. *(Deferred — external account/PR steps.)*
