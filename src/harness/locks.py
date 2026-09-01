"""Cross-process locks for a book's ``.harness/`` working directory.

Nothing in this repo locked across processes before this module. Every wave type
follows the same destructive shape — ``prepare`` renders prompts and rewrites
``manifest.json``, ``fanout`` fills the drafts, ``commit`` reads them — and
``prepare`` unlinks the drafts of the entries it re-renders. Two processes on one
book therefore corrupt each other: a CLI or scheduled wave has no Flask job
record, so ``jobs.start_job`` raises no conflict and a click in the dashboard
re-prepares underneath a run that is still fanning out.

``threading.Lock`` cannot help — the two writers are different *processes* (the
always-on reader service and a scheduled task), often started minutes apart.

The lock is a file, created with ``O_CREAT | O_EXCL``, which is atomic on
Windows as well as POSIX and needs neither ``fcntl`` nor ``msvcrt``. Its body
names who holds it, so a blocked caller can say something useful instead of
"busy", and a lock left behind by a crash can be told from a live one:

* **Same host, dead PID** → break it. The process that wrote it is gone.
* **Same host, live PID** → hold. Someone really is working.
* **Different host** → PID liveness is meaningless, so only the age ceiling
  applies.
* **Older than ``stale_after``** (default 3 h — above the reader's own per-job
  ceiling of 30 min, below a night) → break it either way, because a wave that
  has been "running" that long is not coming back.

Breaking is itself done through the same atomic create, so two processes racing
to break one stale lock cannot both win.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from src.harness import state as hstate

_log = logging.getLogger(__name__)

# A lock older than this is assumed abandoned. Above the longest per-job ceiling
# in ``headless._CLI_JOB_TIMEOUT_S`` (claude, 30 min) times a few jobs, and well
# below the gap between two nightly runs.
DEFAULT_STALE_AFTER_S = 3 * 60 * 60

# How often a waiting caller re-checks. Waves last minutes; sub-second polling
# would only burn CPU.
_POLL_INTERVAL_S = 0.5

# A body that will not parse is normally the signature of a process killed
# mid-write — but it is also what our *own* creation looks like for the instant
# between `O_EXCL` and the write, so a racing acquirer must not read that
# instant as an abandoned lock and break it. Inside this window an unparseable
# body is treated as fresh; past it, the writer really did die.
_EMPTY_BODY_GRACE_S = 5.0

# A release can lose to a sharing violation on Windows while any reader has the
# body open (`read_holder` runs on every dashboard render). Giving up on the
# first failure leaks the lock until the staleness ceiling, so retry briefly.
_RELEASE_ATTEMPTS = 4
_RELEASE_BACKOFF_S = 0.05

LOCK_FILENAME = ".lock"


class LockBusy(RuntimeError):
    """Raised when a lock is held by someone else and the wait ran out.

    Carries the holder's recorded body (``pid`` / ``host`` / ``kind`` /
    ``run_id`` / ``started_at``) so callers can name who is in the way. The
    dashboard turns this into the same 409 shape ``jobs.JobConflict`` produces.
    """

    def __init__(self, path: Path, holder: Optional[dict[str, Any]]) -> None:
        holder = holder or {}
        who = holder.get("kind") or "another process"
        pid = holder.get("pid")
        host = holder.get("host")
        where = f" (pid {pid} on {host})" if pid else ""
        started = holder.get("started_at")
        since = f", started {started}" if started else ""
        super().__init__(f"A {who} run holds {path}{where}{since}.")
        self.path = path
        self.holder = dict(holder)

    @property
    def kind(self) -> str:
        """The ``kind`` the holder registered, or ``"unknown"``."""
        return str(self.holder.get("kind") or "unknown")

    @property
    def run_id(self) -> Optional[str]:
        return self.holder.get("run_id")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def lock_path(project_dir: Path | str) -> Path:
    """``projects/<slug>/.harness/.lock`` — one lock per book.

    Per book rather than per wave type on purpose: ``prepare`` for judges and
    ``prepare`` for annotations write to different subdirectories, but both
    re-resolve the book's config, and a nightly pass that ran annotations while
    the dashboard ran judges would report two sets of usage against one book with
    no way to tell them apart. One lock is also the only granularity a *reader*
    clicking a button can reason about.
    """
    return hstate.harness_dir(Path(project_dir)) / LOCK_FILENAME


def repo_lock_path() -> Path:
    """``logs/.nightly.lock`` — the whole-driver lock."""
    return hstate.REPO_ROOT / "logs" / ".nightly.lock"


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True when a process with this id exists on *this* machine.

    Never ``os.kill(pid, 0)``: on Windows CPython implements ``os.kill`` with
    ``TerminateProcess(handle, sig)`` for any signal that is not a console
    control event, so the POSIX "signal 0 is a liveness probe" idiom would
    *terminate* the very process it asks about.

    Fails open — an unanswerable question returns ``True`` (assume alive) so an
    unreadable liveness result never breaks a lock somebody is holding.
    """
    if pid <= 0:
        return False
    try:
        import psutil  # noqa: PLC0415 - optional, not in requirements.txt
    except ImportError:
        pass
    else:
        try:
            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001 - fall through to the stdlib probes
            _log.debug("psutil.pid_exists(%s) failed", pid, exc_info=True)

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # ERROR_INVALID_PARAMETER is "no such process"; anything else
                # (typically ERROR_ACCESS_DENIED) means it exists but is not
                # ours to inspect, which is still alive.
                return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - fail open
            _log.debug("win32 liveness probe failed for pid %s", pid, exc_info=True)
            return True

    try:
        os.kill(pid, 0)          # POSIX only: here signal 0 really is a probe
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # exists, owned by someone else
    except OSError:
        return True
    return True


def _age_seconds(holder: dict[str, Any]) -> Optional[float]:
    """Seconds since ``started_at``, or ``None`` when it cannot be parsed."""
    raw = holder.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Compare in the body's own frame. A hand-edited lock, or one written by a
    # version that moves to `datetime.now(timezone.utc)`, is offset-aware, and
    # subtracting it from a naive `now` raises TypeError out of every acquire.
    now = datetime.now(started.tzinfo) if started.tzinfo is not None else datetime.now()
    return (now - started).total_seconds()


def _body_written_within_grace(path: Optional[Path]) -> bool:
    """True when ``path`` was written inside ``_EMPTY_BODY_GRACE_S``.

    Only meaningful for an unparseable body: it separates "a writer is between
    its create and its write, right now" from "a process died mid-write".
    """
    if path is None:
        return False
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < _EMPTY_BODY_GRACE_S


def is_stale(
    holder: Optional[dict[str, Any]],
    *,
    stale_after: float,
    path: Optional[Path] = None,
) -> bool:
    """True when this lock body may be broken.

    A body that is missing or unparseable is stale — an empty or truncated
    lockfile is the signature of a process killed mid-write, and honouring it
    forever would wedge the book. The exception is one just written: pass
    ``path`` and a body younger than ``_EMPTY_BODY_GRACE_S`` is left alone, so a
    racing acquirer cannot break a lock the owner is still in the act of taking.
    """
    if not holder:
        return not _body_written_within_grace(path)

    age = _age_seconds(holder)
    if age is not None and age > stale_after:
        return True

    pid = holder.get("pid")
    host = holder.get("host")
    if not isinstance(pid, int):
        return True
    if host and host != socket.gethostname():
        # Another machine's PID numbers say nothing about ours; only age can
        # retire this lock. (Nothing shares a projects/ dir today, but a synced
        # folder would, and silently breaking a live remote lock is worse than
        # waiting.)
        return False
    return not _pid_alive(pid)


# ---------------------------------------------------------------------------
# Read / acquire / release
# ---------------------------------------------------------------------------


def read_holder(path: Path | str) -> Optional[dict[str, Any]]:
    """The lock body, or ``None`` when the file is absent.

    An unreadable or malformed file returns ``{}`` rather than ``None``: it
    exists (so the lock is taken) but names nobody (so it is stale).
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return doc if isinstance(doc, dict) else {}


def holder_of(
    project_dir: Path | str, *, stale_after: float = DEFAULT_STALE_AFTER_S
) -> Optional[dict[str, Any]]:
    """Who currently holds ``project_dir``'s lock, or ``None`` if it is free.

    A stale lock reads as free — the point is to answer "would an acquire
    succeed right now?", which is what the dashboard needs before it offers a
    destructive button. Read-only: it never breaks anything.
    """
    path = lock_path(project_dir)
    holder = read_holder(path)
    if holder is None:
        return None
    if is_stale(holder, stale_after=stale_after, path=path):
        return None
    return holder


def held_by_this_process(holder: Optional[dict[str, Any]]) -> bool:
    """True when this very process wrote that lock body.

    The dashboard needs it: a lock its own job body took is already reported by
    ``jobs.JobConflict``, which knows the job id a lock body does not carry. The
    cross-process check is for the waves ``jobs`` cannot see.
    """
    if not holder:
        return False
    return (
        holder.get("pid") == os.getpid()
        and holder.get("host") == socket.gethostname()
    )


def _write_lock(path: Path, body: dict[str, Any]) -> bool:
    """Atomically create ``path`` with ``body``. False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialise before the create, so the file goes from absent to complete in
    # one `os.write` rather than existing empty for the length of a json.dump.
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except Exception:
        # A body we could not finish writing is worse than no lock: it would
        # read as "held by nobody" forever. Take it back down.
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return True


def _release(path: Path, token: str) -> None:
    """Remove the lock, but only if it is still ours.

    ``token`` is the unique ``run_id``+``pid`` pair we wrote. If a stale-break
    handed the lock to someone else while we were still running (we overran the
    ceiling), unlinking would free *their* lock — so check first. Best-effort:
    a release failure must never mask the body's own exception.
    """
    for attempt in range(_RELEASE_ATTEMPTS):
        try:
            holder = read_holder(path)
            if holder is None or holder.get("_token") != token:
                return
            path.unlink(missing_ok=True)
            return
        except OSError:
            if attempt + 1 >= _RELEASE_ATTEMPTS:
                _log.warning("could not release lock %s", path, exc_info=True)
                return
            time.sleep(_RELEASE_BACKOFF_S * (attempt + 1))


def _wait_to_retry(deadline: float) -> bool:
    """Sleep one poll interval. False when ``deadline`` has already passed.

    The single throttle for every retry path in ``_acquire``, so no branch can
    loop unbounded or un-slept.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(_POLL_INTERVAL_S, remaining))
    return True


def _acquire(
    path: Path,
    *,
    kind: str,
    run_id: Optional[str],
    timeout: float,
    stale_after: float,
) -> str:
    """Take ``path``, waiting up to ``timeout`` seconds. Returns our token."""
    token = f"{os.getpid()}:{run_id or ''}:{time.time_ns()}"
    body = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "kind": kind,
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "_token": token,
    }
    deadline = time.monotonic() + max(0.0, timeout)

    while True:
        if _write_lock(path, body):
            return token

        holder = read_holder(path)
        if holder is None:
            # Released between the two calls. Retry — but through the same
            # bounded wait as every other path, or a file that keeps failing to
            # unlink (a Windows sharing violation) spins a core forever.
            if not _wait_to_retry(deadline):
                raise LockBusy(path, {})
            continue
        if is_stale(holder, stale_after=stale_after, path=path):
            _log.warning(
                "breaking stale lock %s held by pid=%s kind=%s since %s",
                path, holder.get("pid"), holder.get("kind"), holder.get("started_at"),
            )
            # Unlink then re-create: whoever wins the O_EXCL race owns it, so two
            # processes breaking the same stale lock cannot both proceed.
            with contextlib.suppress(OSError):
                path.unlink()
            if _write_lock(path, body):
                return token
            # Lost the race to break it, or the unlink failed. Either way, wait.
            if not _wait_to_retry(deadline):
                raise LockBusy(path, holder)
            continue

        if not _wait_to_retry(deadline):
            raise LockBusy(path, holder)


@contextlib.contextmanager
def project_lock(
    project_dir: Path | str,
    *,
    kind: str,
    run_id: Optional[str] = None,
    timeout: float = 0.0,
    stale_after: float = DEFAULT_STALE_AFTER_S,
) -> Iterator[Path]:
    """Hold one book's ``.harness/`` for the duration of the block.

    Args:
        project_dir: ``projects/<slug>/``.
        kind: What the holder is doing (``"annotations"``, ``"judges"``,
            ``"review-coded"``, …). Purely descriptive — it appears in the
            message a blocked caller shows — but make it recognisable.
        run_id: The run this belongs to, when there is one.
        timeout: Seconds to wait. ``0`` (the default) fails immediately, which
            is what an HTTP route wants; the nightly driver passes a real wait.
        stale_after: Age ceiling before a lock is assumed abandoned.

    Yields:
        The lockfile path.

    Raises:
        LockBusy: Someone else holds it and ``timeout`` elapsed.
    """
    path = lock_path(project_dir)
    token = _acquire(
        path, kind=kind, run_id=run_id, timeout=timeout, stale_after=stale_after
    )
    try:
        yield path
    finally:
        _release(path, token)


@contextlib.contextmanager
def repo_lock(
    *,
    kind: str = "nightly",
    run_id: Optional[str] = None,
    timeout: float = 0.0,
    stale_after: float = DEFAULT_STALE_AFTER_S,
) -> Iterator[Path]:
    """Hold the whole-repo driver lock, so two passes cannot overlap.

    The per-book locks stop two processes fighting over one book; this stops a
    hand-run ``daily_pass.py`` and the scheduled task from interleaving over the
    *set* of books, where each would see the other's finished books as already
    done and its in-flight one as busy.
    """
    path = repo_lock_path()
    token = _acquire(
        path, kind=kind, run_id=run_id, timeout=timeout, stale_after=stale_after
    )
    try:
        yield path
    finally:
        _release(path, token)
