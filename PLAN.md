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
- [ ] **Dialect ABC:** `dialects/base.py` — the contract: `connect(dsn, *, read_only)`, `list_tables`, `get_schema`, `sample_rows`, `execute`.
- [ ] **Registry:** `dialects/registry.py` — map DSN scheme → `Dialect`; clear error for an unknown scheme.
- [ ] **Postgres Dialect:** `dialects/postgres.py` — `asyncpg` connection/pool, introspection via `information_schema`.
- [ ] **Read-Only Guard:** Inside `PostgresDialect.connect(read_only=True)`, force `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;`. Read-only enforcement is the dialect's own responsibility — it must never leak upward.

## Phase 4: Safety & Diagnostics
- [x] **Write Gate:** `safety.py` — a pure decision function: `mode` (hard reject if not `write`) → `yolo` (proceed) → `user_consent` (proceed if `true`, else reject with the "show SQL, get consent" instruction).
- [x] **Diagnostics:** `diagnostics.py` — `explain(error)` classifies driver exceptions into `AUTH_FAILED` / `HOST_UNREACHABLE` / `DB_NOT_FOUND` / `DNS_FAILURE` / `SSL_REQUIRED` / `POOL_EXHAUSTED` / `UNKNOWN`, each with a **sanitized** cause + fix. Must receive only the exception, never the DSN/connection.

## Phase 5: MCP Tool & Prompt Implementation
- [ ] **Initialize Server:** Set up the core `mcp.server.Server` instance in `server.py`.
- [ ] **Exploration Tools:**
  - `list_databases`: Return configured databases (name + mode + yolo).
  - `list_tables`: Query the DB catalog for tables/views.
  - `get_table_schema`: Query the DB catalog for column definitions.
  - `sample_table_rows`: Fetch the first 10 rows (default).
- [ ] **Execution Tools:**
  - `execute_read_query`: Raw SELECT execution inside a read-only transaction.
  - `execute_write_query`: Raw mutation execution, routed through the `safety.py` gate (`mode` → `yolo` → `user_consent`).
- [ ] **Configuration Tool:**
  - `set_yolo_mode(database, enabled)`: Persist the `yolo` flag for one DB via `config.py`.
- [ ] **Diagnostics Tool:**
  - `check_database(database?)`: Test one DB or all; return `OK` or sanitized diagnostic.
- [ ] **Prompt:**
  - `troubleshoot_connection`: Expose the full connection-gotchas checklist as an MCP prompt.
- [ ] **Error Wrapping:** Every tool that connects routes failures through `diagnostics.explain()` so agents never see raw/leaky errors.
- [ ] **Transports:** Entry points for both `--transport stdio` and `--transport http` (SSE).

## Phase 6: The Setup Wizard (CLI)
- [ ] **Interactive Prompts:** `cli.py` flow asking for scope (Global vs Repo) and the first database DSN + mode.
- [ ] **OS-Agnostic Auto-Discovery:** Locate config files for Claude Desktop (Windows/macOS) and Cursor (Windows/macOS/Linux).
- [ ] **Auto-Injection:** Safely parse the discovered JSON configs and inject the `db-conn-mcp` execution command.

## Phase 7: Future Dialects (Post-v1, Extensibility Payoff)
> Not part of v1. Listed to prove the seam works — each is a single new file + one registry line, no changes above the dialect layer.
- [ ] **MySQL:** `dialects/mysql.py` (`aiomysql`/`asyncmy`), `START TRANSACTION READ ONLY`, `information_schema`.
- [ ] **SQLite:** `dialects/sqlite.py` (`aiosqlite`), open file with `?mode=ro`, introspection via `sqlite_master` / `PRAGMA`.

## Phase 8: Distribution & Marketplaces
- [ ] **Package Build:** Standard Python wheel (`.whl`).
- [ ] **PyPI Publishing:** Upload so users can run `pip install db-conn-mcp`.
- [ ] **Marketplace Submissions:** Official `modelcontextprotocol/servers` registry, Smithery.ai, Glama.ai, MCP.so.
