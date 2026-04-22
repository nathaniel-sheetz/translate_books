"""
Per-chunk evaluator persistence helpers for the Flask dashboard.

Results for each chunk are written to
``projects/<project_id>/evaluations/<chunk_id>.json`` (one file per chunk,
single source of truth — overwritten on each rerun). User feedback on
individual issues is appended to ``_feedback.jsonl`` in the same directory.

The module intentionally has no Flask or request-global dependencies so it
can be unit-tested against a bare ``tmp_path`` directory.
"""

from __future__ import annotations

import json
import logging
from dataclasses import is_dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.app_config import get_enabled_evaluators, get_blacklist_path
from src.evaluators import aggregate_results, run_all_evaluators
from src.evaluators.location_normalizer import NormalizedIssue, fan_out_issues
from src.models import Blacklist, Chunk, EvalResult, EvaluationConfig, Glossary
from src.utils.file_io import load_blacklist, load_glossary

logger = logging.getLogger(__name__)

_FEEDBACK_FILENAME = "_feedback.jsonl"
_ALLOWED_FEEDBACK_TYPES = frozenset(
    {"false_positive", "bad_message", "missing_context_gap"}
)


# ---------------------------------------------------------------------------
# Paths


def _eval_results_dir(project_dir: Path) -> Path:
    """Return ``projects/<id>/evaluations/`` (does not create it)."""
    return Path(project_dir) / "evaluations"


def _eval_file(project_dir: Path, chunk_id: str) -> Path:
    """Return the path to a chunk's evaluation JSON file."""
    return _eval_results_dir(project_dir) / f"{chunk_id}.json"


def _feedback_file(project_dir: Path) -> Path:
    """Return the path to the per-project feedback JSONL file."""
    return _eval_results_dir(project_dir) / _FEEDBACK_FILENAME


# ---------------------------------------------------------------------------
# Serialization helpers


def _serialize_result(result: EvalResult) -> dict[str, Any]:
    """Dump an :class:`EvalResult` to a JSON-safe dict."""
    try:
        return result.model_dump(mode="json")
    except Exception:
        # Defensive fallback for older pydantic versions.
        return json.loads(result.model_dump_json())


def _serialize_issue(issue: NormalizedIssue | dict[str, Any]) -> dict[str, Any]:
    """Accept either a ``NormalizedIssue`` or a plain dict and return a dict."""
    if isinstance(issue, NormalizedIssue):
        return issue.to_dict()
    if is_dataclass(issue):
        return asdict(issue)
    return dict(issue)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically via a temp file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Public API


def save_chunk_evaluation(
    project_dir: Path,
    chunk_id: str,
    results: Iterable[EvalResult],
    aggregated: dict[str, Any],
    normalized_issues: Iterable[NormalizedIssue | dict[str, Any]],
    *,
    enabled_evals: Optional[list[str]] = None,
    llm_judge: Optional[dict[str, Any]] = None,
) -> Path:
    """Persist a full evaluation run for ``chunk_id``.

    Overwrites any previous file — callers use :func:`merge_llm_judge_result`
    to tack on the LLM judge section without losing the coded results.

    Args:
        project_dir: ``projects/<id>/`` directory.
        chunk_id: Chunk identifier (already validated by caller).
        results: Iterable of per-evaluator results.
        aggregated: Output from :func:`aggregate_results`.
        normalized_issues: Flattened view-layer issues for the UI.
        enabled_evals: Optional list of evaluator names the run enabled. If
            ``None``, inferred from the ``results`` list in order.
        llm_judge: Optional existing llm_judge section to preserve when the
            caller is replacing the coded evaluation.

    Returns:
        Path to the written JSON file.
    """
    results_list = list(results)
    serialized_results = [_serialize_result(r) for r in results_list]

    if enabled_evals is None:
        enabled_evals = [r.eval_name for r in results_list]

    payload: dict[str, Any] = {
        "chunk_id": chunk_id,
        "evaluated_at": datetime.now().isoformat(),
        "enabled_evals": enabled_evals,
        "aggregated": aggregated,
        "results": serialized_results,
        "normalized_issues": [_serialize_issue(i) for i in normalized_issues],
        "llm_judge": llm_judge,
    }

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    logger.debug("Saved chunk evaluation: %s", path)
    return path


def load_chunk_evaluation(
    project_dir: Path, chunk_id: str
) -> Optional[dict[str, Any]]:
    """Load the saved evaluation for a chunk, or ``None`` if missing."""
    path = _eval_file(project_dir, chunk_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load evaluation %s: %s", path, e)
        return None


def merge_llm_judge_result(
    project_dir: Path,
    chunk_id: str,
    result: dict[str, Any],
) -> Path:
    """Patch the ``llm_judge`` key of a chunk's evaluation JSON.

    Creates a minimal shell file if no coded evaluation exists yet — that way
    the LLM judge button still works in isolation.
    """
    payload = load_chunk_evaluation(project_dir, chunk_id)
    if payload is None:
        payload = {
            "chunk_id": chunk_id,
            "evaluated_at": datetime.now().isoformat(),
            "enabled_evals": [],
            "aggregated": None,
            "results": [],
            "normalized_issues": [],
            "llm_judge": None,
        }
    payload["llm_judge"] = result
    payload["llm_judge_at"] = datetime.now().isoformat()

    path = _eval_file(project_dir, chunk_id)
    _atomic_write_json(path, payload)
    return path


def append_feedback(
    project_dir: Path,
    chunk_id: str,
    eval_name: str,
    issue_index: int,
    feedback_type: str,
    message: Optional[str] = None,
    note: Optional[str] = None,
) -> Path:
    """Append a single feedback record to ``_feedback.jsonl``.

    Raises:
        ValueError: If ``feedback_type`` is not one of the allowed labels.
    """
    if feedback_type not in _ALLOWED_FEEDBACK_TYPES:
        raise ValueError(
            f"Unknown feedback_type {feedback_type!r}; "
            f"expected one of {sorted(_ALLOWED_FEEDBACK_TYPES)}"
        )

    record = {
        "ts": datetime.now().isoformat(),
        "chunk_id": chunk_id,
        "eval_name": eval_name,
        "issue_index": issue_index,
        "feedback_type": feedback_type,
        "message": message,
        "note": note,
    }

    path = _feedback_file(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_feedback_for_chunk(
    project_dir: Path, chunk_id: str
) -> list[dict[str, Any]]:
    """Return all feedback records for ``chunk_id`` from ``_feedback.jsonl``.

    Records are returned in insertion order. Since feedback is append-only,
    multiple records for the same ``(eval_name, issue_index)`` key may be
    present — callers that want "the latest label" should iterate and keep
    the last match per key.

    Malformed lines and I/O errors are swallowed with a log (feedback is
    best-effort UI state, not authoritative).
    """
    path = _feedback_file(project_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.debug(
                        "Skipping malformed feedback line in %s: %s", path, e
                    )
                    continue
                if record.get("chunk_id") == chunk_id:
                    out.append(record)
    except OSError as e:
        logger.warning("Failed to read feedback file %s: %s", path, e)
    return out


def load_project_summary(project_dir: Path) -> dict[str, dict[str, int]]:
    """Walk ``evaluations/*.json`` and return a per-chunk counts map.

    The shape matches what the chapter-table badge renderer expects:

    ``{chunk_id: {"errors": int, "warnings": int, "info": int, "total": int}}``.

    Missing or malformed files are skipped with a debug log — the summary is
    best-effort.
    """
    out: dict[str, dict[str, int]] = {}
    eval_dir = _eval_results_dir(project_dir)
    if not eval_dir.exists():
        return out

    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Skipping unreadable evaluation %s: %s", path, e)
            continue

        chunk_id = data.get("chunk_id") or path.stem
        aggregated = data.get("aggregated") or {}
        severity = aggregated.get("issues_by_severity") or {}
        out[chunk_id] = {
            "errors": int(severity.get("error", 0) or 0),
            "warnings": int(severity.get("warning", 0) or 0),
            "info": int(severity.get("info", 0) or 0),
            "total": int(aggregated.get("total_issues", 0) or 0),
        }

    return out


# ---------------------------------------------------------------------------
# High-level evaluation runner


# The seven coded evaluators; ``llm_judge`` is deliberately excluded here and
# exposed via its own opt-in endpoint.
CODED_EVAL_NAMES: tuple[str, ...] = (
    "length",
    "paragraph",
    "dictionary",
    "glossary",
    "completeness",
    "blacklist",
    "grammar",
)


def _load_project_glossary(project_dir: Path) -> Optional[Glossary]:
    """Best-effort glossary load from ``projects/<id>/glossary.json``."""
    glossary_path = Path(project_dir) / "glossary.json"
    if not glossary_path.exists():
        return None
    try:
        return load_glossary(glossary_path)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to load glossary from %s: %s", glossary_path, e)
        return None


def _load_project_blacklist(project_dir: Path) -> Optional[Blacklist]:
    """Best-effort blacklist load.

    Resolution order:
    1. ``blacklist.json`` inside the project directory (per-project override).
    2. ``blacklist_path`` from ``app_config.json`` (system-wide default).
    """
    # 1. Per-project override
    project_bl = Path(project_dir) / "blacklist.json"
    if project_bl.exists():
        try:
            return load_blacklist(project_bl)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load project blacklist from %s: %s", project_bl, e)
            return None

    # 2. System-wide default from app_config.json
    bl_path = get_blacklist_path()
    if bl_path and bl_path.exists():
        try:
            return load_blacklist(bl_path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load blacklist from %s: %s", bl_path, e)
    return None


def run_coded_evaluators(
    chunk: Chunk,
    *,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
    enabled_evals: Optional[Iterable[str]] = None,
) -> tuple[list[EvalResult], dict[str, Any], list[NormalizedIssue]]:
    """Run the coded evaluators and return ``(results, aggregated, issues)``.

    ``issues`` is the flattened view-layer list ready for UI consumption.
    Failures inside individual evaluators are already swallowed by
    :func:`run_evaluator` and surfaced as ERROR issues in the returned
    :class:`EvalResult` list — the caller never needs to wrap this in its own
    try/except for evaluator crashes, only for programming errors in this
    pipeline.
    """
    if enabled_evals is not None:
        names = list(enabled_evals)
    else:
        system_filter = get_enabled_evaluators()
        if system_filter is not None:
            names = [n for n in system_filter if n in CODED_EVAL_NAMES]
        else:
            names = list(CODED_EVAL_NAMES)
    config = EvaluationConfig(enabled_evals=names)

    results = run_all_evaluators(chunk, config, glossary, blacklist)
    aggregated = aggregate_results(results)

    normalized: list[NormalizedIssue] = []
    for result in results:
        normalized.extend(fan_out_issues(result, chunk))

    return results, aggregated, normalized


def evaluate_and_persist_chunk(
    project_dir: Path,
    chunk: Chunk,
    *,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
    preserve_llm_judge: bool = True,
) -> dict[str, Any]:
    """Run coded evaluators on ``chunk`` and persist results.

    Args:
        project_dir: ``projects/<id>/`` directory.
        chunk: Chunk with a fresh ``translated_text`` to evaluate.
        glossary: Optional preloaded glossary. Loaded from disk if omitted.
        blacklist: Optional preloaded blacklist. Loaded from disk if omitted
            (project-level then app_config fallback).
        preserve_llm_judge: If ``True``, keep any existing ``llm_judge`` block
            from a previous run so rerunning the coded evaluators doesn't wipe
            out the LLM judge output.

    Returns:
        Dict with keys suitable for JSON-ing back to the frontend:
        ``aggregated``, ``issues`` (list of ``NormalizedIssue.to_dict()``),
        and ``enabled_evals``.
    """
    project_dir = Path(project_dir)
    if glossary is None:
        glossary = _load_project_glossary(project_dir)
    if blacklist is None:
        blacklist = _load_project_blacklist(project_dir)

    results, aggregated, normalized = run_coded_evaluators(
        chunk, glossary=glossary, blacklist=blacklist,
    )
    actually_ran = [r.eval_name for r in results]

    existing_llm = None
    if preserve_llm_judge:
        previous = load_chunk_evaluation(project_dir, chunk.id)
        if previous is not None:
            existing_llm = previous.get("llm_judge")

    save_chunk_evaluation(
        project_dir,
        chunk.id,
        results,
        aggregated,
        normalized,
        enabled_evals=actually_ran,
        llm_judge=existing_llm,
    )

    return {
        "aggregated": aggregated,
        "issues": [issue.to_dict() for issue in normalized],
        "enabled_evals": actually_ran,
    }


__all__ = [
    "CODED_EVAL_NAMES",
    "save_chunk_evaluation",
    "load_chunk_evaluation",
    "merge_llm_judge_result",
    "append_feedback",
    "load_feedback_for_chunk",
    "load_project_summary",
    "run_coded_evaluators",
    "evaluate_and_persist_chunk",
]
