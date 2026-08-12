"use strict";

/* The dashboard front-end.
 *
 * Two rules shape every function here:
 *  1. Nothing is ever assigned as HTML. Everything the server returns - table
 *     names, tool output, client labels, doctor details - is untrusted data and
 *     reaches the page only through textContent or a cloned <template>.
 *  2. Click to run. Loading the page performs two reads (summary, databases) and
 *     nothing else: no process is spawned, no database is probed, no file is
 *     written until a human presses a button.
 */

/**
 * The session token this page will send on every call.
 *
 * The query string is the freshly-opened case. A bookmarked or reloaded URL has
 * none - it was authenticated by the session cookie, which is HttpOnly and so
 * unreadable here - and then the token the server stamped into the page is the only
 * source. Both are percent-encoded server-side; URLSearchParams already decodes.
 *
 * Read at load time: the script is deferred, so the document is fully parsed.
 */
function sessionToken() {
  const fromQuery = new URLSearchParams(location.search).get("token");
  if (fromQuery) {
    return fromQuery;
  }
  const meta = document.querySelector('meta[name="gui-token"]');
  return meta ? decodeURIComponent(meta.content) : "";
}

const token = sessionToken();
const HDRS = { "X-GUI-Token": token, "Content-Type": "application/json" };

/** Cards by client key, so a refresh can put a message back where it belongs. */
const clientCards = new Map();

const VERDICT_LABEL = {
  answers: "answers",
  launch_failed: "launch failed",
  handshake_failed: "handshake failed",
  wrong_tool_count: "wrong tool count",
  timeout: "timed out",
  port_in_use: "port in use",
};

const VERDICT_CLASS = {
  answers: "ok",
  launch_failed: "fail",
  handshake_failed: "fail",
  timeout: "fail",
  wrong_tool_count: "warn",
  port_in_use: "warn",
};

const STATUS_CLASS = { ok: "ok", warn: "warn", fail: "fail", skipped: "skip", skip: "skip" };

// ---- plumbing -------------------------------------------------------------

/** Shown when the token this page carries is no longer the server's. ASCII on purpose. */
const EXPIRED_TEXT = "This dashboard session expired - run db-conn-mcp gui to open a fresh one.";

/**
 * Fetch with the session token. Two distinct terminal states, two distinct banners:
 * a transport failure means the host process died, and a 401/403 means the process
 * is alive but minted a new token (every restart does) while this tab kept the old.
 */
async function api(path, opts = {}) {
  let resp;
  try {
    resp = await fetch(path, { headers: HDRS, cache: "no-store", ...opts });
  } catch (err) {
    deadPage();
    throw err;
  }
  if (resp.status === 401 || resp.status === 403) {
    expiredPage();
  }
  return resp;
}

/** Left in place of whatever was mid-flight when the host stopped answering. */
const SETTLED_TEXT = "stopped - see the notice at the top of the page";

/**
 * Show the banner and settle the page around it. Deliberately one-way and
 * first-state-wins: it only fires while the banner is still hidden, so whichever
 * terminal state was reached first is the one that stays on screen.
 *
 * The scroll is the point of this function. The banner lives at the top of the
 * page, and the click that discovers the dead host is usually further down, so
 * without it the user is left staring at cards that simply stopped.
 *
 * `text`, when given, is assigned as textContent like everything else here.
 */
function revealBanner(text) {
  const banner = byId("dead-banner");
  if (!banner || !banner.hidden) {
    return;
  }
  if (text) {
    banner.textContent = text;
  }
  banner.hidden = false;
  banner.scrollIntoView({ behavior: "smooth", block: "start" });
  settleInFlight();
}

/** Reveal the "your server is gone" banner, keeping the markup's own wording. */
function deadPage() {
  revealBanner(null);
}

/** Reveal the same banner with the expired-session text. */
function expiredPage() {
  revealBanner(EXPIRED_TEXT);
}

/**
 * End every action that will never come back: nothing on this page can finish
 * once the banner is up. Buttons are re-enabled so the page is not frozen (a
 * later click just re-runs a fetch that fails the same way), and every status
 * still showing in-flight text is replaced with a terminal line pointing at the
 * banner. Nothing is rebuilt and nothing is re-fetched - the server is gone.
 */
function settleInFlight() {
  document.querySelectorAll("button[disabled]").forEach((button) => {
    button.disabled = false;
  });
  // `message` rewrites className, so this both re-labels the node and clears its
  // in-flight tag: calling it twice is a no-op.
  document.querySelectorAll(".in-flight").forEach((node) => {
    message(node, SETTLED_TEXT, "fail-text");
  });
}

/** The decoded JSON body, or null when there is not one (never the raw text). */
async function readJson(resp) {
  try {
    return await resp.json();
  } catch (err) {
    return null;
  }
}

/** The server's fixed error string for a failed call, with its field names. */
function errorText(payload, fallback) {
  if (payload && typeof payload.error === "string") {
    const fields = Array.isArray(payload.fields) ? payload.fields : [];
    return fields.length ? `${payload.error}: ${fields.join(", ")}` : payload.error;
  }
  return fallback;
}

/** Wrap an async handler: a dead host ends as the banner, not a console rejection. */
function guard(fn) {
  return (event) => {
    fn(event).catch(() => {});
  };
}

/** Run `fn` with every button in `buttons` disabled - one in-flight action at a time. */
async function busy(buttons, fn) {
  const live = buttons.filter(Boolean);
  live.forEach((b) => {
    b.disabled = true;
  });
  try {
    return await fn();
  } finally {
    live.forEach((b) => {
      b.disabled = false;
    });
  }
}

// ---- DOM helpers (textContent only) ---------------------------------------

function byId(id) {
  return document.getElementById(id);
}

function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

/** Clone a <template> and hand back its single root element. */
function fromTemplate(id) {
  return byId(id).content.cloneNode(true).firstElementChild;
}

/** Show `text` in an element that starts hidden; empty text hides it again. */
function message(node, text, className) {
  node.className = className ? `msg ${className}` : "msg";
  node.textContent = text || "";
  node.hidden = !text;
}

/**
 * Show `text` for an action that is still running. Same as `message`, plus a tag
 * so `settleInFlight` can find the statuses that will never resolve on their own.
 */
function working(node, text) {
  message(node, text, null);
  node.classList.add("in-flight");
}

/** Paint a badge: fixed vocabulary in the class, untrusted-safe text inside. */
function badge(node, className, text) {
  node.className = `badge ${className}`;
  node.textContent = text;
  node.hidden = false;
}

/** A launch line as one copy-pasteable string (Windows paths and all). */
function launchLine(command, args) {
  return [command || "", ...(Array.isArray(args) ? args : [])].join(" ").trim();
}

// ---- summary --------------------------------------------------------------

async function renderSummary() {
  const line = byId("summary-line");
  const resp = await api("/api/summary");
  const data = resp.ok ? await readJson(resp) : null;
  if (!data) {
    message(line, "could not read the server summary", "fail-text");
    return;
  }
  clear(line);
  line.className = "muted";
  line.appendChild(make("span", null, `version ${data.version} - `));
  const found = make("span", null, data.config_found ? "connections.json found" : "no connections.json yet");
  line.appendChild(found);
  renderClients(Array.isArray(data.clients) ? data.clients : []);
}

// ---- clients --------------------------------------------------------------

function renderClients(rows) {
  const list = byId("client-list");
  clear(list);
  clientCards.clear();
  if (!rows.length) {
    list.appendChild(make("p", "muted", "No MCP clients were detected on this machine."));
    return;
  }
  rows.forEach((row) => list.appendChild(clientCard(row)));
}

function clientCard(row) {
  const card = fromTemplate("tpl-client");
  card.querySelector(".c-label").textContent = row.label || row.key;
  const state = card.querySelector(".c-state");
  const inject = card.querySelector(".c-inject");
  const uninject = card.querySelector(".c-uninject");
  const verify = card.querySelector(".c-verify");
  const msg = card.querySelector(".c-msg");
  const out = card.querySelector(".c-verify-out");

  if (row.unreadable) {
    badge(state, "fail", "config unreadable");
    message(msg, "that client's config could not be parsed; fix it by hand", "fail-text");
    [inject, uninject, verify].forEach((b) => {
      b.disabled = true;
    });
  } else if (row.injected) {
    badge(state, "ok", "injected");
    inject.textContent = "Re-inject";
  } else {
    badge(state, "warn", "not injected");
    uninject.disabled = true;
    verify.disabled = true;
  }

  const line = launchLine(row.command, row.args);
  if (line) {
    // Rule: show what will actually be launched. A stale pipx/uvx binary on PATH
    // is the whole footgun, and it is only visible in this string.
    card.querySelector(".c-launch-label").hidden = false;
    const pre = card.querySelector(".c-launch");
    pre.textContent = line;
    pre.hidden = false;
  }

  inject.addEventListener(
    "click",
    guard(() => clientAction(row.key, "inject", [inject, uninject, verify]))
  );
  uninject.addEventListener(
    "click",
    guard(() => clientAction(row.key, "uninject", [inject, uninject, verify]))
  );
  verify.addEventListener(
    "click",
    guard(async () => {
      await busy([inject, uninject, verify], async () => {
        working(msg, "spawning the server and speaking MCP to it...");
        const result = await verifyClient(row.key, row.label || row.key);
        message(msg, "", null);
        clear(out);
        out.appendChild(result);
      });
    })
  );
  clientCards.set(row.key, { card, msg });
  return card;
}

async function clientAction(key, action, buttons) {
  await busy(buttons, async () => {
    const resp = await api(`/api/clients/${encodeURIComponent(key)}/${action}`, { method: "POST" });
    const payload = await readJson(resp);
    const ok = resp.ok;
    const text = ok
      ? action === "inject"
        ? "registered - restart that client to pick it up"
        : "entry removed - restart that client to drop it"
      : errorText(payload, "the request failed");
    await renderSummary();
    const entry = clientCards.get(key);
    if (entry) {
      message(entry.msg, text, ok ? null : "fail-text");
    }
  });
}

// ---- databases ------------------------------------------------------------

async function renderDatabases() {
  const list = byId("db-list");
  const status = byId("db-status");
  const resp = await api("/api/databases");
  if (resp.status === 409) {
    clear(list);
    message(
      status,
      "connections.json exists but could not be loaded - fix it by hand; nothing " +
        "will be written to it until it loads.",
      "fail-text"
    );
    return;
  }
  const rows = resp.ok ? await readJson(resp) : null;
  if (!Array.isArray(rows)) {
    clear(list);
    message(status, "could not read the connection list", "fail-text");
    return;
  }
  status.hidden = true;
  clear(list);
  if (!rows.length) {
    list.appendChild(make("p", "muted", "No connections configured yet - add one below."));
    return;
  }
  rows.forEach((row) => list.appendChild(databaseCard(row)));
}

function databaseCard(row) {
  const card = fromTemplate("tpl-db");
  card.querySelector(".db-name").textContent = row.name;
  badge(card.querySelector(".db-mode"), row.mode === "write" ? "warn" : "ok", row.mode);
  if (row.yolo) {
    card.querySelector(".db-yolo").hidden = false;
  }
  if (Array.isArray(row.fallback_ports) && row.fallback_ports.length) {
    const ports = card.querySelector(".db-ports");
    ports.textContent = `fallback ports: ${row.fallback_ports.join(", ")}`;
    ports.hidden = false;
  }

  const check = card.querySelector(".db-check");
  const edit = card.querySelector(".db-edit");
  const remove = card.querySelector(".db-remove");
  const msg = card.querySelector(".db-msg");
  const out = card.querySelector(".db-check-out");
  const form = card.querySelector(".db-form");
  const buttons = [check, edit, remove];

  form.querySelector(".f-name").value = row.name;
  form.querySelector(".f-mode").value = row.mode;
  form.querySelector(".f-yolo").checked = Boolean(row.yolo);
  form.querySelector(".f-ports").value = Array.isArray(row.fallback_ports)
    ? row.fallback_ports.join(", ")
    : "";
  const writeNote = form.querySelector(".f-write-note");
  const mode = form.querySelector(".f-mode");
  writeNote.hidden = mode.value !== "write";
  mode.addEventListener("change", () => {
    writeNote.hidden = mode.value !== "write";
  });

  check.addEventListener(
    "click",
    guard(async () => {
      await busy(buttons, async () => {
        working(msg, "connecting...");
        const resp = await api(`/api/databases/${encodeURIComponent(row.name)}/check`, {
          method: "POST",
        });
        const payload = await readJson(resp);
        clear(out);
        if (!resp.ok || !Array.isArray(payload)) {
          message(msg, errorText(payload, "the check failed"), "fail-text");
          return;
        }
        message(msg, "", null);
        payload.forEach((entry) => out.appendChild(checkRow(entry)));
      });
    })
  );

  edit.addEventListener("click", () => {
    form.hidden = !form.hidden;
    edit.textContent = form.hidden ? "Edit" : "Close editor";
  });

  form.querySelector(".f-cancel").addEventListener("click", () => {
    form.hidden = true;
    edit.textContent = "Edit";
  });

  form.addEventListener(
    "submit",
    guard(async (event) => {
      event.preventDefault();
      const error = form.querySelector(".f-error");
      const patch = { mode: mode.value, yolo: form.querySelector(".f-yolo").checked };
      const dsn = form.querySelector(".f-dsn").value.trim();
      if (dsn) {
        patch.dsn = dsn;
      }
      const ports = parsePorts(form.querySelector(".f-ports").value);
      if (ports === false) {
        message(error, "fallback ports must be whole numbers, e.g. 5433, 5434", "fail-text");
        return;
      }
      if (ports) {
        patch.fallback_ports = ports;
      }
      await busy([form.querySelector(".f-save")], async () => {
        const resp = await api(`/api/databases/${encodeURIComponent(row.name)}`, {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
        if (!resp.ok) {
          message(error, errorText(await readJson(resp), "the edit was rejected"), "fail-text");
          return;
        }
        await renderDatabases();
      });
    })
  );

  remove.addEventListener(
    "click",
    guard(async () => {
      if (!window.confirm(`Remove "${row.name}" from connections.json? Its DSN goes with it.`)) {
        return;
      }
      await busy(buttons, async () => {
        const resp = await api(`/api/databases/${encodeURIComponent(row.name)}`, {
          method: "DELETE",
        });
        if (!resp.ok) {
          message(msg, errorText(await readJson(resp), "the removal failed"), "fail-text");
          return;
        }
        await renderDatabases();
      });
    })
  );

  return card;
}

/** One row of `check_database` output - already sanitized server-side. */
function checkRow(entry) {
  const line = make("p", "msg");
  const mark = make("span", null, entry.status || "?");
  mark.className = `badge ${entry.status === "OK" ? "ok" : "fail"}`;
  line.appendChild(mark);
  const bits = [entry.database];
  if (entry.active_port) {
    bits.push(`port ${entry.active_port}`);
  }
  if (entry.failed_port) {
    bits.push(`failed on port ${entry.failed_port}`);
  }
  if (entry.category) {
    bits.push(entry.category);
  }
  if (entry.detail) {
    bits.push(entry.detail);
  }
  line.appendChild(make("span", null, ` ${bits.join(" - ")}`));
  return line;
}

/**
 * Parse the fallback-ports field.
 * Returns an array of ports, `null` when the field is empty (which means "leave
 * the stored value alone" - the API cannot clear the field, so the form does not
 * pretend to), or `false` when the text is not a port list.
 */
function parsePorts(raw) {
  const text = raw.trim();
  if (!text) {
    return null;
  }
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const ports = [];
  for (const part of parts) {
    if (!/^\d+$/.test(part)) {
      return false;
    }
    const value = Number(part);
    if (value < 1 || value > 65535) {
      return false;
    }
    ports.push(value);
  }
  return ports;
}

async function addDatabase(event) {
  event.preventDefault();
  const error = byId("add-error");
  const ports = parsePorts(byId("add-ports").value);
  if (ports === false) {
    message(error, "fallback ports must be whole numbers, e.g. 5433, 5434", "fail-text");
    return;
  }
  const payload = {
    name: byId("add-name").value.trim(),
    dsn: byId("add-dsn").value,
    mode: byId("add-mode").value,
    yolo: byId("add-yolo").checked,
  };
  if (ports) {
    payload.fallback_ports = ports;
  }
  await busy([byId("add-submit")], async () => {
    const resp = await api("/api/databases", { method: "POST", body: JSON.stringify(payload) });
    if (!resp.ok) {
      message(error, errorText(await readJson(resp), "the connection was rejected"), "fail-text");
      return;
    }
    message(error, "", null);
    byId("add-form").reset();
    byId("add-write-note").hidden = true;
    byId("add-panel").open = false;
    await renderDatabases();
  });
}

// ---- verify & doctor ------------------------------------------------------

/** POST one client verification and hand back a rendered card. */
async function verifyClient(key, label) {
  const resp = await api(`/api/verify/client/${encodeURIComponent(key)}`, { method: "POST" });
  const payload = await readJson(resp);
  if (!resp.ok) {
    return failureCard(label, errorText(payload, "the verification could not run"));
  }
  return verifyCard(label, payload);
}

async function verifyHttp() {
  const resp = await api("/api/verify/http", { method: "POST" });
  const payload = await readJson(resp);
  if (!resp.ok) {
    return failureCard("HTTP transport", errorText(payload, "the verification could not run"));
  }
  return verifyCard("HTTP transport", payload);
}

function failureCard(target, detail) {
  const card = fromTemplate("tpl-verify");
  card.querySelector(".v-target").textContent = target;
  badge(card.querySelector(".v-verdict"), "fail", "not run");
  card.querySelector(".v-detail").textContent = detail;
  return card;
}

function verifyCard(target, result) {
  const card = fromTemplate("tpl-verify");
  card.querySelector(".v-target").textContent = target;
  const verdict = String(result.verdict || "");
  badge(
    card.querySelector(".v-verdict"),
    VERDICT_CLASS[verdict] || "skip",
    VERDICT_LABEL[verdict] || verdict || "unknown"
  );
  if (result.stale) {
    // An `answers` verdict from a binary of a different version is the quiet
    // failure this whole page exists to surface: it works, but it is not this code.
    card.querySelector(".v-stale").hidden = false;
  }
  card.querySelector(".v-detail").textContent = result.detail || "";
  if (result.suggested_action) {
    const action = card.querySelector(".v-action");
    action.textContent = `suggested: ${result.suggested_action}`;
    action.hidden = false;
  }
  const meta = [];
  if (result.server_name) {
    meta.push(`server ${result.server_name}`);
  }
  if (result.server_version) {
    meta.push(`reports version ${result.server_version}`);
  }
  if (result.gui_version) {
    meta.push(`dashboard version ${result.gui_version}`);
  }
  if (typeof result.tool_count === "number") {
    meta.push(`${result.tool_count} tools`);
  }
  if (meta.length) {
    const node = card.querySelector(".v-meta");
    node.textContent = meta.join(" - ");
    node.hidden = false;
  }
  const line = launchLine(result.command, result.args);
  if (line) {
    card.querySelector(".v-launch-label").hidden = false;
    const pre = card.querySelector(".v-launch");
    pre.textContent = line;
    pre.hidden = false;
  }
  if (result.list_databases_text) {
    const details = card.querySelector(".v-out");
    card.querySelector(".v-dbtext").textContent = result.list_databases_text;
    details.hidden = false;
  }
  return card;
}

/** Every detected client in turn, then the HTTP transport. Strictly sequential. */
async function verifyAll() {
  const results = byId("verify-results");
  const status = byId("verify-status");
  const controls = [byId("verify-all"), byId("verify-http"), byId("run-doctor")].concat(
    Array.from(document.querySelectorAll(".c-verify"))
  );
  await busy(controls, async () => {
    clear(results);
    const resp = await api("/api/summary");
    const data = resp.ok ? await readJson(resp) : null;
    const rows = data && Array.isArray(data.clients) ? data.clients : [];
    // Only clients that actually hold an entry can be verified; the rest would
    // just spawn nothing and report launch_failed.
    const targets = rows.filter((row) => row.injected && !row.unreadable);
    for (const row of targets) {
      const label = row.label || row.key;
      working(status, `verifying ${label}...`);
      results.appendChild(await verifyClient(row.key, label));
    }
    working(status, "verifying the HTTP transport...");
    results.appendChild(await verifyHttp());
    message(status, "", null);
  });
}

async function runDoctor() {
  const results = byId("doctor-results");
  const status = byId("doctor-status");
  const offline = byId("doctor-offline").checked;
  await busy([byId("run-doctor")], async () => {
    working(status, "running the checks...");
    const resp = await api("/api/doctor", {
      method: "POST",
      body: JSON.stringify({ offline }),
    });
    const payload = await readJson(resp);
    clear(results);
    if (!resp.ok || !Array.isArray(payload)) {
      message(status, errorText(payload, "doctor could not run"), "fail-text");
      return;
    }
    message(status, "", null);
    payload.forEach((row) => results.appendChild(findingCard(row)));
  });
}

function findingCard(row) {
  const card = fromTemplate("tpl-finding");
  card.querySelector(".fi-check").textContent = row.check || "check";
  const state = String(row.status || "");
  badge(card.querySelector(".fi-status"), STATUS_CLASS[state] || "skip", state || "unknown");
  card.querySelector(".fi-detail").textContent = row.detail || "";
  if (row.suggested_action && row.suggested_action !== "none") {
    const action = card.querySelector(".fi-action");
    action.textContent = `suggested: ${row.suggested_action}`;
    action.hidden = false;
  }
  return card;
}

// ---- wiring ---------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  byId("add-form").addEventListener("submit", guard(addDatabase));
  const addMode = byId("add-mode");
  addMode.addEventListener("change", () => {
    byId("add-write-note").hidden = addMode.value !== "write";
  });
  byId("verify-all").addEventListener("click", guard(verifyAll));
  byId("verify-http").addEventListener(
    "click",
    guard(async () => {
      const results = byId("verify-results");
      await busy([byId("verify-all"), byId("verify-http")], async () => {
        working(byId("verify-status"), "verifying the HTTP transport...");
        const card = await verifyHttp();
        clear(results);
        results.appendChild(card);
        message(byId("verify-status"), "", null);
      });
    })
  );
  byId("run-doctor").addEventListener("click", guard(runDoctor));

  // The only two calls made without a click: both are reads.
  renderSummary().catch(() => {});
  renderDatabases().catch(() => {});
});
