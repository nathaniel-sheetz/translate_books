"""Append-only run log for the translate-harness — one JSONL line per event.

Where ``prompt_logger`` records each *LLM call*, this records each *harness run*:
the timeline of CLI commands (durations, outcomes, key result counts) plus the
conversational "beats" the CLI can't see on its own (approve-vs-reject, spawn
mode, worker re-spawns), all tied together by a per-run ``run_id`` minted at
``setup``. The goal is a lightweight record of how a run went, expandable later
into a summary report without changing call sites.

Every event is a single JSON object on its own line in ``logs/harness_runs.jsonl``
at the repo root, carrying ``ts`` / ``run_id`` / ``project`` / ``event`` plus any
extra fields. Writes are best-effort: a logging failure must never break a
harness command (same stance as ``prompt_logger`` and ``_write_output_artifact``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# Resolve once — works regardless of cwd (src/utils/run_logger.py -> parents[2] = repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNS_PATH = _REPO_ROOT / "logs" / "harness_runs.jsonl"


def log_run_event(
    *,
    run_id: str | None,
    project: str | None,
    event: str,
    **fields,
) -> None:
    """Append one event to ``logs/harness_runs.jsonl``. Best-effort; never raises.

    Parameters
    ----------
    run_id : str | None
        The run this event belongs to (minted at ``setup``); ``None`` if it
        could not be resolved.
    project : str | None
        The project slug/id the command targeted.
    event : str
        Event kind — ``"command"`` for the automatic per-command timeline, or a
        beat name (``"approval"`` / ``"spawn_mode"`` / ``"backend"`` /
        ``"respawn"`` / …) written by the agent via ``harness.py log-event``.
    **fields
        Any additional JSON-serializable metadata (e.g. ``cmd``, ``status``,
        ``dur_s``, ``counts``).
    """
    record = {
        **fields,  # user-supplied fields first so fixed keys always win
        "ts": datetime.now().isoformat(),
        "run_id": run_id,
        "project": project,
        "event": event,
    }
    try:
        _RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _RUNS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break a command
        _log.warning("Failed to write run log to %s", _RUNS_PATH, exc_info=True)
