"""Cross-process project locks.

The lock exists to stop the always-on dashboard and the nightly pass from
re-``prepare``-ing on top of each other's in-flight waves, so what these tests
pin is the failure surface rather than the happy path: contention, breaking a
lock whose owner is gone, *not* breaking one whose owner is alive, and the two
ways a lock body can be unreadable.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.harness import locks


def _age(path, seconds):
    """Backdate ``path``'s mtime, so the empty-body grace window has passed."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.fixture
def book(tmp_path):
    d = tmp_path / "testbook"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------


def test_lock_file_lands_in_the_harness_dir(book):
    with locks.project_lock(book, kind="annotations", run_id="r1") as path:
        assert path == book / ".harness" / ".lock"
        assert path.exists()
    assert not path.exists()


def test_body_names_the_holder(book):
    with locks.project_lock(book, kind="annotations", run_id="r1"):
        body = json.loads(locks.lock_path(book).read_text(encoding="utf-8"))
    assert body["pid"] == os.getpid()
    assert body["host"] == socket.gethostname()
    assert body["kind"] == "annotations"
    assert body["run_id"] == "r1"
    datetime.fromisoformat(body["started_at"])  # parseable


def test_released_even_when_the_body_raises(book):
    with pytest.raises(ValueError):
        with locks.project_lock(book, kind="annotations"):
            raise ValueError("boom")
    assert not locks.lock_path(book).exists()


def test_holder_of_is_none_when_free(book):
    assert locks.holder_of(book) is None


def test_holder_of_reports_a_live_holder(book):
    with locks.project_lock(book, kind="judges", run_id="r9"):
        holder = locks.holder_of(book)
    assert holder is not None
    assert holder["kind"] == "judges"
    assert holder["run_id"] == "r9"


# ---------------------------------------------------------------------------
# Contention
# ---------------------------------------------------------------------------


def test_second_acquire_fails_immediately(book):
    with locks.project_lock(book, kind="annotations", run_id="first"):
        with pytest.raises(locks.LockBusy) as excinfo:
            with locks.project_lock(book, kind="judges"):
                pass
    assert excinfo.value.kind == "annotations"
    assert excinfo.value.run_id == "first"
    assert "annotations" in str(excinfo.value)


def test_timeout_waits_then_succeeds(book):
    """A waiter gets the lock as soon as the holder lets go."""
    released = threading.Event()

    def hold():
        with locks.project_lock(book, kind="annotations"):
            time.sleep(0.6)
        released.set()

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.1)                      # let the holder win the race
    with locks.project_lock(book, kind="nightly", timeout=10.0):
        assert released.is_set()
    thread.join()


def test_timeout_gives_up(book):
    with locks.project_lock(book, kind="annotations"):
        started = time.monotonic()
        with pytest.raises(locks.LockBusy):
            with locks.project_lock(book, kind="nightly", timeout=0.6):
                pass
        assert time.monotonic() - started >= 0.5


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def _plant(book, **body):
    path = locks.lock_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_breaks_a_lock_whose_pid_is_gone(book):
    _plant(
        book, pid=999_999, host=socket.gethostname(), kind="annotations",
        run_id="dead", started_at=datetime.now().isoformat(),
    )
    with locks.project_lock(book, kind="nightly", run_id="live"):
        body = json.loads(locks.lock_path(book).read_text(encoding="utf-8"))
    assert body["run_id"] == "live"


def test_does_not_break_a_lock_whose_pid_is_alive(book):
    _plant(
        book, pid=os.getpid(), host=socket.gethostname(), kind="annotations",
        run_id="alive", started_at=datetime.now().isoformat(),
    )
    with pytest.raises(locks.LockBusy):
        with locks.project_lock(book, kind="nightly"):
            pass
    locks.lock_path(book).unlink()


def test_breaks_a_lock_older_than_the_ceiling(book):
    """Even a live PID loses the lock once it has held it past the ceiling."""
    old = (datetime.now() - timedelta(hours=4)).isoformat()
    _plant(
        book, pid=os.getpid(), host=socket.gethostname(), kind="annotations",
        run_id="ancient", started_at=old,
    )
    with locks.project_lock(book, kind="nightly", run_id="fresh"):
        body = json.loads(locks.lock_path(book).read_text(encoding="utf-8"))
    assert body["run_id"] == "fresh"


def test_another_hosts_pid_is_never_probed(book):
    """PID numbers mean nothing across machines, so only age can retire it."""
    _plant(
        book, pid=999_999, host="some-other-machine", kind="annotations",
        run_id="remote", started_at=datetime.now().isoformat(),
    )
    with pytest.raises(locks.LockBusy):
        with locks.project_lock(book, kind="nightly"):
            pass
    locks.lock_path(book).unlink()


@pytest.mark.parametrize("content", ["", "not json", "[]"])
def test_an_unreadable_body_is_stale_once_it_is_past_the_grace(book, content):
    """A truncated lockfile is a killed process, not a permanent claim."""
    path = locks.lock_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _age(path, locks._EMPTY_BODY_GRACE_S + 1)
    with locks.project_lock(book, kind="nightly", run_id="fresh"):
        body = json.loads(path.read_text(encoding="utf-8"))
    assert body["run_id"] == "fresh"


@pytest.mark.parametrize("content", ["", "not json", "[]"])
def test_an_unreadable_body_just_written_is_not_broken(book, content):
    """The window between a holder's O_EXCL create and its write is not a stale lock.

    Without this, two processes acquiring the same book milliseconds apart both
    end up owning it: the second reads the first's still-empty body, calls it
    abandoned, and breaks it.
    """
    path = locks.lock_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(locks.LockBusy):
        with locks.project_lock(book, kind="nightly", run_id="racer"):
            pass
    assert path.read_text(encoding="utf-8") == content


def test_a_release_retries_an_unlink_that_loses_a_race(book, monkeypatch):
    """On Windows a delete fails while any reader has the body open.

    `read_holder` runs on every dashboard render and every `pending_work scan`,
    so a release colliding with one is ordinary. Giving up on the first failure
    leaks a lock whose pid is still alive — no staleness check retires it, and
    the book is 409ing for three hours.
    """
    real_unlink = Path.unlink
    calls = {"n": 0}

    def _flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(32, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky)
    with locks.project_lock(book, kind="annotations", run_id="r1") as path:
        pass

    assert calls["n"] == 3
    assert not path.exists()


def test_a_release_that_cannot_win_gives_up_quietly(book, monkeypatch):
    """A release failure must never mask the body's own exception."""
    def _always_fails(self, *args, **kwargs):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(Path, "unlink", _always_fails)
    with locks.project_lock(book, kind="annotations", run_id="r1"):
        pass  # exits without raising


@pytest.mark.parametrize("tz", [timezone.utc, timezone(timedelta(hours=-4))])
def test_a_tz_aware_started_at_is_aged_not_raised(tz):
    """Subtracting an aware datetime from a naive `now` is a TypeError.

    Nothing catches it, so it escapes `_acquire`, `holder_of` and the
    dashboard's conflict check — one hand-edited lock file 500s every route for
    that book until somebody deletes it. Age is compared in the body's own
    frame, so an offset does not make a fresh lock look ancient either.
    """
    def _holder(when):
        return {
            "pid": os.getpid(), "host": socket.gethostname(),
            "started_at": when.isoformat(),
        }

    fresh = _holder(datetime.now(tz))
    old = _holder(datetime.now(tz) - timedelta(hours=4))

    assert locks.is_stale(fresh, stale_after=locks.DEFAULT_STALE_AFTER_S) is False
    assert locks.is_stale(old, stale_after=locks.DEFAULT_STALE_AFTER_S) is True


def test_holder_of_ignores_a_stale_lock(book):
    _plant(
        book, pid=999_999, host=socket.gethostname(), kind="annotations",
        run_id="dead", started_at=datetime.now().isoformat(),
    )
    assert locks.holder_of(book) is None


# ---------------------------------------------------------------------------
# O_EXCL and release ownership
# ---------------------------------------------------------------------------


def test_only_one_of_many_threads_wins(book):
    """The atomic create is the whole mutual-exclusion mechanism."""
    winners: list[str] = []
    barrier = threading.Barrier(8)

    def contend(index):
        barrier.wait()
        try:
            with locks.project_lock(book, kind="annotations", run_id=str(index)):
                winners.append(str(index))
                time.sleep(0.05)
        except locks.LockBusy:
            pass

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert not locks.lock_path(book).exists()


def test_release_leaves_someone_elses_lock_alone(book):
    """A run that overran the ceiling must not free the lock that replaced it."""
    path = locks.lock_path(book)
    with locks.project_lock(book, kind="annotations", run_id="mine"):
        # Simulate another process having broken and retaken this lock.
        path.write_text(
            json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(),
                "kind": "nightly", "run_id": "theirs",
                "started_at": datetime.now().isoformat(), "_token": "theirs",
            }),
            encoding="utf-8",
        )
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "theirs"


# ---------------------------------------------------------------------------
# Repo lock
# ---------------------------------------------------------------------------


def test_repo_lock_is_exclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(locks, "repo_lock_path", lambda: tmp_path / ".nightly.lock")
    with locks.repo_lock(kind="nightly", run_id="a"):
        with pytest.raises(locks.LockBusy):
            with locks.repo_lock(kind="nightly", run_id="b"):
                pass


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


def test_pid_alive_says_yes_to_this_process():
    assert locks._pid_alive(os.getpid()) is True


def test_pid_alive_says_no_to_a_free_pid():
    assert locks._pid_alive(999_999) is False


@pytest.mark.parametrize("pid", [0, -1])
def test_pid_alive_rejects_nonsense(pid):
    assert locks._pid_alive(pid) is False


@pytest.mark.skipif(os.name != "nt", reason="POSIX signal 0 really is a probe")
def test_windows_probe_never_calls_os_kill(monkeypatch):
    """On Windows ``os.kill(pid, 0)`` *terminates* the process it asks about.

    CPython implements ``os.kill`` there with ``TerminateProcess(handle, sig)``
    for any signal that is not a console control event, so the POSIX idiom would
    kill a live wave to find out whether it was live. psutil is hidden here so
    the fallback ctypes path is the one under test, not the shortcut.
    """
    import builtins

    def explode(*args, **kwargs):
        raise AssertionError("os.kill must never be used as a liveness probe")

    real_import = builtins.__import__

    def no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("hidden for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    monkeypatch.setattr(os, "kill", explode)
    assert locks._pid_alive(os.getpid()) is True
    assert locks._pid_alive(999_999) is False
