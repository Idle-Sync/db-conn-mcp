# Changelog

All notable changes to `db-conn-mcp` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every release
that alters observable behaviour carries a **Breaking / Behaviour changes** section — read
that first when upgrading, since it is the part that can bite.

Entries for 0.5.2 and earlier were backfilled from each release's own notes, written at
the time of that release.

## [Unreleased]

### Breaking / Behaviour changes

- **A tool call carrying a parameter the tool doesn't have is now rejected instead of
  quietly ignored.** Previously the unknown argument was dropped and the tool ran anyway
  — calling `check_database(connection="prod")` probed *every* configured database while
  looking like it had targeted one. Such a call now comes back as an error naming the
  unknown parameter(s), listing the ones the tool accepts, and suggesting the closest
  match for a near-miss spelling. Only parameter *names* appear in the message, never
  their values. If a client of yours passes extra arguments, it will now see errors where
  it previously saw (wrong) results — fix the argument name.

### Added

- **Interactive commands now tell you when a newer release is out.** After a command
  like `db-conn-mcp status` or `doctor` finishes, a single line points at the newer
  version and how to upgrade. It only ever appears in a real terminal, is looked up in
  the background so it can never slow a command down (offline just means no line), and
  never changes the command's exit code. Set `DB_CONN_MCP_NO_UPDATE_CHECK=1` to turn it
  off. The MCP server path never checks — a client launching db-conn-mcp makes no
  network calls of ours.

## [0.6.2] — 2026-08-12

The dashboard grows a front door and a face: a bare visit now tells you how in, a
bookmark keeps working, and the page looks like the instrument it is. Still 23 tools and
2 prompts — nothing about querying your databases changed.

### Changed

- **The dashboard has been restyled as a bench instrument.** It now reads like a piece of
  diagnostic equipment sitting next to your terminal rather than a web app: one column,
  engraved section labels, dense bordered rows instead of floating cards, quiet outlined
  buttons, and colour spent only on state. Every row carries a **status lamp** next to its
  title — hollow when idle, pulsing while an action is in flight, and green / amber / red
  once it settles — so you can see what your plumbing is doing without reading a word. The
  "your host process stopped" notice is now a mains-warning strip, and the whole page
  follows your system light/dark setting with contrast checked in both. Nothing you click
  behaves differently; only the appearance changed.
- **The "you need a token" page you get from visiting `http://127.0.0.1:31415` by hand now
  looks like part of the tool** instead of an unstyled browser default. It styles itself
  from a small inline block, because the real stylesheet sits behind the same guard that
  refused you — so that one response is served under a *stricter* policy than the rest of
  the dashboard (`default-src 'none'; style-src 'unsafe-inline'`): it may not fetch a
  script, image, font, frame or request of any kind, from anywhere. It still refuses you,
  still with a 403, and still discloses nothing but the tool's name and `db-conn-mcp gui`.

### Added

- **Opening `http://127.0.0.1:31415` without a token now tells you how to get in.** Instead
  of a raw `{"error": "forbidden"}` with no way forward, a browser navigating to the
  dashboard gets a small page saying it needs a token and to run `db-conn-mcp gui`. The
  request is still refused (it is still a 403, and it still discloses nothing else) — only
  what a human sees changed. Every other unauthenticated request keeps the same opaque JSON
  refusal it always had.
- **The dashboard URL is now bookmarkable for the life of the server run.** Opening the page
  with its token also sets a session cookie, so reloading the tab — or visiting the bare
  `http://127.0.0.1:31415` after the `?token=` is gone — keeps working instead of dropping
  you back to a 403. The cookie is `HttpOnly` and `SameSite=Strict`, it disappears when you
  close the browser, and it authorises **reads only**: anything that spawns a process,
  edits `connections.json`, or writes a client config still requires the real token. As
  before, restarting the server mints a new token and retires every existing session.
- **An empty Databases list now explains itself** instead of rendering nothing: it points
  you at the Add form below, and says outright when adding one will create
  `connections.json` in your home directory.

## [0.6.1] — 2026-08-12

One dashboard polish, found in first real-world use. No other changes.

### Fixed

- **The dashboard no longer leaves you staring at frozen cards.** When the host process
  stops (or the session expires), the notice explaining it now scrolls itself into view
  instead of sitting off screen at the top of the page, and every action it interrupted is
  released: stuck `connecting...` / `verifying...` labels are replaced with
  `stopped - see the notice at the top of the page`, and the buttons they disabled become
  clickable again.

## [0.6.0] — 2026-08-12

A browser dashboard, and the first proof your setup actually speaks MCP. Still 23 tools and
2 prompts — nothing about querying your databases changed, but read the behaviour changes
below: the server now hosts the dashboard by default.

### Added

- **A browser dashboard — the CLI's equal, clickable.** One page on
  `http://127.0.0.1:31415`, in three sections:
  - **Databases** — add, edit, remove and test your connections without hand-editing
    `connections.json`. A stored DSN is **never shown**, by anything, ever: the field is
    write-only, so an edit form starts blank and leaving it blank keeps the DSN already
    saved. A connection's name is fixed once created (to rename, remove and re-add), and
    fallback ports can be set or changed from here. A name that is blank, padded with
    spaces, or contains a `/` is refused: the dashboard could save one, but could never
    edit, test or remove it again.
  - **Clients** — the same nine MCP clients the wizard knows, each with inject/uninject
    buttons and the **exact command and arguments that client would launch**. A client
    whose config file cannot be parsed is listed and explained, never written to — the same
    refusal `setup` and `clients` make.
  - **Verify & Doctor** — the live verification below, plus the full `doctor` sweep with the
    same `ok` / `warn` / `fail` / `skipped` findings the CLI prints.

- **Live MCP verification — "does the binary my client launches actually answer?"** For each
  detected client, the dashboard spawns the *exact* command and arguments stored in that
  client's own config and holds a real MCP conversation with it using the SDK's own client
  library: `initialize`, then `tools/list` (23 expected), then a real `list_databases` call.
  The verdict is one of `answers`, `launch_failed`, `handshake_failed`, `wrong_tool_count`,
  `timeout` — evidence, not a guess. The dashboard never answers from its own process, so a
  client pointed at a *different* install is caught rather than masked. One more button runs
  the same check over the HTTP (SSE) transport (`port_in_use` when port 8000 is already
  taken).

- **Stale-install detection.** When the server a client launches reports a different version
  than the dashboard itself is running, the result is flagged as stale with the upgrade
  command — the "you upgraded but that client still starts the old copy" case, now visible
  per client rather than inferred.

- **`db-conn-mcp gui`** opens the dashboard: it reuses the one a running server is already
  hosting, or starts a standalone one (which shuts itself down after 15 idle minutes) and
  opens your browser at it. `db-conn-mcp setup` now ends with a tip pointing at the command,
  so a first-time user discovers the dashboard instead of never hearing about it.

- **A dashboard tab left open from before a restart says so.** Each start mints a fresh
  token, so an old tab's requests are refused; the page now shows a single banner asking
  you to run `db-conn-mcp gui` again, instead of every panel failing for no stated reason.

### Breaking / Behaviour changes

- **Starting the MCP server now also starts a local dashboard listener on
  `127.0.0.1:31415`.** Every server start hosts the dashboard alongside the MCP protocol;
  the first server process to start wins the port and the rest skip it silently, so several
  clients running the server at once still means exactly one dashboard. It listens on
  loopback only — no connection from another machine can reach it — and every request,
  including the page itself, must carry a secret token generated at start-up and stored in a
  user-only file at `~/.db-conn-mcp/gui-token`. To turn it off, add `--no-gui` to the
  db-conn-mcp command in your client's config. If port 31415 is already taken by something
  else on your machine, the server simply carries on without a dashboard.

- **The server now reports its own version to your MCP client.** During the initialize
  handshake, `serverInfo.version` used to be the version of the underlying MCP SDK (e.g.
  `1.27.2`) because the SDK fills that in when a server does not supply one. It is now the
  db-conn-mcp version (e.g. `0.5.6`). If you script against that field, expect our version
  there from now on — and it finally lets a client tell which build of db-conn-mcp it is
  talking to.

### Changed

- Three dependencies are now declared explicitly: **`starlette`**, **`uvicorn`** and
  **`httpx`**. All three already arrived with `mcp`, but the dashboard imports them
  directly, and an import of ours must not rest on somebody else's transitive pin.
- `db_conn_mcp.server.run()` takes a new `gui=True` keyword (the CLI passes `not --no-gui`).
  Calling it as before is unchanged apart from the listener described above.

## [0.5.6] — 2026-08-11

Codex joins the setup targets, and no client config you have is overwritten any more when it
cannot be read. Still 23 tools and 2 prompts — nothing about querying your databases changed.

### Added

- **Codex is now a setup target.** `db-conn-mcp setup` and `db-conn-mcp clients` detect
  `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`) and write a `[mcp_servers.db-conn-mcp]`
  entry, bringing the auto-injection list to nine clients. One entry covers the ChatGPT
  desktop app, the Codex CLI and the IDE extension, which share that file. Your comments,
  formatting and other MCP servers in that file are preserved. The entry carries an explicit
  `startup_timeout_sec = 30`, because Codex's 10s default can be tight for Python startup on
  a cold disk.

- **`doctor` now flags a client config it cannot read.** A detected MCP client whose config
  file does not parse gets a `client_paths` warning (`repair_client_config`) telling you to fix
  that file by hand and re-run `db-conn-mcp clients`. Previously `clients` and `status` both
  showed the problem while `doctor` — the one command whose whole job is diagnostics — stayed
  silent about it. The finding names the client and the path, never the file's contents.

### Fixed

- **A client config that is not valid UTF-8 no longer crashes `db-conn-mcp status`.** Reading a
  client's config used to guard against a bad-JSON or an I/O error but not against undecodable
  bytes, so a config saved in a non-UTF-8 encoding took the whole command down with a traceback.
  Such a file is now treated like any other unreadable config: the client is listed as
  `config unreadable` and left untouched. `doctor` and the injection commands were affected the
  same way.

### Breaking / Behaviour changes

- **A client config file that exists but does not parse is no longer overwritten.** Previously,
  if `setup` or `clients` could not read a client's config, it treated the file as empty and
  wrote a fresh one — silently discarding whatever was in there, including your other MCP
  servers. It now skips that client, tells you which file it could not parse, and leaves the
  file untouched. The client still appears in every listing — `setup`, `status` and
  `clients --remove` — marked `config unreadable`, so you can fix it by hand and re-run.
  `clients --remove` reports it without offering it as a removal target (uninjecting means
  rewriting the file, which is exactly what we refuse to do), so it no longer answers a broken
  config with a bare "not injected into any detected MCP client". "Could not read" covers a
  syntax error, an unreadable file, and a file whose top level is not an object. This affects
  all nine clients, not just Codex.

### Changed

- Adds one dependency, **`tomlkit`** (pure Python, no transitive dependencies). Codex's config
  is TOML, and the standard library has a TOML *reader* but no writer at any Python version.

## [0.5.5] — 2026-08-10

Documentation and packaging only. No code change, no behaviour change — still 23 tools and
2 prompts, and nothing about how the server runs is different from 0.5.4.

### Added

- **This changelog**, covering all releases back to 0.1.0. Entries for 0.5.2 and earlier were
  backfilled from each release's own contemporaneous notes.
- A **Changelog link on the PyPI project page**, via a `Changelog` project URL.

### Changed

- Release notes now come from this file. The release workflow extracts the tag's section and
  passes it to `gh release create --notes-file`, so GitHub, PyPI and the repo cannot drift
  apart. A missing section logs a warning and falls back to generated notes, so a release can
  never fail for want of prose.

  This replaces `--generate-notes`, which emitted PR-title lists. That was how 0.5.3 shipped a
  breaking change to tool output described only as "Fence tool output as untrusted database
  data" — accurate about the work, silent about the consequence.
- The notes for 0.5.2, 0.5.3 and 0.5.4 have been rewritten from their changelog entries.
  0.1.0 through 0.5.1 keep their original hand-written notes, which were already better than
  anything a regeneration would produce.
- Rule 7 (living documentation) now covers `CHANGELOG.md`, so entries are written with the
  change rather than remembered afterwards.

## [0.5.4] — 2026-08-10

Infrastructure only. No tool was added, removed or changed — still 23 tools and 2 prompts.

### Breaking / Behaviour changes

- **Python 3.10 and 3.11 are no longer supported.** The floor is now **3.12**. Python 3.10
  reaches end of life on 2026-10-31 and 3.11 is already security-only. If you are on 3.10 or
  3.11, `pip install --upgrade db-conn-mcp` will keep you on 0.5.3 rather than fail — pip
  honours `requires-python` — so the effect is silent: you simply stop receiving updates.
  Install on 3.12+ to continue.

  Note this shipped as a patch bump. Under semver, dropping interpreters argues for a minor.

### Added

- `.github/workflows/ci.yml` — lint and the full test suite on every push to `main` and every
  pull request: Python 3.12, 3.13 and 3.14 on Linux, plus the floor version on Windows and
  macOS (`client_specs()` branches three ways on `sys.platform`). Previously `publish.yml` was
  the only workflow, it runs on tags only, and it never ran `pytest` or `ruff` — so nothing had
  ever been gated on the tests. The green "Analyze (python)" check on pull requests is GitHub's
  default CodeQL setup, not this project's suite.
- Python 3.13 and 3.14 classifiers. Both were missing, so the package did not advertise support
  for either actively-maintained branch.

### Changed

- `ruff` is pinned exactly (`ruff==0.16.2`) in the `dev` extra rather than floored, and CI
  installs it from that extra so `pyproject.toml` stays the single source of truth. A floor let
  CI resolve a newer ruff than a developer had locally and fail on rules they could not
  reproduce.
- Markdown is excluded from ruff. Ruff 0.16+ formats Python blocks inside `.md`, and doc
  snippets here are illustrative while `docs/superpowers/` holds dated artifacts that must stay
  verbatim.
- Internal modernisation unlocked by the new floor: `asyncio.TimeoutError` → `TimeoutError`
  (the same object since 3.11) and `timezone.utc` → `datetime.UTC`. No behaviour change.

## [0.5.3] — 2026-08-10

### Breaking / Behaviour changes

- **A tool result's text content block is no longer bare JSON.** Every result is now fenced
  between `<<<UNTRUSTED DATABASE DATA …>>>` markers, so `json.loads(result.content[0].text)`
  will raise.

  **`structuredContent` is unchanged and byte-identical**, so clients that read it — the
  spec-conforming path, and what all 23 tools declare an output schema for — are unaffected.
  Only code parsing the text channel needs updating. A `list[dict]` tool emits one text block
  per item and each is fenced individually.

- **A failed commit now consumes its dry-run grant.** Previously the grant survived, letting
  an agent retry the identical statement immediately. Because a commit can fail ambiguously —
  a statement timeout, or a connection dropped during `COMMIT`, may still have applied the
  write server-side — that retry could silently double-apply a non-idempotent statement such as
  `UPDATE t SET n = n + 1`. One preview now authorises exactly one commit attempt, so a failed
  commit requires a fresh dry-run that shows the agent the current state before it retries.

  This reverses a deliberate earlier choice that favoured retry ergonomics.

### Added

- Prompt-injection hardening (`guard.py`). Database content is attacker-controllable: anyone
  who can insert a row, or name a table or column, chooses text that lands verbatim in the
  agent's context. Two layers, both advisory to the model:
  - Every tool result's text channel is fenced with an explicit "this is data, not
    instructions" notice, applied at a single seam (`GuardedFastMCP.call_tool`) rather than in
    each tool.
  - A standing policy travels in the server's `initialize` instructions — the durable half,
    since a client reading only `structuredContent` never sees the per-response fence.

  Markers found inside a payload are defanged first, so hostile content cannot close the fence
  early and appear to speak with the server's authority.

  This is mitigation, not a guarantee: a determined injection can still influence a model.

### Changed

- Both raw-SQL tool descriptions now tell the agent to confirm table and column names with
  `get_table_schema` (or `list_tables` / `find_columns`) when they are not already in context,
  and explicitly not to re-fetch a schema it already has. Guidance only — nothing is enforced
  server-side and no call is mandatory.

### Fixed

- The `doctor` MCP tool re-checks the config file's existence per call, as the CLI already did.
  A config deleted or made unreadable after the server started produced a hard `fail` reading
  "connections.json exists but could not be read" instead of the intended skip, "no
  configuration found — run `db-conn-mcp setup`".

## [0.5.2] — 2026-08-10

Closes #12. Two features: whole-setup diagnostics, and making the write preview mandatory.

### Breaking / Behaviour changes

- **`execute_write_query` now defaults to `dry_run=true`, and the preview is enforced rather
  than advisory.** A commit (`dry_run=false`) is **rejected** unless the identical statement was
  dry-run first. A bare call therefore previews instead of committing — an agent that used to
  call the tool and have it write will now get a preview back.

  The grant fingerprints database + SQL + params with a 10-minute TTL. `skip_dry_run=true`
  exists solely for an agent to attest the user explicitly asked to skip the preview.

  Gate order is now `mode` → dry-run-first → `yolo` → `user_consent`. **`yolo` cannot skip the
  preview**, and nothing can ever make a `read` database writable.

### Added

- **`doctor`** — whole-setup diagnostics, not just connectivity. One engine (`doctor.py`), two
  surfaces: `db-conn-mcp doctor [--offline]` (exit 0 only if nothing fails, 2 otherwise) and a
  `doctor` MCP tool returning `{check, status, detail, suggested_action}` so an agent can
  self-diagnose mid-session. **23 tools** total.

  Six checks, each drawn from a real failure during the 0.5.0 → 0.5.1 upgrade: running server
  processes older than the installed package (psutil optional, and it reports its own host
  process when stale); a cache-bypassed PyPI version check; per-database connectivity plus a
  **credential-free** listener probe of fallback ports, catching "a different local Postgres
  answered my port"; config-schema typos with did-you-mean hints; secrets exposure (POSIX file
  mode, git-committability); and injected client entries whose command path no longer exists.

  The engine never raises — a crashing check degrades to a `fail` naming only the exception
  type. A poisoned-DSN sweep test asserts no DSN, host, user or password can reach any finding,
  including via pydantic validation errors (Rule 6).
- `check_database` UNREACHABLE results now report `failed_port`.

### Changed

- MCP-client helpers moved from `cli.py` into a new `clients.py` (re-exported for
  compatibility), unlocking reuse without a circular import.

## [0.5.1] — 2026-08-06

### Added

- **`fallback_ports`** (#10) — tunnel-friendly connections. A database behind an SSH tunnel does
  not always land on the same local port; a connection can now declare where else to look:

  ```json
  { "name": "aws-aurora-db", "dsn": "postgresql://…@localhost:5432/mydb",
    "fallback_ports": [5433, 15432], "mode": "read" }
  ```

  Probed **only** on refused/timeout, in order, first answer wins — auth, TLS, DNS and
  database errors fail immediately, so a wrong-credentials error is never masked by
  port-hopping. Bounded: listed ports only, no scanning, 5s cap per fallback probe. The winning
  port is remembered for the process lifetime and surfaced as `active_port` in
  `list_databases`, `check_database` and `db-conn-mcp check`.

  Fully backward compatible: existing config files parse unchanged, and the server never writes
  the key into a file that lacks it.
- Automated releases — pushing a `vX.Y.Z` tag builds, publishes to PyPI and the MCP Registry,
  and creates the GitHub Release.
- README gained an Upgrade section covering pip/pipx/uv/uvx.

## [0.5.0] — 2026-08-06

Tool surface grows **12 → 22** (issue #8, PR #9), from months of field use.

### Added

- **Bind parameters** — optional `params` on both query tools, passed to the driver as real
  `$1`/`$2` bind args. Values never touch the SQL string.
- **Dry-run writes** — `execute_write_query(dry_run=true)` executes in a transaction, reports
  would-be `rows_affected`, and always rolls back. Opt-in here; made mandatory in 0.5.2.
  Documented caveat: a dry-run still executes server-side until rollback, so brief locks,
  sequence advancement and trigger side effects are real.
- **Per-call timeouts** — optional `timeout_ms` on both query tools.
- **Server-side cursors** — `open_query_cursor` / `fetch_rows` / `close_cursor` for result sets
  too large for one response. Bounded: max 5 open, 15-minute idle reaping, auto-close on drain.
- **`get_object_definition`** — faithful definitions for views, functions/procedures (all
  overloads), triggers, sequences and indexes via Postgres' own `pg_get_*def`.
- **`cancel_query`** — cancel a stuck statement by pid via `pg_cancel_backend()`.
- **Operational insight tools**, all read-only: `diff_schemas`, `check_sequences`,
  `table_stats`, `show_activity` (sanitized — no user names or client addresses; query text
  opt-in and truncated) and `explain_query`.

### Fixed

- Pinned `mcp>=1.0.0,<2.0.0` — SDK 2.0 removed `mcp.server.fastmcp` and broke fresh installs.

## [0.4.1] — 2026-06-30

### Added

- **`format="sql"` on `get_database_schema`** — a self-contained, runnable DDL script (schemas,
  sequences, tables, PK/FK/UNIQUE/CHECK, indexes, trigger functions, triggers) assembled from
  native `pg_get_*def`. No external tools required.
- **`dump_schema_faithful`** — byte-faithful `pg_dump --schema-only`. Requires the `pg_dump`
  binary; returns `pg_dump_not_found` with per-OS install guidance when absent. The DSN goes
  via `PG*` env vars, never argv, and all error text is sanitized.
- **`faithful_schema_export` prompt** — guides choosing between the two exports.

## [0.4.0] — 2026-06-30

### Added

- **`get_database_schema`** — the entire schema (all tables, columns, types, keys) in one call,
  with optional `output_dir` writing `{database}_schema_{UTC}.json`.
- `db-conn-mcp -v` also reports the exact build commit, so you can tell which commit is
  installed on any device.

### Fixed

- Silent hang when the stdio server was run directly in a terminal.

## [0.3.0] — 2026-06-03

### Added

- **`db-conn-mcp reset`** — removes all connections by deleting `connections.json` after a
  confirmation. A separate command from `remove <name>` (Rule 10, one concern per tool).

## [0.2.1] — 2026-06-03

### Added

- `db-conn-mcp -v` / `--version` — prints the installed version and exits, for verifying which
  version `uvx`/`pipx` resolved.

## [0.2.0] — 2026-06-03

### Added

- **`find_columns(database, pattern)`** — fuzzy, case-insensitive search for columns **by
  name** across all tables.
- **`search_value(database, value, tables?, limit_per_column?)`** — fuzzy search for **where a
  value appears** in the data. Bounded by a statement timeout and per-column limit, with
  partial results flagged `truncated`.

  Two tools rather than one with a switch, per Rule 10. Both read-only, with safely-quoted
  identifiers and parameterized values. **10 tools** total.

## [0.1.2] — 2026-06-03

### Security

- **`execute_read_query` could write to a `read`-mode database.** The transaction could be
  flipped to read-write from within the read path — working exploit:
  `SET TRANSACTION READ WRITE; <write>`.

  The read tool now validates that the SQL is a **single read-only statement**
  (`SELECT`/`WITH`/`VALUES`/`TABLE`/`SHOW`/`EXPLAIN`) before any connection opens, and relies on
  asyncpg's single-command execution for the read path. The docs now also recommend a read-only
  database role as the privilege-level boundary.

  Reported and fixed by @axonova-bot (#1, #2). Verified against a live PostgreSQL.

## [0.1.1] — 2026-06-03

### Added

- Published to PyPI and listed on the official MCP Registry
  (`io.github.Idle-Sync/db-conn-mcp`) — `server.json` metadata, the PyPI ownership marker, and
  a tokenless OIDC publish job.

No functional changes versus 0.1.0.

## [0.1.0] — 2026-06-03

First release — a dead-simple, self-hosted MCP server for safely querying databases with AI
agents.

### Added

- PostgreSQL support behind a `Dialect` seam, so MySQL/SQLite are a one-file addition later.
- **Native read-only safety**: `read` databases are enforced read-only by Postgres itself;
  writes gated `mode` → `yolo` → `user_consent`.
- **Sanitized diagnostics**: connection errors return a category and a fix, never the DSN or
  credentials.
- **8 MCP tools** plus a `troubleshoot_connection` prompt; `stdio` and `http` (SSE) transports.
- Setup and management CLI — `setup`, `status`, `add`, `clients`, `check`, `remove`, `yolo` —
  with auto-injection into 8 MCP clients.

[Unreleased]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.6...v0.6.0
[0.5.6]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Idle-Sync/db-conn-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Idle-Sync/db-conn-mcp/releases/tag/v0.1.0
