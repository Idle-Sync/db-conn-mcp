# Architecture: `db-conn-mcp`

This document shows how `db-conn-mcp` is structured and traces the **end-to-end flow**, from a user setting up a connection on the CLI, through an AI agent exploring and querying the database, to the write-safety gate that protects their data.

> **v1 scope:** PostgreSQL only. The code is built around a **dialect seam** so adding MySQL / SQLite later is a *one-file* job. See [Extensibility](#extensibility-adding-a-database).

---

## 1. Component Overview

```
                            ┌──────────────────────────────────────────┐
   ┌──────────────┐         │              db-conn-mcp                  │
   │  AI Agent    │  MCP    │  server.py  ── 22 tools + 2 prompts       │
   │ (Claude,     │◄───────►│      │                                   │
   │  Cursor, …)  │ stdio/  │      ├─► safety.py      ── write-gate     │
   └──────────────┘  http   │      │                                   │
                            │      ├─► diagnostics.py ── doctor /       │
   ┌──────────────┐         │      │     sanitized error → cause + fix  │
   │  Human user  │  CLI    │      ▼                                   │
   │ (terminal)   │────────►│  dialects/   ── Postgres SQL + RO guard  │
   └──────────────┘ setup   │   (registry) │        config.py ◄─ conns │
                            └──────┼───────────────────────│──────────┘
                                   ▼                        ▼
                            ┌─────────────┐         ┌────────────────┐
                            │ PostgreSQL  │         │connections.json│
                            └─────────────┘         └────────────────┘
```

### Module responsibilities (single-purpose layers)

| Module | Responsibility | Knows about Postgres? |
|---|---|---|
| `cli.py` | Parse args (`--config`, `--transport`); run server or a management subcommand (`setup`/`status`/`add`/`clients`/`remove`/`yolo`); drive the interactive wizard | No |
| `clients.py` | Known MCP clients: `ClientSpec` (config path + entry format) and the pure inject/remove/detect helpers. Shared by `cli.py` and the doctor | No |
| `config.py` | Resolve, load, validate, **and save** `connections.json` | No |
| `models.py` | Pydantic types: `Connection{name, dsn, mode, yolo}`, `Config` | No |
| `dialects/base.py` | The `Dialect` ABC — the extensibility contract | No |
| `dialects/postgres.py` | `asyncpg` impl + native read-only enforcement | **Yes (only here)** |
| `dialects/registry.py` | Map DSN scheme → `Dialect`; clear error on unknown scheme | No |
| `safety.py` | Pure write-gate decision (`mode` + dry-run-first + `yolo` + `consent`; a `dry-run` itself = mode gate only) | No |
| `diagnostics.py` | Classify driver errors → **sanitized** cause + fix; the doctor | No |
| `handlers.py` | The 22 tool handlers as plain async methods (transport-free, unit-testable) + the open-cursor registry + the dry-run grant registry (`_dry_run_grants`) | No |
| `server.py` | `FastMCP` app: registers the 22 tools + 2 prompts onto `handlers`, transport wiring | No |

The **dialect layer is the only place that knows a database is PostgreSQL.** Everything above it speaks the abstract `Dialect` contract.

---

## 2. The Tools an Agent Sees

| # | Tool | Kind | Safety |
|---|---|---|---|
| 1 | `list_databases` | Explore | safe — names + mode + yolo |
| 2 | `list_tables` | Explore | safe |
| 3 | `get_table_schema` | Explore | safe |
| 4 | `get_database_schema` | Explore | safe — whole-DB schema, deterministic; `format` json or self-contained SQL DDL |
| 5 | `dump_schema_faithful` | Export | safe (read-only) — faithful `pg_dump -s`; DSN never leaks; `pg_dump_not_found` if the binary is absent |
| 6 | `sample_table_rows` | Explore | safe (first N rows) |
| 7 | `find_columns` | Search | safe — fuzzy column-name search across tables |
| 8 | `search_value` | Search | safe (read-only) — fuzzy value search across tables; scoped/bounded |
| 9 | `execute_read_query` | Execute | runs inside a **read-only transaction**; optional `params` (driver bind) + `timeout_ms` |
| 10 | `execute_write_query` | Execute | **gated** (mode → dry-run-first → yolo → consent); defaults to `dry_run=true`, which executes-then-ROLLS-BACK (mode gate only — nothing commits) and grants the matching commit; optional `params` + `timeout_ms` |
| 11 | `explain_query` | Execute | safe — EXPLAIN (optionally ANALYZE) of a validated read-only query |
| 12 | `cancel_query` | Execute | safe — native `pg_cancel_backend(pid)`; cancels the statement, session survives |
| 13 | `open_query_cursor` | Cursor | safe (read-only) — server-side cursor for large results; pins one connection |
| 14 | `fetch_rows` | Cursor | safe — next N rows from an open cursor; auto-closes when drained |
| 15 | `close_cursor` | Cursor | safe — release the cursor + its connection; idempotent |
| 16 | `get_object_definition` | Explore | safe — faithful `pg_get_*def` definition of a view/function/trigger/sequence/index |
| 17 | `diff_schemas` | Insight | safe — structural schema diff between two configured DBs |
| 18 | `check_sequences` | Insight | safe — sequences whose next value would collide with existing rows |
| 19 | `table_stats` | Insight | safe — approximate rows + disk/index sizes per table (statistics, no scans) |
| 20 | `show_activity` | Insight | safe — **sanitized** `pg_stat_activity` (no user names/addresses; query text opt-in, truncated) |
| 21 | `set_yolo_mode` | Config | persists `yolo` flag for one named DB |
| 22 | `check_database` | Doctor | tests one DB (or all) → `OK` or sanitized cause + fix |

**Cursor lifecycle** (the one stateful corner, deliberately bounded): `open_query_cursor` validates the SQL read-only, opens a dedicated read-only connection, and registers the native cursor under a `cursor_id` in `handlers.py`. At most **5** cursors may be open; a cursor idle for **15 minutes** is reaped on the next cursor call; a fully drained cursor closes itself. Everything else in the server remains one-connection-per-call.

Plus **two MCP prompts** — `troubleshoot_connection` (a discoverable, full connection-gotchas checklist for when a DB won't connect, see [§7](#7-flow-e--self-diagnosing-connections-the-doctor)) and `faithful_schema_export` (how to choose the self-contained vs. `pg_dump` schema export, and how to offer installing `pg_dump`).

---

## 3. Flow A — Setup (human, on the CLI)

A human runs the wizard once to register a database. This is the *only* step that handles a raw DSN.

```mermaid
sequenceDiagram
    actor User
    participant CLI as setup wizard (cli.py)
    participant Cfg as config.py
    participant FS as connections.json

    User->>CLI: python -m db_conn_mcp setup
    CLI->>User: Scope? (global ~/.db-conn-mcp  |  repo ./)
    User-->>CLI: global
    CLI->>User: Name? DSN? Mode (read/write)?
    User-->>CLI: "prod", "postgresql://…", "read"
    CLI->>Cfg: add_connection(Connection)
    Cfg->>Cfg: validate (pydantic + scheme is known)
    Cfg->>FS: write connections.json  (yolo defaults to false)
    CLI->>User: ✅ registered. Inject into Claude/Cursor config? (y/n)
    Note over CLI,FS: DSN is never logged — only the name is echoed.
```

Resulting `connections.json`:
```json
{
  "connections": [
    { "name": "prod", "dsn": "postgresql://…", "mode": "read" },
    { "name": "dev",  "dsn": "postgresql://…", "mode": "write", "yolo": false }
  ]
}
```

**Config resolution order** (first match wins): `--config <path>` → `./connections.json` → `~/.db-conn-mcp/connections.json`.

---

## 4. Flow B — Agent explores, then runs a READ query

The everyday safe path. The agent discovers what exists, learns the schema, then queries.

```mermaid
sequenceDiagram
    actor Agent
    participant Server as server.py
    participant Reg as dialects/registry
    participant PG as PostgresDialect
    participant DB as PostgreSQL

    Agent->>Server: list_databases()
    Server-->>Agent: [{prod, read}, {dev, write, yolo:false}]

    Agent->>Server: list_tables(db="prod")
    Server->>Reg: dialect_for("postgresql://…")
    Reg-->>Server: PostgresDialect
    Server->>PG: connect(dsn, read_only=true)
    PG->>DB: SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY
    PG->>DB: query information_schema
    DB-->>Agent: [users, orders, …]

    Agent->>Server: get_table_schema(db="prod", table="orders")
    Server->>PG: get_schema(conn, "orders")
    PG-->>Agent: columns, types, PK/FK

    Agent->>Server: execute_read_query(db="prod", sql="SELECT …")
    Server->>PG: connect(read_only=true) → execute(sql)
    Note over PG,DB: A write here would be rejected by Postgres itself.
    DB-->>Agent: rows
```

Read-only is enforced **natively by the database** — even a `read`-mode connection physically cannot mutate data, regardless of what SQL is sent.

---

## 5. Flow C — Agent runs a WRITE query (the safety gate)

This is the heart of the safety model. The decision lives **in the server**, not in the agent's good intentions.

The decision order is **`mode` → dry-run-first → `yolo` → `user_consent`**, and `execute_write_query` defaults to `dry_run=true` so the preview is what an agent gets unless it deliberately asks to commit:

```mermaid
flowchart TD
    A[execute_write_query db, sql, dry_run=true by default] --> B{db.mode == write?}
    B -- no --> R1[❌ REJECT: db is read-only]
    B -- yes --> P{dry_run == true?}
    P -- yes --> PRE[✅ execute inside a tx, ALWAYS ROLL BACK<br/>record a grant for this exact statement]
    P -- no --> G{unexpired grant for this exact<br/>statement, or skip_dry_run == true?}
    G -- no --> R0[❌ REJECT:<br/>'Preview it first with dry_run=true.<br/>Pass skip_dry_run=true ONLY if the<br/>user explicitly asked to skip it']
    G -- yes --> C{db.yolo == true?}
    C -- yes --> RUN[✅ commit write SQL<br/>grant consumed]
    C -- no --> D{user_consent == true?}
    D -- yes --> RUN
    D -- no --> R2[❌ REJECT:<br/>'Read the table & schema, show the<br/>exact SQL to the user, get a yes,<br/>then call again with user_consent=true']
```

The dry-run grant is process-local state in `Handlers` (`_dry_run_grants`, TTL 600s, consumed on commit) — precedent: `_active_ports`.

The intended agent choreography for a non-yolo write:

```mermaid
sequenceDiagram
    actor User
    actor Agent
    participant Server as server.py
    participant PG as PostgresDialect
    participant DB as PostgreSQL

    Note over Agent: 1. Understand before changing
    Agent->>Server: get_table_schema(db="dev", table="users")
    Server->>PG: get_schema → columns/types
    PG-->>Agent: schema
    Agent->>Server: sample_table_rows(db="dev", table="users")
    PG-->>Agent: example rows (learn the data shape)

    Note over Agent,DB: 2. Dry-run (the default) — executes, ALWAYS rolls back
    Agent->>Server: execute_write_query(db="dev", sql="UPDATE users SET …")
    Server->>PG: connect(read_only=false) → execute in tx → ROLLBACK
    PG-->>Agent: rows_affected it WOULD have had (grant recorded)

    Agent->>User: "I will run:  UPDATE users SET … (affects 3 rows). Proceed?"
    User-->>Agent: "yes"

    Note over Agent,DB: 3. Re-call to commit — grant + consent
    Agent->>Server: execute_write_query(db="dev", sql="UPDATE users SET …", dry_run=false, user_consent=true)
    Server->>PG: connect(read_only=false) → execute(sql)
    DB-->>Agent: rows affected (grant consumed)
```

Skipping step 2 is not a matter of etiquette: a commit with no matching unexpired grant is **rejected by the server** unless `skip_dry_run=true` attests the user explicitly asked to skip the preview.

`mode` is the **hard boundary** (native, unbypassable). Everything after it — the dry-run stage, `yolo`, `user_consent` — only decides *how much ceremony* a write needs on a DB that is *already* `write`; none of them, nor `skip_dry_run`, can ever make a `read` DB writable. And the two relaxations are scoped: `yolo` waives only the consent prompt (never the preview), `skip_dry_run` waives only the preview (never consent).

**Dry-run writes** (`dry_run=true`, the default): the statement executes inside a transaction that is **always rolled back**, returning the `rows_affected` it *would* have had, and records a grant keyed on the exact `(database, sql, params)`. Because nothing commits, only the `mode` gate applies — no yolo or consent needed — which is exactly what lets the agent show the user the real impact *before* asking for consent to the real write. The grant expires after 10 minutes and is **consumed** by the commit it authorizes, so running the same statement twice means previewing it twice. Caveat (documented on the tool): a dry-run still executes server-side until the rollback, so it briefly takes locks, advances sequences, and fires triggers.

---

## 6. Flow D — Enabling YOLO mode (persisted)

When the user is tired of confirming every write on a trusted DB, they ask the agent to enable yolo. The server writes it back to disk so it survives restarts.

```mermaid
sequenceDiagram
    actor User
    actor Agent
    participant Server as server.py
    participant Cfg as config.py
    participant FS as connections.json

    User->>Agent: "Enable yolo mode for dev — stop asking me."
    Agent->>Server: set_yolo_mode(database="dev", enabled=true)
    Server->>Cfg: set_yolo("dev", true)
    Cfg->>FS: rewrite connections.json (dev.yolo = true)
    Server-->>Agent: ✅ yolo enabled for "dev" (persisted)

    Note over Agent,FS: Future writes to "dev" skip the consent gate —<br/>across this and all future sessions.
```

`set_yolo_mode("dev", false)` reverts it. YOLO is **per-database**: enabling it on `dev` never affects `prod`.

---

## 7. Flow E — Self-diagnosing connections (the doctor)

Connections are validated **lazily**: the server boots even if a database is down, and *any* tool that hits an unreachable DB gets a sanitized, actionable diagnostic instead of a raw stack trace. The agent (or user) can also probe proactively with `check_database`.

```mermaid
sequenceDiagram
    actor Agent
    participant Server as server.py
    participant Diag as diagnostics.py
    participant PG as PostgresDialect
    participant DB as PostgreSQL

    Agent->>Server: check_database(name="prod")
    Server->>PG: connect(dsn, read_only=true)
    PG->>DB: TCP connect / auth
    DB--xPG: error (refused / auth / no-db / SSL / timeout)
    PG-->>Server: raises driver exception
    Server->>Diag: explain(error)  ⟵ DSN/password NEVER passed in
    Diag-->>Server: {category, cause, fixes[]}
    Server-->>Agent: "❌ prod unreachable — AUTH FAILED.<br/>Likely: wrong user/password or role missing.<br/>Fix: verify credentials; confirm the role exists.<br/>(full checklist: prompt 'troubleshoot_connection')"
    Note over Agent: For the complete gotchas list:
    Agent->>Server: getPrompt("troubleshoot_connection")
    Server-->>Agent: full host/port/firewall/sslmode/<br/>container-localhost/db-name/pool checklist
```

**Error classification** (`diagnostics.py` maps driver exception → sanitized advice — credentials never appear in the output):

| Category | Triggered by (examples) | Sanitized cause + fix shown to agent |
|---|---|---|
| `AUTH_FAILED` | invalid password / role does not exist | "Wrong user/password, or the role doesn't exist. Verify credentials and that the role exists." |
| `HOST_UNREACHABLE` | connection refused / timeout | "DB not accepting connections. Is it running? Check host/port, firewall. In Docker, `localhost` ≠ the DB container." |
| `DB_NOT_FOUND` | database "x" does not exist | "Database name is wrong or not created yet. Check spelling/case; create it if needed." |
| `DNS_FAILURE` | could not translate host name | "Hostname can't be resolved — likely a typo in the host or a missing VPN/network." |
| `SSL_REQUIRED` | server requires SSL | "Server demands SSL. Add `?sslmode=require` (or appropriate mode) to the DSN." |
| `POOL_EXHAUSTED` | too many connections | "Connection limit hit. Close idle sessions or raise the server's `max_connections`." |
| `UNKNOWN` | anything unmatched | "Unrecognized connection error. See the `troubleshoot_connection` prompt for the full checklist." |

The **`troubleshoot_connection` MCP prompt** is the standalone, full gotchas checklist — discoverable by the agent at any time, not just on error. Targeted fixes appear inline per error; the prompt is the complete reference.

> **Security:** `diagnostics.explain()` receives only the *driver exception type/message-class* — never the `Connection` object or DSN — so no host, user, or password can leak into a tool response or log.

### Fallback-port probing (optional, `fallback_ports`)

Probing lives in `handlers._connect` — **above** the dialect seam, so no dialect knows about it and `Dialect.connect()` keeps its single job of opening one DSN. `_connect` builds the candidate list (`_dsn_candidates`: primary DSN first, then one rewritten DSN per configured `fallback_ports` entry) and walks it, gated purely on the diagnostic category above: a failure classified `HOST_UNREACHABLE` moves to the next candidate, **anything else** (`AUTH_FAILED`, `SSL_REQUIRED`, `DB_NOT_FOUND`, `DNS_FAILURE`, …) is raised immediately so a real misconfiguration is never masked by a port hunt. Each fallback attempt is wrapped in `asyncio.wait_for(FALLBACK_CONNECT_TIMEOUT_SECONDS)` (5s) so a black-holed port can't stall the chain; the primary attempt keeps the driver's own timeout behavior. The port that answers is cached in `Handlers._active_ports` (process memory only — never written back to `connections.json`) so it's tried first next time, re-probed from the primary if it later fails, and forgotten when the primary succeeds; `list_databases` / `check_database` surface it as `active_port`. If every candidate fails, the last sanitized diagnostic is returned with the tried fallback **port numbers** appended — ports only, still no host, user, or DSN (Rule 6). A connection without the key produces a single-candidate list, i.e. exactly the pre-existing behavior (one alignment: on Python 3.10 a driver-raised connect timeout now classifies as `HOST_UNREACHABLE`, matching 3.11+, instead of `UNKNOWN`).

---

## 8. Extensibility — Adding a Database

Adding MySQL or SQLite later touches **exactly one new file** plus a one-line registration. Nothing above the dialect layer changes.

```mermaid
flowchart LR
    subgraph Today [v1 — today]
        REG[registry] --> PGD[postgres.py]
    end
    subgraph Later [adding MySQL]
        REG2[registry<br/>+1 line] --> PGD2[postgres.py]
        REG2 -.new file.-> MYD[mysql.py]
    end
    Today ==> Later
```

The contract a new dialect must satisfy:

```python
class Dialect(ABC):
    scheme: str  # e.g. "mysql"

    async def connect(self, dsn, *, read_only): ...  # native read-only enforcement lives here
    async def list_tables(self, conn): ...
    async def get_schema(self, conn, table): ...
    async def sample_rows(self, conn, table, n=10): ...
    async def execute(self, conn, sql, params=None, timeout_ms=None): ...  # driver bind params
    async def execute_dry_run(self, conn, sql, params=None, timeout_ms=None): ...  # tx + ROLLBACK
    async def open_cursor(self, conn, sql, params=None): ...  # native server-side cursor
    async def get_object_definition(self, conn, object_type, name): ...
    async def cancel_backend(self, conn, pid): ...
    async def explain(self, conn, sql, analyze=False): ...
    async def check_sequences(self, conn): ...
    async def table_stats(self, conn): ...
    async def show_activity(self, conn, include_query=False): ...
```

(plus the schema-export and search methods — see `dialects/base.py` for the full, documented contract.)

| DB | Read-only mechanism (inside `connect`) | Introspection source |
|---|---|---|
| PostgreSQL (v1) | `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` | `information_schema` |
| MySQL (future) | `START TRANSACTION READ ONLY` | `information_schema` |
| SQLite (future) | open file with `?mode=ro` | `sqlite_master` / `PRAGMA` |

Because each dialect owns its own read-only enforcement and catalog queries, those per-database differences **never leak** into `safety.py` or `server.py`.

---

## 9. End-to-End at a Glance

```
Human ──setup──► connections.json ──read──► config.py
                                              │
AI Agent ──MCP (stdio/http)──► server.py ─────┼─► safety.py     (write gate)
                                  │            ├─► diagnostics.py (doctor, sanitized)
                                  ▼            ▼
                              dialects/registry ──► PostgresDialect ──► PostgreSQL
                                (scheme → impl)      (RO guard + SQL)
```

1. A human registers a DB once via the CLI wizard → `connections.json`.
2. An agent connects over MCP and **explores** (`list_*`, `get_table_schema`, `sample_table_rows`) — always read-only.
3. **Reads** run inside a native read-only transaction.
4. **Writes** pass through the server-side gate: `mode` (hard) → dry-run-first (a commit needs a prior preview of the identical statement, unless the user asked to skip) → `yolo` (persisted trust) → `user_consent` (per-op ask).
5. `set_yolo_mode` lets a trusted DB skip the per-op ask, persisted to disk.
6. Any unreachable DB yields a **sanitized diagnostic** (cause + fix), never a raw error or leaked DSN; `check_database` probes proactively and `troubleshoot_connection` is the full gotchas checklist.
