# Design Spec: `db-conn-mcp` (v1)

**Date:** 2026-06-03
**Status:** Approved design — ready for implementation planning
**Companion docs:** [`PRD.md`](../../../PRD.md) · [`PLAN.md`](../../../PLAN.md) · [`ARCHITECTURE.md`](../../../ARCHITECTURE.md) · rules in [`AGENT_RULES.md`](../../../AGENT_RULES.md)

> This spec consolidates every decision made during brainstorming and adds the implementation-level detail (interfaces, data shapes, error handling, testing) needed to scaffold the project in a fresh session. It is the single source of design truth; if it conflicts with PRD/PLAN/ARCHITECTURE, fix all of them (Rule 7).

---

## 1. Goal & Scope

A dead-simple, self-hostable MCP server that lets AI agents safely explore and query databases. Security and config are delegated to the simplest primitives: a static JSON file and native database features.

**v1 scope (this spec):** PostgreSQL only, built behind a `Dialect` seam so MySQL/SQLite are a one-file add later.

### Non-goals (YAGNI)
- No custom OAuth / JWT / auth server.
- No AST-based SQL parsing to classify read vs. write — enforcement is native (read-only transactions).
- No ORM. No SQLAlchemy. Raw async driver behind a thin adapter.
- No MySQL/SQLite implementation in v1 (only the seam that makes them trivial).
- No HTTP authentication layer in v1 (bind to localhost; see §9 open question).

---

## 2. Architecture

Five single-purpose layers; only the dialect layer knows a specific database exists. (Full diagrams: `ARCHITECTURE.md`.)

```
src/db_conn_mcp/
├── __init__.py
├── cli.py           # argparse: setup wizard + `--config`, `--transport`
├── config.py        # resolve / load / validate / save connections.json
├── models.py        # pydantic: Connection, Config
├── dialects/
│   ├── base.py      # Dialect ABC — the extensibility contract
│   ├── postgres.py  # PostgresDialect (asyncpg) — only impl in v1
│   └── registry.py  # scheme -> Dialect
├── safety.py        # pure write-gate decision
├── diagnostics.py   # classify driver errors -> sanitized cause + fix
├── handlers.py      # the 8 tool handlers as plain async methods (transport-free)
└── server.py        # FastMCP app: registers tools + prompt onto handlers, transport
```

**SDK choice:** the server uses the official SDK's high-level `FastMCP` API rather than
the low-level `mcp.server.Server` — it is radically simpler (Rule 1) and the same SDK.
Tool *logic* lives in `handlers.py` (plain async methods) so it is unit-testable without
a transport; `server.py` only wires those methods onto FastMCP tools/prompts.

**Dependency direction:** `server` → `handlers` → (`config`, `safety`, `diagnostics`, `dialects/registry`) → `dialects/postgres` → `asyncpg`. `safety` and `diagnostics` are pure and import no DB driver.

---

## 3. Data Models (`models.py`)

```python
from typing import Literal, List
from pydantic import BaseModel, Field

Mode = Literal["read", "write"]

class Connection(BaseModel):
    name: str
    dsn: str                      # secret — never logged or returned by any tool
    mode: Mode
    yolo: bool = False            # optional; per-database write-consent bypass

class Config(BaseModel):
    connections: List[Connection] = Field(default_factory=list)
```

- `name` is unique within a config; duplicate names are a validation error.
- `dsn` scheme must be resolvable by the registry (`postgresql://` / `postgres://` in v1); unknown scheme → clear validation error naming the supported schemes.
- A public-safe view (`name`, `mode`, `yolo` — **never `dsn`**) is what `list_databases` returns.

---

## 4. Configuration (`config.py`)

**Resolution order (first existing wins):**
1. `--config <path>` (explicit)
2. `./connections.json` (repo-scoped)
3. `~/.db-conn-mcp/connections.json` (global-scoped)

**Responsibilities:**
- `load() -> Config` — resolve path, parse JSON, validate via pydantic. Missing file at all three locations → actionable error telling the user how to create one (or run `setup`).
- `get(name) -> Connection` — lookup by name; unknown name → error listing available names.
- `save(config)` / `set_yolo(name, enabled)` — atomic rewrite of `connections.json` (write temp + replace) preserving formatting/indentation. Only `set_yolo` mutates config in v1; the file path written is the one that was resolved on load.

---

## 5. Dialect Seam (`dialects/`)

### 5.1 The contract (`base.py`)
```python
from abc import ABC, abstractmethod
from typing import Any, Sequence

class Dialect(ABC):
    """One database family. The ONLY place DB-specific SQL/behavior lives."""
    scheme: str  # e.g. "postgresql"

    @abstractmethod
    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        """Open a connection. When read_only=True, enforce it natively
        (Postgres: SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY) before returning."""

    @abstractmethod
    async def list_tables(self, conn: Any) -> list[dict]:
        """Tables + views: [{schema, name, kind}]."""

    @abstractmethod
    async def get_schema(self, conn: Any, table: str) -> dict:
        """Columns, types, nullability, PK/FK for one table."""

    @abstractmethod
    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        """First n rows. Identifier must be safely quoted (Rule 9)."""

    @abstractmethod
    async def execute(self, conn: Any, sql: str) -> dict:
        """Run raw SQL; return {columns, rows} or {rows_affected}."""
```

### 5.2 PostgresDialect (`postgres.py`)
- Driver: `asyncpg`.
- `connect(read_only=True)`: open connection, run `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (session-level read-only) before handing it back.
- Introspection via `information_schema` (`tables`, `columns`, `table_constraints`/`key_column_usage` for PK/FK).
- `sample_rows`: quote the identifier safely (validate against catalog or use `format('%I')`-style quoting via `quote_ident`); never f-string the table name raw.
- Connection lifecycle: open-per-operation in v1 (simple, correct). A pool is an optional later optimization, not required for v1 correctness.

### 5.3 Registry (`registry.py`)
- `dialect_for(dsn) -> Dialect` parses the scheme and returns the matching dialect instance; unknown scheme → error listing supported schemes. Adding a dialect = implement `base.Dialect` + register one entry.

---

## 6. Safety Model (`safety.py`)

A **pure function** — no I/O — so it's trivially testable and the boundary is unmissable.

```python
def authorize_write(conn: Connection, user_consent: bool) -> None:
    """Raise WriteRejected with a clear reason if the write must not proceed."""
```

Decision order (also see flowchart in ARCHITECTURE.md §5):

| Step | Condition | Result |
|---|---|---|
| 1 | `conn.mode != "write"` | **REJECT** — "DB is read-only (mode=read)." (hard, native; this can never be bypassed) |
| 2 | `conn.yolo is True` | **ALLOW** |
| 3 | `user_consent is True` | **ALLOW** |
| 4 | otherwise | **REJECT** — instruct: read table+schema, show exact SQL to the user, re-call with `user_consent=true` |

`yolo` and `user_consent` only relax the prompt on an already-`write` DB. Read-only is *also* enforced natively at the DB (defense in depth): a `read` connection physically cannot mutate.

---

## 7. Diagnostics (`diagnostics.py`)

```python
def explain(error: Exception) -> dict:
    """Map a driver exception to {category, cause, fixes[]}.
    Receives ONLY the exception — never a Connection/DSN — so nothing leaks."""
```

**Categories** (asyncpg/OS error → sanitized advice):

| Category | Trigger (examples) | Cause + fix (sanitized) |
|---|---|---|
| `AUTH_FAILED` | `InvalidPasswordError`, role does not exist | Wrong user/password or missing role; verify credentials/role. |
| `HOST_UNREACHABLE` | connection refused, timeout | DB not accepting connections; check it's running, host/port, firewall; Docker `localhost` ≠ container. |
| `DB_NOT_FOUND` | `InvalidCatalogNameError` | DB name wrong/not created; check spelling/case; create it. |
| `DNS_FAILURE` | host name resolution error | Hostname unresolvable; typo or missing VPN/network. |
| `SSL_REQUIRED` | server requires SSL | Add `?sslmode=require` (or correct mode) to the DSN. |
| `POOL_EXHAUSTED` | too many connections | Limit hit; close idle sessions or raise `max_connections`. |
| `UNKNOWN` | unmatched | Generic message + pointer to the `troubleshoot_connection` prompt. |

**Sanitation contract:** `explain()` output is plain strings with no host/user/password. Every tool that connects wraps failures through `explain()` so agents never see raw, leaky tracebacks.

---

## 8. MCP Surface (`server.py`)

### 8.1 Tools (8)

| # | Tool | Params | Returns | Errors |
|---|---|---|---|---|
| 1 | `list_databases` | — | `[{name, mode, yolo}]` (no DSN) | — |
| 2 | `list_tables` | `database` | `[{schema, name, kind}]` | unknown db; diagnostic on connect fail |
| 3 | `get_table_schema` | `database, table` | columns/types/PK/FK | unknown db/table; diagnostic |
| 4 | `sample_table_rows` | `database, table, n=10` | rows | unknown db/table; diagnostic |
| 5 | `execute_read_query` | `database, sql` | `{columns, rows}` | runs read-only; write attempt → native DB error, sanitized |
| 6 | `execute_write_query` | `database, sql, user_consent=false` | `{rows_affected}` or rows | `safety.authorize_write` gate (§6) |
| 7 | `set_yolo_mode` | `database, enabled` | confirmation | unknown db; persists via `config.set_yolo` |
| 8 | `check_database` | `database?` (omit = all) | `OK` or sanitized diagnostic per db | — |

Tool descriptions are part of the security model — `execute_write_query`'s description carries the explicit "read schema, show SQL, get consent" instruction.

### 8.2 Prompt (1)
- `troubleshoot_connection` — discoverable MCP prompt returning the full connection-gotchas checklist (host/port, firewall, `sslmode`, Docker localhost, db-name case, pool limits, …).

### 8.3 Transports & CLI (`cli.py`)
- `--transport stdio` (default) | `http` (SSE). `--config <path>`.
- `setup` subcommand → interactive wizard (§10).

---

## 9. Error Handling Strategy
- **Connection failures** → `diagnostics.explain()` → sanitized tool error. Never raise raw driver tracebacks to the agent.
- **Validation failures** (bad config, unknown db/table) → clear, actionable messages naming valid options.
- **Write rejections** → `WriteRejected` with the precise next step.
- **Never** include DSN/host/credentials in any error, log, or tool result.

---

## 10. Setup Wizard (`cli.py setup`)
- Ask scope (global `~/.db-conn-mcp/` vs repo `./`), then name / DSN / mode for the first DB (`yolo` defaults false).
- Validate the DSN scheme; offer to test connectivity via the doctor.
- Auto-detect & optionally inject into Claude Desktop / Cursor configs (OS-aware paths). Echo only the connection name — never the DSN.

---

## 11. Security & Sanitation (Rules 6 & 9)
- `connections.json` / `.env` are git-ignored; real DSNs never committed.
- No DSN/credential logging; diagnostics sanitized.
- Identifiers (table/column) safely quoted before catalog/sampling queries; user values parameterized. Raw SQL in `execute_*` is intentional and gated.
- `mode` is the absolute boundary; `yolo`/consent never escalate a `read` DB.

---

## 12. Testing Strategy
- **`safety.authorize_write`** — pure unit tests covering the full truth table (mode × yolo × consent).
- **`diagnostics.explain`** — unit tests mapping representative driver exceptions → expected category; assert no DSN/host substring ever appears in output.
- **`config`** — resolution order, validation (dupes, unknown scheme), atomic `set_yolo` round-trip.
- **`dialects.postgres`** — integration against a disposable Postgres (Docker) or a mocked asyncpg connection: read-only enforcement actually blocks writes; introspection shapes; identifier quoting rejects/escapes hostile table names.
- **`server`** — tool wiring: `list_databases` never leaks DSN; write gate invoked; connect failures surface as sanitized diagnostics.
- All code passes `ruff check` and `ruff format`.

---

## 13. Open Questions / Future
- **HTTP transport hardening:** v1 binds localhost, no auth. Remote/multi-user use would need a transport-level auth story (deliberately deferred).
- **Connection pooling:** open-per-op in v1; add an asyncpg pool if perf warrants.
- **Future dialects (Phase 7):** `mysql.py` (`START TRANSACTION READ ONLY`, `information_schema`) and `sqlite.py` (`?mode=ro`, `sqlite_master`/`PRAGMA`) — each one new file + one registry line.
