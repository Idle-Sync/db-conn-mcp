"""Tests for the background update check (issue #28).

The real network is NEVER touched: every test either replaces the module-level
fetch function or replaces ``urllib.request.urlopen`` with a fake transport.
"""

import io
import json
import threading
import time
import urllib.error
import urllib.request

from db_conn_mcp import update_check
from db_conn_mcp.update_check import start_check


def _fake_urlopen(body: bytes):
    """Return a urlopen stand-in yielding ``body`` from a context manager."""

    def opener(request, timeout=None):
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp(body)

    return opener


def _finished(check) -> None:
    """Wait for the worker to finish so the assertion is deterministic."""
    check.thread.join(timeout=5)
    assert not check.thread.is_alive()


def test_newer_version_is_reported(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "99.0.0")
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") == "99.0.0"


def test_equal_version_is_silent(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "1.2.3")
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_older_version_is_silent(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "1.2.2")
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_default_current_version_is_the_installed_one(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "0.0.1")
    check = start_check()
    _finished(check)
    assert check.newer_version() is None


def test_network_error_is_swallowed(monkeypatch):
    def _boom():
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(update_check, "_fetch_latest", _boom)
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_timeout_is_swallowed(monkeypatch):
    def _slow():
        raise TimeoutError("timed out")

    monkeypatch.setattr(update_check, "_fetch_latest", _slow)
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_slow_fetch_never_blocks_the_caller(monkeypatch):
    release = threading.Event()

    def _blocking():
        release.wait(timeout=10)
        return "99.0.0"

    monkeypatch.setattr(update_check, "_fetch_latest", _blocking)
    check = start_check()
    try:
        started = time.monotonic()
        assert check.newer_version("1.2.3") is None
        assert time.monotonic() - started < 0.5
        assert check.thread.daemon
    finally:
        release.set()
        check.thread.join(timeout=5)


def test_malformed_json_body_is_silent(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"<html>not json</html>"))
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_payload_without_a_version_is_silent(monkeypatch):
    body = json.dumps({"info": {}}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None


def test_real_fetch_path_parses_a_version(monkeypatch):
    body = json.dumps({"info": {"version": "9.9.9"}}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") == "9.9.9"


def test_non_numeric_versions_are_silent(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "1.2.3rc1")
    check = start_check()
    _finished(check)
    assert check.newer_version("1.2.3") is None
