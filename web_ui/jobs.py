"""
Generic background-job registry for the dashboard.

Extracted from the *pattern* of ``project_translate_batch`` (``web_ui/app.py``)
rather than its code: a worker thread pushing JSON events onto a queue, an SSE
route draining that queue, and — the hard-won part — a wrapper that guarantees
a terminal ``complete`` event even when the thread body raises. Without it the
browser's progress bar sits at 0% forever with nothing to explain why, which
matters more here than for translation: the grammar evaluator drives
LanguageTool through a JVM, and the ``hs_err_pid*.log`` files in the repo root
are evidence that it can take the process down with it.

Two things this adds over the batch-translate registry:

* **One live job per project.** Two concurrent runs would interleave partial
  writes to the same ``evaluations/<chunk>.json``; :func:`start_job` refuses
  the second with :class:`JobConflict` (the route turns that into a 409).
* **A single terminal event, by construction.** The job body *returns* its
  summary; this module — not the body — emits the one ``complete``.

``project_translate_batch`` deliberately stays on its own ``_batch_jobs``
registry: migrating it is risk with no benefit here.

The module has no Flask dependency so it can be unit-tested directly.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# How long a finished job's record (and any unread tail of its queue) is kept
# so a browser that reconnects late still sees the terminal event.
_RETENTION_SECONDS = 30 * 60

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

#: Signature of the callable passed to :func:`start_job`. It receives an
#: ``emit(event_name, **fields)`` function and returns the fields to attach to
#: the terminal ``complete`` event (or ``None`` for none).
JobBody = Callable[[Callable[..., None]], Optional[dict[str, Any]]]


class JobConflict(RuntimeError):
    """Raised when a project already has a job running."""

    def __init__(self, job_id: str, kind: str) -> None:
        super().__init__(f"A {kind} job ({job_id}) is already running for this project.")
        self.job_id = job_id
        self.kind = kind


def _prune_locked() -> None:
    """Drop long-finished jobs. Caller must hold ``_lock``."""
    cutoff = time.time() - _RETENTION_SECONDS
    for job_id in [
        jid for jid, job in _jobs.items()
        if job["status"] != "running" and (job.get("finished_at") or 0) < cutoff
    ]:
        _jobs.pop(job_id, None)


def active_job(project_id: str) -> Optional[str]:
    """Return the id of the project's running job, or ``None``."""
    with _lock:
        for job_id, job in _jobs.items():
            if job["project_id"] == project_id and job["status"] == "running":
                return job_id
    return None


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    """Return the raw job record, or ``None`` if unknown/expired."""
    with _lock:
        return _jobs.get(job_id)


def start_job(project_id: str, kind: str, fn: JobBody) -> str:
    """Run ``fn`` on a daemon thread and return the new job id.

    ``fn(emit)`` may emit as many progress events as it likes; the terminal
    ``complete`` is emitted here, exactly once, carrying either ``fn``'s return
    value or a ``fatal`` string if it raised.

    Raises:
        JobConflict: If a job is already running for ``project_id``.
    """
    with _lock:
        _prune_locked()
        for existing_id, job in _jobs.items():
            if job["project_id"] == project_id and job["status"] == "running":
                raise JobConflict(existing_id, job["kind"])

        job_id = uuid.uuid4().hex[:8]
        job_queue: queue.Queue = queue.Queue()
        record: dict[str, Any] = {
            "queue": job_queue,
            "status": "running",
            "kind": kind,
            "project_id": project_id,
            "started_at": time.time(),
            "finished_at": None,
            "thread": None,
        }
        _jobs[job_id] = record

    def emit(event: str, **fields: Any) -> None:
        job_queue.put({"event": event, **fields})

    def run() -> None:
        summary: dict[str, Any] = {}
        try:
            returned = fn(emit)
            # A body that returns something other than a mapping is a bug in the
            # body, not a reason to lose the terminal event.
            summary = returned if isinstance(returned, dict) else {}
        except Exception as exc:  # noqa: BLE001 - the whole point of this wrapper
            logger.exception("Job %s (%s) died: %s", job_id, kind, exc)
            summary = {"fatal": f"{kind} failed: {exc}"}
        finally:
            with _lock:
                record["status"] = "complete"
                record["finished_at"] = time.time()
            job_queue.put({"event": "complete", **summary})

    thread = threading.Thread(target=run, daemon=True, name=f"job-{kind}-{job_id}")
    record["thread"] = thread
    thread.start()
    return job_id


def stream_job(job_id: str, *, keepalive_seconds: float = 20.0):
    """Yield SSE frames for ``job_id`` until its terminal ``complete``.

    Emits a comment keepalive when the queue is quiet so proxies (and the
    browser's own idle timeout) don't drop a long deterministic run that is
    simply slow between chunks.
    """
    import json

    job = get_job(job_id)
    if job is None:
        return
    job_queue: queue.Queue = job["queue"]
    while True:
        try:
            payload = job_queue.get(timeout=keepalive_seconds)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        event = payload.get("event", "message")
        yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if event == "complete":
            break


def reset_for_tests() -> None:
    """Drop every job record. Test-only."""
    with _lock:
        _jobs.clear()


__all__ = [
    "JobConflict",
    "active_job",
    "get_job",
    "start_job",
    "stream_job",
    "reset_for_tests",
]
