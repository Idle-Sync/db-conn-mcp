# db-conn-mcp v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PostgreSQL-only MCP server that lets AI agents explore and query a database safely, behind a dialect seam that makes adding MySQL/SQLite a one-file change.

**Architecture:** Five single-purpose layers — `config` (JSON I/O), `dialects` (DB-specific SQL behind a `Dialect` ABC), `safety` (pure write-gate), `diagnostics` (sanitized error mapping), `server` (MCP tools/prompt + transports). Pure modules are unit-tested in isolation; the Postgres dialect is integration-tested against a disposable database.

**Tech Stack:** Python 3.10+, `mcp` SDK, `asyncpg`, `pydantic`, `pytest` + `pytest-asyncio`, `ruff`.

**Source of truth:** [`docs/superpowers/specs/2026-06-03-db-conn-mcp-design.md`](../specs/2026-06-03-db-conn-mcp-design.md). Keep PRD/PLAN/ARCHITECTURE in sync with any deviation (Rule 7). No agent watermarks in commits (Rule 8). Work inside `.venv`; deps live in `pyproject.toml` (Rule 9).

---

## Task 1: Project scaffolding & tooling

**Files:**
- Create: `pyproject.toml`
- Create: `src/db_conn_mcp/__init__.py`
- Create: `tests/__init__.py`
- Create: `connections.example.json`

- [ ] **Step 1: Create the virtual environment**

Run:
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |    POSIX: source .venv/bin/activate
python -m pip install --upgrade pip
```

- [ ] **Step 2: Write `pyproject.toml`** (single source of dependencies)

```toml
[project]
name = "db-conn-mcp"
version = "0.1.0"
description = "A dead-simple, self-hostable MCP server for safely querying databases."
requires-python = ">=3.10"
dependencies = [
    "mcp>=1.0.0",
    "asyncpg>=0.29",
    "pydantic>=2.5",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.4"]

[project.scripts]
db-conn-mcp = "db_conn_mcp.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Create empty package + test markers**

`src/db_conn_mcp/__init__.py`:
```python
"""db-conn-mcp: a dead-simple MCP server for safely querying databases."""

__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

- [ ] **Step 4: Create `connections.example.json`** (tracked template; real `connections.json` is git-ignored)

```json
{
  "connections": [
    { "name": "local", "dsn": "postgresql://user:pass@localhost:5432/mydb", "mode": "read" },
    { "name": "dev",   "dsn": "postgresql://user:pass@localhost:5432/devdb", "mode": "write", "yolo": false }
  ]
}
```

- [ ] **Step 5: Install in editable mode with dev deps**

Run:
```bash
pip install -e ".[dev]"
```
Expected: installs `mcp`, `asyncpg`, `pydantic`, `pytest`, `pytest-asyncio`, `ruff` into `.venv`.

- [ ] **Step 6: Verify tooling**

Run:
```bash
ruff check . && pytest -q
```
Expected: ruff passes; pytest reports "no tests ran" (exit 5 is fine at this stage).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/ tests/ connections.example.json
git commit -m "Scaffold package, tooling, and example config"
```

---

## Task 2: Data models (`models.py`)

**Files:**
- Create: `src/db_conn_mcp/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing tests**

`tests/test_models.py`:
```python
import pytest
from pydantic import ValidationError
from db_conn_mcp.models import Connection, Config


def test_connection_defaults_yolo_false():
    c = Connection(name="db", dsn="postgresql://x", mode="read")
    assert c.yolo is False


def test_connection_rejects_bad_mode():
    with pytest.raises(ValidationError):
        Connection(name="db", dsn="postgresql://x", mode="readonly")


def test_public_view_excludes_dsn():
    c = Connection(name="db", dsn="postgresql://secret", mode="write", yolo=True)
    pub = c.public()
    assert pub == {"name": "db", "mode": "write", "yolo": True}
    assert "dsn" not in pub


def test_config_rejects_duplicate_names():
    with pytest.raises(ValidationError):
        Config(connections=[
            Connection(name="dup", dsn="postgresql://a", mode="read"),
            Connection(name="dup", dsn="postgresql://b", mode="read"),
        ])


def test_get_returns_connection():
    cfg = Config(connections=[Connection(name="db", dsn="postgresql://x", mode="read")])
    assert cfg.get("db").name == "db"


def test_get_unknown_lists_available():
    cfg = Config(connections=[Connection(name="alpha", dsn="postgresql://x", mode="read")])
    with pytest.raises(KeyError) as exc:
        cfg.get("missing")
    assert "alpha" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: db_conn_mcp.models`.

- [ ] **Step 3: Implement `models.py`**

```python
"""Pydantic models for connections.json (the config source of truth)."""

from typing import List, Literal

from pydantic import BaseModel, Field, model_validator

Mode = Literal["read", "write"]


class Connection(BaseModel):
    """A single database entry. `dsn` is secret and never logged or returned."""

    name: str
    dsn: str
    mode: Mode
    yolo: bool = False

    def public(self) -> dict:
        """A safe, DSN-free view for tools like `list_databases`."""
        return {"name": self.name, "mode": self.mode, "yolo": self.yolo}


class Config(BaseModel):
    """The whole config: a list of connections under a top-level object."""

    connections: List[Connection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "Config":
        names = [c.name for c in self.connections]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"Duplicate connection names: {dupes}")
        return self

    def get(self, name: str) -> Connection:
        """Look up a connection by name, or raise KeyError listing valid names."""
        for c in self.connections:
            if c.name == name:
                return c
        available = ", ".join(c.name for c in self.connections) or "(none)"
        raise KeyError(f"Unknown database '{name}'. Available: {available}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/db_conn_mcp/models.py tests/test_models.py
git commit -m "Add Connection/Config models with uniqueness and public view"
```

---

## Task 3: Config resolution & persistence (`config.py`)

**Files:**
- Create: `src/db_conn_mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

`tests/test_config.py`:
```python
import json
from pathlib import Path

import pytest
from db_conn_mcp import config as cfgmod
from db_conn_mcp.models import Config, Connection


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_explicit_path_wins(tmp_path):
    p = tmp_path / "custom.json"
    _write(p, {"connections": []})
    assert cfgmod.resolve_path(str(p)) == p


def test_repo_scoped_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "connections.json"
    _write(repo, {"connections": []})
    assert cfgmod.resolve_path(None) == repo


def test_load_missing_file_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        cfgmod.load(str(tmp_path / "nope.json"))
    assert "setup" in str(exc.value)


def test_load_parses_and_validates(tmp_path):
    p = tmp_path / "connections.json"
    _write(p, {"connections": [
        {"name": "db", "dsn": "postgresql://x", "mode": "read"}
    ]})
    config, path = cfgmod.load(str(p))
    assert path == p
    assert config.get("db").mode == "read"


def test_set_yolo_persists_atomically(tmp_path):
    p = tmp_path / "connections.json"
    _write(p, {"connections": [
        {"name": "dev", "dsn": "postgresql://x", "mode": "write"}
    ]})
    config, path = cfgmod.load(str(p))
    cfgmod.set_yolo(config, path, "dev", True)

    reloaded, _ = cfgmod.load(str(p))
    assert reloaded.get("dev").yolo is True


def test_set_yolo_unknown_db_raises(tmp_path):
    p = tmp_path / "connections.json"
    _write(p, {"connections": []})
    config, path = cfgmod.load(str(p))
    with pytest.raises(KeyError):
        cfgmod.set_yolo(config, path, "ghost", True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError`/`ModuleNotFoundError` for `config`.

- [ ] **Step 3: Implement `config.py`**

```python
"""Resolve, load, validate, and persist connections.json."""

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from .models import Config

GLOBAL_PATH = Path.home() / ".db-conn-mcp" / "connections.json"


def resolve_path(explicit: Optional[str] = None) -> Path:
    """3-tier resolution: --config > ./connections.json > ~/.db-conn-mcp/."""
    if explicit:
        return Path(explicit)
    repo = Path.cwd() / "connections.json"
    if repo.exists():
        return repo
    return GLOBAL_PATH


def load(explicit: Optional[str] = None) -> Tuple[Config, Path]:
    """Load and validate the config; return (config, resolved_path)."""
    path = resolve_path(explicit)
    if not path.exists():
        raise FileNotFoundError(
            f"No connections.json found at {path}. "
            "Create one or run `db-conn-mcp setup`."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config.model_validate(data), path


def save(config: Config, path: Path) -> None:
    """Atomically rewrite connections.json (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def set_yolo(config: Config, path: Path, name: str, enabled: bool) -> Config:
    """Set yolo for one DB and persist. Raises KeyError if name is unknown."""
    config.get(name).yolo = enabled  # raises KeyError if missing
    save(config, path)
    return config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/db_conn_mcp/config.py tests/test_config.py
git commit -m "Add config resolution, validation, and atomic yolo persistence"
```

---

## Task 4: Write-safety gate (`safety.py`)

**Files:**
- Create: `src/db_conn_mcp/safety.py`
- Test: `tests/test_safety.py`

- [ ] **Step 1: Write failing tests** (full truth table: mode × yolo × consent)

`tests/test_safety.py`:
```python
import pytest
from db_conn_mcp.models import Connection
from db_conn_mcp.safety import WriteRejected, authorize_write


def _conn(mode, yolo=False):
    return Connection(name="db", dsn="postgresql://x", mode=mode, yolo=yolo)


def test_read_mode_always_rejected_even_with_consent():
    with pytest.raises(WriteRejected) as exc:
        authorize_write(_conn("read"), user_consent=True)
    assert "read-only" in str(exc.value)


def test_write_with_yolo_allowed_without_consent():
    authorize_write(_conn("write", yolo=True), user_consent=False)  # no raise


def test_write_with_consent_allowed():
    authorize_write(_conn("write"), user_consent=True)  # no raise


def test_write_without_yolo_or_consent_rejected():
    with pytest.raises(WriteRejected) as exc:
        authorize_write(_conn("write"), user_consent=False)
    assert "user_consent=true" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_safety.py -v`
Expected: FAIL — `ModuleNotFoundError: db_conn_mcp.safety`.

- [ ] **Step 3: Implement `safety.py`**

```python
"""The write-authorization gate: a pure function, no I/O.

Decision order: mode (hard) -> yolo (per-db trust) -> user_consent (per-op ask).
`yolo`/`user_consent` only relax the prompt on an already-`write` DB; they can
NEVER make a `read` DB writable.
"""

from .models import Connection


class WriteRejected(Exception):
    """Raised when a write must not proceed; message tells the agent what to do."""


def authorize_write(conn: Connection, user_consent: bool) -> None:
    """Allow (return None) or reject (raise WriteRejected) a write."""
    if conn.mode != "write":
        raise WriteRejected(
            f"Database '{conn.name}' is read-only (mode=read); writes are not allowed."
        )
    if conn.yolo:
        return
    if user_consent:
        return
    raise WriteRejected(
        "Write requires explicit user consent. First read the table and its schema, "
        "then show the exact SQL to the user. Only re-call with user_consent=true if "
        "they say yes."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_safety.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/db_conn_mcp/safety.py tests/test_safety.py
git commit -m "Add pure write-safety gate (mode/yolo/consent)"
```

---

## Task 5: Sanitized diagnostics (`diagnostics.py`)

**Files:**
- Create: `src/db_conn_mcp/diagnostics.py`
- Test: `tests/test_diagnostics.py`

- [ ] **Step 1: Write failing tests** (classification + the sanitation guarantee)

`tests/test_diagnostics.py`:
```python
from db_conn_mcp.diagnostics import explain


def _err(type_name: str, message: str) -> Exception:
    """Build an exception whose class name and message we control."""
    return type(type_name, (Exception,), {})(message)


def test_auth_failure_classified():
    out = explain(_err("InvalidPasswordError", "password authentication failed for user"))
    assert out["category"] == "AUTH_FAILED"


def test_host_unreachable_classified():
    out = explain(ConnectionRefusedError("connection refused"))
    assert out["category"] == "HOST_UNREACHABLE"


def test_db_not_found_classified():
    out = explain(_err("InvalidCatalogNameError", 'database "nope" does not exist'))
    assert out["category"] == "DB_NOT_FOUND"


def test_ssl_required_classified():
    out = explain(_err("Error", "server does not support SSL, but SSL was required"))
    assert out["category"] == "SSL_REQUIRED"


def test_unknown_falls_back():
    out = explain(_err("WeirdError", "something inexplicable"))
    assert out["category"] == "UNKNOWN"
    assert "troubleshoot_connection" in " ".join(out["fixes"])


def test_output_never_leaks_credentials():
    leaky = _err(
        "InvalidPasswordError",
        "password authentication failed for user 'admin' at secret-host.internal:5432",
    )
    out = explain(leaky)
    blob = (out["category"] + out["cause"] + " ".join(out["fixes"])).lower()
    for secret in ("admin", "secret-host", "5432"):
        assert secret not in blob
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_diagnostics.py -v`
Expected: FAIL — `ModuleNotFoundError: db_conn_mcp.diagnostics`.

- [ ] **Step 3: Implement `diagnostics.py`**

```python
"""Map raw driver exceptions to sanitized, actionable diagnostics.

CRITICAL: explain() receives ONLY the exception and returns ONLY our static
advice strings — never the raw message — so no DSN/host/user/password leaks.
"""

from typing import Dict

_ADVICE: Dict[str, dict] = {
    "AUTH_FAILED": {
        "cause": "Authentication failed — wrong user/password, or the role does not exist.",
        "fixes": ["Verify the username and password.", "Confirm the role exists on the server."],
    },
    "HOST_UNREACHABLE": {
        "cause": "The database is not accepting connections.",
        "fixes": [
            "Confirm the server is running and the host/port are correct.",
            "Check firewall rules.",
            "In Docker, 'localhost' refers to the container, not the DB host.",
        ],
    },
    "DB_NOT_FOUND": {
        "cause": "The named database does not exist.",
        "fixes": ["Check the database name spelling and case.", "Create the database if needed."],
    },
    "DNS_FAILURE": {
        "cause": "The hostname could not be resolved.",
        "fixes": ["Check the host for typos.", "Confirm any required VPN/network is connected."],
    },
    "SSL_REQUIRED": {
        "cause": "The server requires an SSL connection.",
        "fixes": ["Add '?sslmode=require' (or the correct mode) to the DSN."],
    },
    "POOL_EXHAUSTED": {
        "cause": "The server's connection limit has been reached.",
        "fixes": ["Close idle sessions.", "Increase the server's max_connections."],
    },
    "UNKNOWN": {
        "cause": "Unrecognized connection error.",
        "fixes": ["See the 'troubleshoot_connection' prompt for the full checklist."],
    },
}


def _classify(error: Exception) -> str:
    name = type(error).__name__
    msg = str(error).lower()

    if name == "InvalidPasswordError" or "password authentication failed" in msg or (
        "role" in msg and "does not exist" in msg
    ):
        return "AUTH_FAILED"
    if name == "InvalidCatalogNameError" or ("database" in msg and "does not exist" in msg):
        return "DB_NOT_FOUND"
    if "ssl" in msg:
        return "SSL_REQUIRED"
    if isinstance(error, (ConnectionRefusedError, TimeoutError)) or "refused" in msg or (
        "timeout" in msg or "timed out" in msg
    ):
        return "HOST_UNREACHABLE"
    if "could not translate host" in msg or "name or service not known" in msg or (
        "nodename nor servname" in msg
    ):
        return "DNS_FAILURE"
    if "too many" in msg and "connection" in msg:
        return "POOL_EXHAUSTED"
    return "UNKNOWN"


def explain(error: Exception) -> dict:
    """Return {category, cause, fixes[]} — sanitized, no credentials."""
    category = _classify(error)
    advice = _ADVICE[category]
    return {"category": category, "cause": advice["cause"], "fixes": list(advice["fixes"])}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diagnostics.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/db_conn_mcp/diagnostics.py tests/test_diagnostics.py
git commit -m "Add sanitized connection diagnostics"
```

---

## Task 6: Dialect ABC + registry (`dialects/base.py`, `dialects/registry.py`)

**Files:**
- Create: `src/db_conn_mcp/dialects/__init__.py`
- Create: `src/db_conn_mcp/dialects/base.py`
- Create: `src/db_conn_mcp/dialects/registry.py`
- Test: `tests/test_registry.py`

> Note: `registry.py` imports `postgres.py` (Task 7). Implement Task 7's `PostgresDialect` class shell first if needed, or temporarily register a stub. The order below assumes Task 7 lands with this task; if doing strictly sequentially, create `postgres.py` from Task 7 Step 3 before running these tests.

- [ ] **Step 1: Write failing tests** (registry resolution — no DB needed)

`tests/test_registry.py`:
```python
import pytest
from db_conn_mcp.dialects.registry import dialect_for


def test_resolves_postgresql_scheme():
    d = dialect_for("postgresql://user:pass@localhost/db")
    assert d.scheme == "postgresql"


def test_resolves_postgres_alias():
    d = dialect_for("postgres://user:pass@localhost/db")
    assert d.scheme == "postgresql"


def test_unknown_scheme_lists_supported():
    with pytest.raises(ValueError) as exc:
        dialect_for("mysql://user:pass@localhost/db")
    assert "postgresql" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the ABC** (`dialects/base.py`)

```python
"""The Dialect contract — the ONLY place database-specific behavior lives."""

from abc import ABC, abstractmethod
from typing import Any


class Dialect(ABC):
    """One database family (e.g. PostgreSQL). Add a DB = implement this once."""

    scheme: str  # e.g. "postgresql"

    @abstractmethod
    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        """Open a connection. When read_only=True, enforce it natively before
        returning (Postgres: SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY)."""

    @abstractmethod
    async def list_tables(self, conn: Any) -> list[dict]:
        """Return tables + views: [{schema, name, kind}]."""

    @abstractmethod
    async def get_schema(self, conn: Any, table: str) -> dict:
        """Return columns, types, nullability, and PK/FK for one table."""

    @abstractmethod
    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        """Return the first n rows. The identifier MUST be safely quoted."""

    @abstractmethod
    async def execute(self, conn: Any, sql: str) -> dict:
        """Run raw SQL; return {'columns', 'rows'} or {'rows_affected'}."""
```

- [ ] **Step 4: Create `dialects/__init__.py`** (empty file).

- [ ] **Step 5: Implement the registry** (`dialects/registry.py`)

```python
"""Map a DSN scheme to its Dialect. Adding a DB = one register() call."""

from urllib.parse import urlparse
from typing import Dict

from .base import Dialect
from .postgres import PostgresDialect

_DIALECTS: Dict[str, Dialect] = {}


def register(dialect: Dialect) -> None:
    _DIALECTS[dialect.scheme] = dialect


def dialect_for(dsn: str) -> Dialect:
    """Return the Dialect for a DSN, or raise ValueError listing supported schemes."""
    scheme = urlparse(dsn).scheme.split("+")[0]
    if scheme == "postgres":
        scheme = "postgresql"
    try:
        return _DIALECTS[scheme]
    except KeyError:
        supported = ", ".join(sorted(_DIALECTS)) or "(none)"
        raise ValueError(
            f"Unsupported DSN scheme '{scheme}'. Supported: {supported}"
        ) from None


register(PostgresDialect())
```

- [ ] **Step 6: Run tests to verify they pass** (requires Task 7's `postgres.py`)

Run: `pytest tests/test_registry.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add src/db_conn_mcp/dialects/__init__.py src/db_conn_mcp/dialects/base.py \
        src/db_conn_mcp/dialects/registry.py tests/test_registry.py
git commit -m "Add Dialect ABC and scheme registry"
```

---

## Task 7: PostgreSQL dialect (`dialects/postgres.py`)

**Files:**
- Create: `src/db_conn_mcp/dialects/postgres.py`
- Test: `tests/test_postgres_integration.py`

> Integration tests need a real Postgres. They **skip** automatically unless `TEST_DATABASE_URL` is set (a `write`-capable DSN to a disposable DB). Quick start:
> ```bash
> docker run --rm -d -e POSTGRES_PASSWORD=pw -p 5433:5432 --name dcm-pg postgres:16
> export TEST_DATABASE_URL="postgresql://postgres:pw@localhost:5433/postgres"
> ```

- [ ] **Step 1: Write failing/integration tests**

`tests/test_postgres_integration.py`:
```python
import os

import pytest
from db_conn_mcp.dialects.postgres import PostgresDialect

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run")


@pytest.fixture
async def seeded():
    d = PostgresDialect()
    conn = await d.connect(DSN, read_only=False)
    await conn.execute("DROP TABLE IF EXISTS dcm_people")
    await conn.execute(
        "CREATE TABLE dcm_people (id serial PRIMARY KEY, name text NOT NULL)"
    )
    await conn.execute("INSERT INTO dcm_people (name) VALUES ('ada'), ('alan')")
    yield d, conn
    await conn.execute("DROP TABLE IF EXISTS dcm_people")
    await conn.close()


async def test_list_tables_includes_seeded(seeded):
    d, conn = seeded
    names = {t["name"] for t in await d.list_tables(conn)}
    assert "dcm_people" in names


async def test_get_schema_reports_columns_and_pk(seeded):
    d, conn = seeded
    schema = await d.get_schema(conn, "dcm_people")
    cols = {c["name"]: c for c in schema["columns"]}
    assert cols["id"]["is_primary_key"] is True
    assert cols["name"]["nullable"] is False


async def test_sample_rows_returns_data(seeded):
    d, conn = seeded
    rows = await d.sample_rows(conn, "dcm_people", n=10)
    assert {r["name"] for r in rows} == {"ada", "alan"}


async def test_sample_rows_quotes_identifier_safely(seeded):
    d, conn = seeded
    # A hostile name must not execute injected SQL; it should error cleanly.
    with pytest.raises(Exception):
        await d.sample_rows(conn, "dcm_people; DROP TABLE dcm_people; --")
    # Table still exists:
    assert {t["name"] for t in await d.list_tables(conn)} >= {"dcm_people"}


async def test_read_only_connection_blocks_writes():
    d = PostgresDialect()
    conn = await d.connect(DSN, read_only=True)
    try:
        with pytest.raises(Exception) as exc:
            await d.execute(conn, "CREATE TABLE dcm_should_not_exist (x int)")
        assert "read-only" in str(exc.value).lower()
    finally:
        await conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_postgres_integration.py -v`
Expected: FAIL (module missing) — or SKIP if `TEST_DATABASE_URL` unset. Set it to actually drive the work.

- [ ] **Step 3: Implement `postgres.py`**

```python
"""PostgreSQL dialect using asyncpg. The only DB-specific module in v1."""

from typing import Any

import asyncpg

from .base import Dialect

_TABLES_SQL = """
SELECT table_schema AS schema, table_name AS name,
       CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name
"""

_COLUMNS_SQL = """
SELECT column_name AS name, data_type AS type,
       (is_nullable = 'YES') AS nullable
FROM information_schema.columns
WHERE table_name = $1
ORDER BY ordinal_position
"""

_PK_SQL = """
SELECT kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
WHERE tc.table_name = $1 AND tc.constraint_type = 'PRIMARY KEY'
"""


class PostgresDialect(Dialect):
    """Talks to PostgreSQL; enforces read-only natively at the session level."""

    scheme = "postgresql"

    async def connect(self, dsn: str, *, read_only: bool) -> Any:
        conn = await asyncpg.connect(dsn)
        if read_only:
            # Session-level: every subsequent (autocommit) statement is read-only.
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        return conn

    async def list_tables(self, conn: Any) -> list[dict]:
        rows = await conn.fetch(_TABLES_SQL)
        return [dict(r) for r in rows]

    async def get_schema(self, conn: Any, table: str) -> dict:
        cols = await conn.fetch(_COLUMNS_SQL, table)
        pks = {r["column_name"] for r in await conn.fetch(_PK_SQL, table)}
        if not cols:
            raise ValueError(f"Table '{table}' not found.")
        return {
            "table": table,
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "nullable": c["nullable"],
                    "is_primary_key": c["name"] in pks,
                }
                for c in cols
            ],
        }

    async def sample_rows(self, conn: Any, table: str, n: int = 10) -> list[dict]:
        # Quote the identifier via Postgres itself — never f-string a raw name.
        quoted = await conn.fetchval("SELECT quote_ident($1)", table)
        rows = await conn.fetch(f"SELECT * FROM {quoted} LIMIT $1", n)
        return [dict(r) for r in rows]

    async def execute(self, conn: Any, sql: str) -> dict:
        stripped = sql.lstrip().lower()
        if stripped.startswith("select") or stripped.startswith("with"):
            rows = await conn.fetch(sql)
            columns = list(rows[0].keys()) if rows else []
            return {"columns": columns, "rows": [dict(r) for r in rows]}
        status = await conn.execute(sql)  # e.g. "UPDATE 3"
        return {"status": status}
```

> Note on `sample_rows` hostile-name test: `quote_ident` quotes the whole string as a single identifier, so `dcm_people; DROP ...` becomes a quoted non-existent table name → clean "relation does not exist" error, no injection. v1 accepts a bare table name; schema-qualified names are a later enhancement.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_postgres_integration.py -v` (with `TEST_DATABASE_URL` set)
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/db_conn_mcp/dialects/postgres.py tests/test_postgres_integration.py
git commit -m "Add PostgreSQL dialect with native read-only enforcement"
```

---

## Task 8: MCP server — tools, prompt, gate wiring (`server.py`)

**Files:**
- Create: `src/db_conn_mcp/server.py`
- Test: `tests/test_server.py`

> Design for testability: `server.py` exposes plain async handler functions that take a loaded `Config` + resolved `path`, so they can be unit-tested without a live MCP transport. The MCP `Server` registration wraps these handlers. Connection objects come from a small helper that routes failures through `diagnostics.explain`.

- [ ] **Step 1: Write failing tests** (handlers, with a fake dialect — no real DB)

`tests/test_server.py`:
```python
import pytest
from db_conn_mcp import server
from db_conn_mcp.models import Config, Connection


@pytest.fixture
def cfg(tmp_path):
    config = Config(connections=[
        Connection(name="ro", dsn="postgresql://x", mode="read"),
        Connection(name="rw", dsn="postgresql://x", mode="write"),
    ])
    return config, tmp_path / "connections.json"


def test_list_databases_hides_dsn(cfg):
    config, path = cfg
    out = server.list_databases(config)
    assert {d["name"] for d in out} == {"ro", "rw"}
    assert all("dsn" not in d for d in out)


async def test_write_rejected_without_consent(cfg, monkeypatch):
    config, path = cfg
    with pytest.raises(server.WriteRejected):
        await server.execute_write_query(config, "rw", "UPDATE t SET x=1", user_consent=False)


async def test_write_to_read_db_rejected(cfg):
    config, path = cfg
    with pytest.raises(server.WriteRejected):
        await server.execute_write_query(config, "ro", "UPDATE t SET x=1", user_consent=True)


async def test_connect_failure_returns_sanitized_diagnostic(cfg, monkeypatch):
    config, path = cfg

    async def boom(*a, **k):
        raise type("InvalidPasswordError", (Exception,), {})(
            "password authentication failed for admin@secret-host"
        )

    # Force the connect helper to fail:
    monkeypatch.setattr(server, "_open", boom)
    result = await server.check_database(config, "ro")
    assert result["ro"]["category"] == "AUTH_FAILED"
    blob = str(result).lower()
    assert "secret-host" not in blob and "admin" not in blob
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: db_conn_mcp.server`.

- [ ] **Step 3: Implement `server.py`**

```python
"""MCP server: 8 tools + 1 prompt, plus testable handler functions."""

from typing import Any, Optional

from .config import set_yolo
from .diagnostics import explain
from .dialects.registry import dialect_for
from .models import Config
from .safety import WriteRejected, authorize_write

TROUBLESHOOT_CHECKLIST = """\
Connection troubleshooting checklist:
- Host/port correct? Is the server actually listening there?
- Firewall or security group blocking the port?
- In Docker, 'localhost' is the container, not the DB host — use the service name or host IP.
- Database name correct (and case-sensitive)?
- Credentials valid; does the role exist?
- SSL: does the server require it? Add '?sslmode=require' if so.
- Connection limit reached? Close idle sessions or raise max_connections.
"""


async def _open(dsn: str, *, read_only: bool) -> Any:
    """Open a connection via the right dialect. Failures bubble up for explain()."""
    return await dialect_for(dsn).connect(dsn, read_only=read_only)


def list_databases(config: Config) -> list[dict]:
    return [c.public() for c in config.connections]


async def list_tables(config: Config, database: str) -> Any:
    conn = await _open(config.get(database).dsn, read_only=True)
    try:
        return await dialect_for(config.get(database).dsn).list_tables(conn)
    finally:
        await conn.close()


async def get_table_schema(config: Config, database: str, table: str) -> Any:
    dsn = config.get(database).dsn
    conn = await _open(dsn, read_only=True)
    try:
        return await dialect_for(dsn).get_schema(conn, table)
    finally:
        await conn.close()


async def sample_table_rows(config: Config, database: str, table: str, n: int = 10) -> Any:
    dsn = config.get(database).dsn
    conn = await _open(dsn, read_only=True)
    try:
        return await dialect_for(dsn).sample_rows(conn, table, n)
    finally:
        await conn.close()


async def execute_read_query(config: Config, database: str, sql: str) -> Any:
    dsn = config.get(database).dsn
    conn = await _open(dsn, read_only=True)
    try:
        return await dialect_for(dsn).execute(conn, sql)
    finally:
        await conn.close()


async def execute_write_query(
    config: Config, database: str, sql: str, user_consent: bool = False
) -> Any:
    conn_cfg = config.get(database)
    authorize_write(conn_cfg, user_consent)  # raises WriteRejected if not allowed
    conn = await _open(conn_cfg.dsn, read_only=False)
    try:
        return await dialect_for(conn_cfg.dsn).execute(conn, sql)
    finally:
        await conn.close()


def set_yolo_mode(config: Config, path, database: str, enabled: bool) -> dict:
    set_yolo(config, path, database, enabled)
    return {"database": database, "yolo": enabled, "persisted": True}


async def check_database(config: Config, database: Optional[str] = None) -> dict:
    names = [database] if database else [c.name for c in config.connections]
    report: dict = {}
    for name in names:
        try:
            conn = await _open(config.get(name).dsn, read_only=True)
            await conn.close()
            report[name] = {"status": "OK"}
        except Exception as e:  # noqa: BLE001 — we sanitize via explain()
            report[name] = explain(e)
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Register MCP tools + prompt** (append to `server.py`)

```python
import mcp.types as types
from mcp.server import Server

app = Server("db-conn-mcp")


def build_app(config: Config, path) -> Server:
    """Wire the testable handlers above into MCP tool/prompt registrations."""

    @app.list_tools()
    async def _tools() -> list[types.Tool]:
        return [
            types.Tool(name="list_databases", description="List configured databases (name, mode, yolo).", inputSchema={"type": "object", "properties": {}}),
            types.Tool(name="list_tables", description="List tables/views in a database.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}}, "required": ["database"]}),
            types.Tool(name="get_table_schema", description="Columns, types, and primary keys for a table.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}, "table": {"type": "string"}}, "required": ["database", "table"]}),
            types.Tool(name="sample_table_rows", description="First N rows of a table (default 10).", inputSchema={"type": "object", "properties": {"database": {"type": "string"}, "table": {"type": "string"}, "n": {"type": "integer", "default": 10}}, "required": ["database", "table"]}),
            types.Tool(name="execute_read_query", description="Run a read-only SELECT. Enforced as a read-only DB transaction.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}, "sql": {"type": "string"}}, "required": ["database", "sql"]}),
            types.Tool(name="execute_write_query", description="Run an INSERT/UPDATE/DELETE/DDL. First read the table and schema, print the EXACT SQL to the user, and get explicit permission. Only call with user_consent=true after they say yes.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}, "sql": {"type": "string"}, "user_consent": {"type": "boolean", "default": False}}, "required": ["database", "sql"]}),
            types.Tool(name="set_yolo_mode", description="Enable/disable per-database yolo (skip write-consent prompts); persisted to connections.json.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["database", "enabled"]}),
            types.Tool(name="check_database", description="Test connectivity for one database (or all). Returns OK or a sanitized cause + fix.", inputSchema={"type": "object", "properties": {"database": {"type": "string"}}}),
        ]

    @app.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        import json

        try:
            if name == "list_databases":
                result = list_databases(config)
            elif name == "list_tables":
                result = await list_tables(config, arguments["database"])
            elif name == "get_table_schema":
                result = await get_table_schema(config, arguments["database"], arguments["table"])
            elif name == "sample_table_rows":
                result = await sample_table_rows(config, arguments["database"], arguments["table"], arguments.get("n", 10))
            elif name == "execute_read_query":
                result = await execute_read_query(config, arguments["database"], arguments["sql"])
            elif name == "execute_write_query":
                result = await execute_write_query(config, arguments["database"], arguments["sql"], arguments.get("user_consent", False))
            elif name == "set_yolo_mode":
                result = set_yolo_mode(config, path, arguments["database"], arguments["enabled"])
            elif name == "check_database":
                result = await check_database(config, arguments.get("database"))
            else:
                result = {"error": f"Unknown tool '{name}'"}
        except WriteRejected as e:
            result = {"rejected": str(e)}
        except (KeyError, ValueError) as e:
            result = {"error": str(e)}
        except Exception as e:  # noqa: BLE001 — sanitize anything DB-related
            result = {"error": explain(e)}
        return [types.TextContent(type="text", text=json.dumps(result, default=str, indent=2))]

    @app.list_prompts()
    async def _prompts() -> list[types.Prompt]:
        return [types.Prompt(name="troubleshoot_connection", description="Full connection-gotchas checklist.")]

    @app.get_prompt()
    async def _prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
        return types.GetPromptResult(
            description="Connection troubleshooting checklist",
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=TROUBLESHOOT_CHECKLIST))],
        )

    return app
```

- [ ] **Step 6: Commit**

```bash
git add src/db_conn_mcp/server.py tests/test_server.py
git commit -m "Add MCP server handlers, tool/prompt registration, and gate wiring"
```

---

## Task 9: CLI, transports, and setup wizard (`cli.py`)

**Files:**
- Create: `src/db_conn_mcp/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests** (arg parsing + wizard config-writing, no transport)

`tests/test_cli.py`:
```python
import json

from db_conn_mcp import cli


def test_parse_defaults_to_stdio():
    args = cli.parse_args([])
    assert args.transport == "stdio"
    assert args.command is None


def test_parse_http_with_config():
    args = cli.parse_args(["--transport", "http", "--config", "/tmp/c.json"])
    assert args.transport == "http"
    assert args.config == "/tmp/c.json"


def test_setup_writes_valid_config(tmp_path):
    path = tmp_path / "connections.json"
    cli.write_first_connection(path, name="dev", dsn="postgresql://x", mode="write")
    data = json.loads(path.read_text())
    assert data["connections"][0]["name"] == "dev"
    assert data["connections"][0]["yolo"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `cli.py`**

```python
"""Command-line entry point: setup wizard + transport launcher."""

import argparse
import asyncio
from pathlib import Path
from typing import Optional, Sequence

from . import config as cfgmod
from .models import Config, Connection
from .server import build_app


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="db-conn-mcp")
    parser.add_argument("command", nargs="?", choices=["setup"], default=None)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def write_first_connection(path: Path, *, name: str, dsn: str, mode: str) -> None:
    """Create connections.json with one entry (used by the wizard)."""
    config = Config(connections=[Connection(name=name, dsn=dsn, mode=mode)])
    cfgmod.save(config, path)


def _run_setup() -> None:
    print("db-conn-mcp setup")
    scope = input("Scope — [g]lobal (~/.db-conn-mcp) or [r]epo (./)? [g]: ").strip().lower() or "g"
    path = cfgmod.GLOBAL_PATH if scope.startswith("g") else Path.cwd() / "connections.json"
    name = input("Connection name: ").strip()
    dsn = input("DSN (e.g. postgresql://user:pass@host:5432/db): ").strip()
    mode = input("Mode — read/write [read]: ").strip().lower() or "read"
    write_first_connection(path, name=name, dsn=dsn, mode=mode)
    print(f"Saved '{name}' to {path}")  # never echo the DSN


async def _serve(args: argparse.Namespace) -> None:
    config, path = cfgmod.load(args.config)
    app = build_app(config, path)
    if args.transport == "stdio":
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())
    else:
        from mcp.server.sse import SseServerTransport  # transport wiring per SDK docs

        raise NotImplementedError(
            "HTTP/SSE transport: wire SseServerTransport per the mcp SDK version in use."
        )


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    if args.command == "setup":
        _run_setup()
        return
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
```

> Note: the exact HTTP/SSE wiring depends on the installed `mcp` SDK version — implement `_serve`'s `http` branch against the SDK's current SSE/streamable-http API at build time (stdio is the v1 default and is fully wired). Update this plan + ARCHITECTURE if the API differs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Smoke-test stdio startup**

Run (with a valid `connections.json` present):
```bash
python -m db_conn_mcp --transport stdio
```
Expected: process starts and waits on stdio (Ctrl-C to exit). Optionally verify via an MCP client / inspector that the 8 tools + prompt list.

- [ ] **Step 6: Commit**

```bash
git add src/db_conn_mcp/cli.py tests/test_cli.py
git commit -m "Add CLI arg parsing, setup wizard, and stdio transport"
```

---

## Task 10: Final pass — lint, full test run, docs sync

**Files:**
- Modify (if needed): `PLAN.md`, `ARCHITECTURE.md`, `README.md` (create)

- [ ] **Step 1: Lint & format**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: clean.

- [ ] **Step 2: Full test suite**

Run: `pytest -q` (and again with `TEST_DATABASE_URL` set to include integration)
Expected: all pass; integration tests skip only when no DB is configured.

- [ ] **Step 3: Create `README.md`** with: what it is, install (`pip install -e .`), `setup` wizard, configuring an MCP client (stdio), the 8 tools, and the yolo/consent model. (Mirror PRD; keep in sync — Rule 7.)

- [ ] **Step 4: Tick completed boxes in `PLAN.md`** Phases 1–6 and confirm ARCHITECTURE matches the built tool/prompt names.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Lint clean, full test pass, and docs sync for v1"
```

---

## Coverage map (plan ↔ spec)

| Spec section | Task(s) |
|---|---|
| §3 Models | 2 |
| §4 Config (resolution, save, set_yolo) | 3 |
| §5 Dialect seam (ABC, registry, Postgres) | 6, 7 |
| §6 Safety gate | 4 |
| §7 Diagnostics | 5 |
| §8 MCP surface (8 tools + prompt, transports) | 8, 9 |
| §9 Error handling (sanitized) | 5, 8 |
| §10 Setup wizard | 9 |
| §11 Security/sanitation | 3 (.gitignore), 5, 7 (quote_ident), 8 |
| §12 Testing strategy | every task (TDD) |
