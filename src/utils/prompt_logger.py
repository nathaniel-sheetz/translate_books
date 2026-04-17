"""
Log every LLM prompt and response to prompts/history/ for auditing,
debugging, and future batch-replay.

Each call produces a single JSON file with metadata, the full prompt,
and the full response (or null if the response hasn't arrived yet,
e.g. for batch submissions).
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve once — works regardless of cwd
_HISTORY_DIR = Path(__file__).resolve().parents[2] / "prompts" / "history"


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
    if extra:
        record["metadata"].update(extra)

    history_dir = _ensure_history_dir()
    path = history_dir / filename

    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("Failed to write prompt log to %s", path, exc_info=True)

    return path
