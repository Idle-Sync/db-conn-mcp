# AI Agent Rules (CLAUDE.md / .cursorrules)

Welcome, AI Agent (Claude, Agy, Cursor, or otherwise)! 
When assisting the user with the `db-conn-mcp` project, please strictly adhere to the following rules to maintain the project's core philosophy.

> **Note:** This file is canonical. `CLAUDE.md` and `.cursorrules` are exact copies — when you change a rule, update all three so they stay byte-identical.

## 1. Simplicity First (No Over-engineering)
- **DO NOT** introduce complex architectures, custom OAuth servers, or JWT minting.
- **DO NOT** use complex AST parsers to validate read vs. write queries. 
- **DO** rely on native database features. Read-only enforcement lives inside each dialect (for Postgres v1: set the session read-only with `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;`).

## 2. Code Quality & Standards (Production Grade)
- **Ruff:** Use `ruff` as the strict standard for all linting and formatting. Ensure all code passes `ruff check` and `ruff format`.
- **SOLID & KISS:** Code must be highly modular but radically simple. Follow SOLID principles to keep concerns separated (e.g., config parsing vs. database connection vs. MCP transport), but never at the expense of KISS (Keep It Simple, Stupid).
- **DevX & AgentX:** The codebase must be highly readable and easy to modify for *both* human developers and AI agents. Keep files reasonably small, avoid deep nesting, and use explicit typing (`typing` module) everywhere.
- **Documentation:** Well-documented from day one. Every class, module, and non-trivial function must have a clear Python docstring.

## 3. Configuration via JSON
- The single source of truth for database connections is `connections.json`.
- The schema is a top-level object `{ "connections": [ ... ] }`; each connection has `name`, `dsn`, `mode` (`"read"` or `"write"`), and an optional `yolo` (boolean, default `false`).
- Feel free to programmatically read, update, or create `connections.json` for the user if they ask you to add a database.

## 4. Technology Stack
- Keep the project in Python.
- Use the official `mcp` SDK (`pip install mcp`).
- Use standard, modern async libraries where applicable (e.g., `asyncpg`).
- **`pyproject.toml` is the single source of truth for dependencies** — declare all packages there. Do NOT maintain a separate `requirements.txt`.
- **v1 ships PostgreSQL only**, but all DB-specific code lives behind a `Dialect` abstraction (`dialects/`) so adding MySQL/SQLite later is a single new file. Never leak a specific database's SQL above the dialect layer.

## 5. MCP Transport Modes
- The application must support both `stdio` and `http` (SSE) transports.
- Ensure command-line arguments (using `argparse`) dictate which transport is launched.

## 6. Security & Safety
- **Never log DSNs** or passwords to the console. Connection diagnostics must be **sanitized** — describe the *category* of failure and how to fix it, never echo the DSN, host, user, or password.
- Treat the `mode` attribute in `connections.json` as an absolute security boundary. If `mode` is `read`, the AI must not be able to bypass it.
- Writes are gated server-side in this order: `mode` (hard, native) → `yolo` (per-database persisted trust) → `user_consent` (explicit per-operation approval). `yolo` and `user_consent` only relax the *prompt* on a DB that is already `write`; they can NEVER make a `read` DB writable.

## 7. Living Documentation (Always Keep Docs Updated)
- **ALWAYS, ALWAYS, ALWAYS** keep the docs in sync with decisions. The moment a design, scope, or architecture decision is made or accepted, update the relevant docs in the SAME change — never defer it:
  - `PRD.md` — product scope, features, tools.
  - `PLAN.md` — implementation roadmap / phases.
  - `ARCHITECTURE.md` — components, flows, diagrams.
  - These rule files (`AGENT_RULES.md` → copied to `CLAUDE.md` and `.cursorrules`) when a rule itself changes.
- Docs and code must never drift. A change that alters behavior or scope without updating the docs is **incomplete**.

## 8. Commit Hygiene (No Agent Watermarks)
- **NEVER** add agent/AI watermarks, attributions, or trailers to commits, PRs, or any other output — e.g. no `Co-Authored-By: Claude …`, no "Generated with Claude Code", no 🤖 lines. Commits and messages must read like a normal human developer's work.

## 9. Environment & Sanitation
- **Always work inside a project-local virtual environment** (`.venv`): `python -m venv .venv`, activate it, and install there. NEVER install dependencies into the global/system Python. `.venv` is git-ignored.
- **Install from `pyproject.toml`** (the single source of truth — see Rule 4): `pip install -e .`. Do not hand-maintain a `requirements.txt`.
- **Input sanitation:** validate and safely quote any identifier (database/table/column names) before placing it into catalog/introspection queries — never naive string concatenation. Route user-supplied *values* through driver parameterization. (Raw SQL in `execute_read_query`/`execute_write_query` is intentional and governed by the `mode` → `yolo` → `user_consent` gate.)
- **Secrets sanitation:** never commit `connections.json` or `.env` (both git-ignored); never print or log DSNs, hosts, or credentials; keep all error/diagnostic output sanitized (see Rule 6).

## 10. One Concern per Tool (Separate Tools for Separate Things)
- **Each MCP tool — and each CLI subcommand — does exactly ONE well-defined job.** Never multiplex unrelated concerns into a single tool via mode flags or overloaded parameters.
- When a new capability is needed, **add a new tool** rather than bolting another behavior onto an existing one. Small, single-purpose tools stay independently understandable, testable, and safe — and keep the agent-facing surface predictable.
- Example: "find columns by name" and "find a value in the data" are **two** tools, not one search tool with a switch.

Follow these rules rigidly to ensure `db-conn-mcp` remains the cleanest, most straightforward open-source database MCP server available.
