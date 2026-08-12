"""A background "is there a newer release?" check that can only ever answer instantly.

Design decisions worth knowing before you change anything here (issue #28):

* **No cache file.** Every CLI command starts a fresh check. A cache would mean a
  state file to locate, version, corrupt, and clean up — for a nudge. The cost of
  going without one is a single HTTPS GET per command, run off the critical path.
* **Offline or slow means silence.** The check runs on a daemon thread and the
  caller never joins it. :meth:`UpdateCheck.newer_version` answers from whatever
  the thread has already produced, so no network condition can ever slow a
  command down or make it fail. A miss simply means no nudge this time.
* **stdlib ``urllib.request``, not ``httpx``.** This module is imported on every
  CLI invocation, so it stays import-light and dependency-free.
* **The serve path never calls this.** ``stdio`` transport speaks JSON-RPC on
  stdout, where a nudge would be corruption. Deciding *when* to check, and
  printing the result at exit, is the CLI's job (Task 2) — this module only
  answers the question.

Rule 6: nothing from the network response is ever raised, logged, or printed by
this module. The request URL is a fixed constant containing no user data, and a
failed lookup is swallowed without a message.
"""

import json
import threading
import urllib.request

from . import __version__

#: The package's PyPI JSON endpoint. A fixed URL — it embeds no user data.
PYPI_JSON_URL = "https://pypi.org/pypi/db-conn-mcp/json"

#: Kept short: a nudge is worthless if it outlives the command it belongs to.
_TIMEOUT_SECONDS = 3.0


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse '0.5.2' -> (0, 5, 2); None for anything non-numeric (pre-releases).

    Shared with :mod:`db_conn_mcp.doctor` so the two PyPI lookups can never
    disagree about which of two versions is newer.
    """
    parts = version.split(".")
    if parts and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return None


def is_newer(candidate: str, current: str) -> bool:
    """True only if ``candidate`` is strictly newer than ``current``.

    Unparseable versions on either side are treated as "not newer": a release
    train we cannot compare is not one we should nag about.
    """
    remote, installed = _version_tuple(candidate), _version_tuple(current)
    if remote is None or installed is None:
        return False
    return remote > installed


def _fetch_latest() -> str | None:
    """One blocking HTTPS GET for the latest published version, or None.

    Raises whatever ``urllib``/``json`` raise — the worker thread swallows it.
    Monkeypatch this function in tests; never let a test touch the real network.
    """
    request = urllib.request.Request(
        PYPI_JSON_URL, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        version = json.load(response)["info"]["version"]
    return version if isinstance(version, str) and version else None


class UpdateCheck:
    """A lookup already running on a daemon thread, plus the slot it writes into.

    Construct via :func:`start_check`. The only question you may ask is
    :meth:`newer_version`, and it answers immediately or not at all.
    """

    def __init__(self) -> None:
        self._latest: str | None = None
        #: Public so callers (and tests) *can* join deliberately. Nothing in the
        #: normal path does — :meth:`newer_version` never waits on it.
        self.thread = threading.Thread(
            target=self._run, name="db-conn-mcp-update-check", daemon=True
        )

    def _run(self) -> None:
        """Worker body: fetch, or leave the result unset. Never raises, never logs."""
        try:
            self._latest = _fetch_latest()
        except Exception:  # noqa: BLE001 — no internet is not an error worth reporting
            self._latest = None

    def newer_version(self, current: str | None = None) -> str | None:
        """The newer version string, or None — without ever blocking.

        Returns a version only when the worker has already finished, parsed a
        version, and that version is strictly newer than ``current`` (defaulting
        to the installed :data:`db_conn_mcp.__version__`). A thread still in
        flight, a failed lookup, or an equal/older release all return None.
        """
        if self.thread.is_alive():
            return None
        latest = self._latest
        if latest is None:
            return None
        return latest if is_newer(latest, current or __version__) else None


def start_check() -> UpdateCheck:
    """Kick off the lookup now and hand back the handle to ask later."""
    check = UpdateCheck()
    check.thread.start()
    return check
