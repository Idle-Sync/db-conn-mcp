"""Tests for the bounded retry of ``os.replace`` on Windows error 5 (issue #63).

Every test runs on every OS: a fake ``os.replace`` raises the exact exception Windows
raises, so the retry's bound and its refusals are checked without a Windows runner.
"""

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from db_conn_mcp import atomic_write
from db_conn_mcp.atomic_write import write_text_atomically


def _access_denied(source: str, destination: Path) -> PermissionError:
    """Build the exception a refused swap raises on Windows, on any OS."""
    exc = PermissionError(13, "Access is denied", source, None, str(destination))
    exc.winerror = 5  # the five-argument form does not set it on POSIX
    return exc


def _access_denied_with_winerror_2(source: str, destination: Path) -> PermissionError:
    exc = PermissionError(13, "Access is denied", source, None, str(destination))
    exc.winerror = 2
    return exc


def _access_denied_on_posix(source: str, destination: Path) -> PermissionError:
    return PermissionError(13, "Permission denied", source, None, str(destination))


def _file_not_found(source: str, destination: Path) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, "No such file", source, None, str(destination))


def _file_not_found_with_winerror_5(source: str, destination: Path) -> FileNotFoundError:
    """The one combination that tells the ``errno`` check from the ``winerror`` check."""
    exc = _file_not_found(source, destination)
    exc.winerror = 5
    return exc


def _refuse_replace_three_times(monkeypatch) -> list[str]:
    """Make ``os.replace`` raise error 5 three times, then work; returns one entry per call."""
    real_replace = os.replace
    calls: list[str] = []

    def fake_replace(source: str, destination: Path) -> None:
        calls.append(source)
        if len(calls) <= 3:
            raise _access_denied(source, destination)
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write.os, "replace", fake_replace)
    return calls


def _refuse_replace_always(
    monkeypatch, make_error: Callable[[str, Path], OSError] = _access_denied
) -> list[OSError]:
    """Make every ``os.replace`` raise ``make_error``; returns each exception raised."""
    raised: list[OSError] = []

    def fake_replace(source: str, destination: Path) -> None:
        raised.append(make_error(source, destination))
        raise raised[-1]

    monkeypatch.setattr(atomic_write.os, "replace", fake_replace)
    return raised


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record each ``time.sleep``; call 21 fails, so a loop with no bound cannot spin."""
    calls: list[float] = []

    def recorder(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) > 20:
            raise AssertionError("sleep called more than 20 times")

    monkeypatch.setattr(time, "sleep", recorder)
    return calls


def test_refused_replace_is_retried_over_an_existing_target(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "connections.json"
    target.write_text("old", encoding="utf-8")
    calls = _refuse_replace_three_times(monkeypatch)
    write_text_atomically(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]
    assert len(calls) == 4
    assert sleeps == [0.05, 0.05, 0.05]


def test_refused_replace_is_retried_when_the_target_does_not_exist(tmp_path, monkeypatch, sleeps):
    """A first save has no target; ``os.access`` is False for it, yet it is not read-only."""
    target = tmp_path / "connections.json"
    assert list(tmp_path.iterdir()) == []
    calls = _refuse_replace_three_times(monkeypatch)
    write_text_atomically(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]
    assert len(calls) == 4
    assert sleeps == [0.05, 0.05, 0.05]


def test_replace_refused_on_every_attempt_gives_up_with_the_last_error(
    tmp_path, monkeypatch, sleeps
):
    """The bound is what turns a stuck file into an error the caller can report."""
    target = tmp_path / "connections.json"
    target.write_bytes(b"old")
    raised = _refuse_replace_always(monkeypatch)
    with pytest.raises(PermissionError) as excinfo:
        write_text_atomically(target, "new")
    assert excinfo.value is raised[-1]
    assert len(raised) == 20
    assert sleeps == [0.05] * 19
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_read_only_target_fails_at_once(tmp_path, monkeypatch, sleeps):
    """A read-only target refuses every attempt, so waiting would only delay the error."""
    target = tmp_path / "connections.json"
    target.write_bytes(b"old")
    raised = _refuse_replace_always(monkeypatch)
    monkeypatch.setattr(atomic_write.os, "access", lambda path, mode: False)
    with pytest.raises(PermissionError) as excinfo:
        write_text_atomically(target, "new")
    assert excinfo.value is raised[0]
    assert len(raised) == 1
    assert sleeps == []
    assert list(tmp_path.iterdir()) == [target]


def test_read_only_check_runs_once_before_the_first_replace(tmp_path, monkeypatch, sleeps):
    """``os.access`` is also False while the attributes can't be read, the state being retried."""
    real_replace = os.replace
    target = tmp_path / "connections.json"
    target.write_bytes(b"old")
    events: list[object] = []

    def fake_access(path: Path, mode: int) -> bool:
        events.append((path, mode))
        return len(events) == 1  # writable when first asked, unreadable from then on

    def fake_replace(source: str, destination: Path) -> None:
        events.append("replace")
        if events.count("replace") <= 3:
            raise _access_denied(source, destination)
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write.os, "access", fake_access)
    monkeypatch.setattr(atomic_write.os, "replace", fake_replace)
    write_text_atomically(target, "new")
    assert events == [(target.resolve(), os.W_OK), "replace", "replace", "replace", "replace"]
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    "make_error",
    [
        _access_denied_with_winerror_2,
        _access_denied_on_posix,
        _file_not_found,
        _file_not_found_with_winerror_5,
    ],
)
def test_other_errors_are_not_retried(tmp_path, monkeypatch, sleeps, make_error):
    target = tmp_path / "connections.json"
    target.write_bytes(b"old")
    raised = _refuse_replace_always(monkeypatch, make_error)
    with pytest.raises(OSError) as excinfo:
        write_text_atomically(target, "new")
    assert excinfo.value is raised[0]
    assert len(raised) == 1
    assert sleeps == []
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_refused_copymode_is_not_retried(tmp_path, monkeypatch, sleeps):
    """Only the swap can be refused for a moment; nothing else in the save is retried."""
    target = tmp_path / "connections.json"
    target.write_bytes(b"old")
    copies: list[str] = []

    def fake_copymode(source: Path, destination: str) -> None:
        copies.append(destination)
        raise _access_denied(destination, source)

    monkeypatch.setattr(atomic_write.shutil, "copymode", fake_copymode)
    raised = _refuse_replace_always(monkeypatch)
    with pytest.raises(PermissionError):
        write_text_atomically(target, "new")
    assert len(copies) == 1
    assert raised == []
    assert sleeps == []
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
