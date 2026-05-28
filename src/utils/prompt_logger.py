"""
Log every LLM prompt and response to prompts/history/ for auditing,
debugging, and future batch-replay.

Each completed LLM call produces a single JSON file with metadata, the
full prompt, and the full response. Batch submissions write the file at
submission time with response=None and update_log_response() fills in
the response when retrieval completes — so the on-disk shape is uniform
across batch and realtime.
"""

import contextvars
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve once — works regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HISTORY_DIR = _REPO_ROOT / "prompts" / "history"

# Path of the most recent log_prompt() write in this execution context.
# Callers downstream of an LLM dispatch can read this to attach provenance
# without threading the path through every layer of the call stack.
_LAST_LOG_PATH: contextvars.ContextVar["Path | None"] = contextvars.ContextVar(
    "prompt_logger_last_path", default=None
)


def last_log_path() -> "Path | None":
    """Return the absolute path of the most recent log_prompt() write in
    this context, or None if no log has been written yet here."""
    return _LAST_LOG_PATH.get()


def relative_log_path(path: Path) -> str:
    """Convert an absolute prompts/history path to a repo-relative POSIX
    string suitable for storage in chunk JSON."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _ensure_history_dir() -> Path:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return _HISTORY_DIR


def _short_hash(text: str, length: int = 6) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length]


def log_prompt(
    *,
    prompt: str,
    response: str | None,
    provider: str,
    model: str,
    call_type: str = "unknown",
    mode: str = "realtime",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    duration_seconds: float | None = None,
    batch_job_id: str | None = None,
    chunk_id: str | None = None,
    project_slug: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a prompt/response log file and return its path.

    Parameters
    ----------
    prompt : str
        The full prompt sent to the LLM.
    response : str | None
        The full response text, or None for batch submissions
        whose results haven't arrived yet.
    provider / model : str
        Which provider and model were used.
    call_type : str
        One of "translation", "glossary", "style_questions",
        "style_guide_generate", or "unknown".
    mode : str
        "realtime" or "batch".
    temperature / max_tokens : float / int
        Generation parameters.
    duration_seconds : float | None
        Wall-clock time for the API call (realtime only).
    batch_job_id : str | None
        The batch job ID (batch mode only).
    chunk_id : str | None
        The chunk ID (translation calls only).
    project_slug : str | None
        The project slug. Logs that carry this can be unambiguously
        attributed to a project even when chunk_id collides across projects.
    extra : dict | None
        Any additional metadata to store.
    """
    now = datetime.now()
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    # Include chunk_id and batch_job_id in hash to avoid collisions
    # (e.g. batch retrieval logs share the same placeholder prompt)
    hash_input = prompt + (chunk_id or "") + (batch_job_id or "")
    short = _short_hash(hash_input)
    filename = f"{timestamp_str}_{call_type}_{short}.json"

    record = {
        "metadata": {
            "timestamp": now.isoformat(),
            "provider": provider,
            "model": model,
            "call_type": call_type,
            "mode": mode,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        "prompt": prompt,
        "response": response,
    }

    if duration_seconds is not None:
        record["metadata"]["duration_seconds"] = round(duration_seconds, 3)
    if batch_job_id is not None:
        record["metadata"]["batch_job_id"] = batch_job_id
    if chunk_id is not None:
        record["metadata"]["chunk_id"] = chunk_id
    if project_slug is not None:
        record["metadata"]["project_slug"] = project_slug
    if extra:
        record["metadata"].update(extra)

    history_dir = _ensure_history_dir()
    path = history_dir / filename

    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        _LAST_LOG_PATH.set(path)
    except Exception:
        logger.warning("Failed to write prompt log to %s", path, exc_info=True)

    return path


def update_log_response(
    log_path: Path,
    response: str,
    *,
    retrieved_at: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Atomically rewrite an existing prompt log to fill in the response.

    Used when a deferred result (e.g. batch retrieval) arrives for a log
    that was written at submission time with response=None. Preserves the
    original prompt and metadata; adds metadata.retrieved_at and merges any
    `extra` keys into metadata. Updates _LAST_LOG_PATH so callers can stamp
    chunk.last_llm_log without threading the path through every layer.
    """
    record = json.loads(log_path.read_text(encoding="utf-8"))
    record["response"] = response
    meta = record.setdefault("metadata", {})
    meta["retrieved_at"] = retrieved_at or datetime.now().isoformat()
    if extra:
        meta.update(extra)

    fd, tmp_name = tempfile.mkstemp(
        prefix=log_path.stem + ".",
        suffix=".tmp",
        dir=str(log_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, log_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        logger.warning("Failed to update prompt log %s", log_path, exc_info=True)
        raise

    _LAST_LOG_PATH.set(log_path)
    return log_path
