"""
Subagent backend for tailored judges: render prompts, collect drafts.

The two judge backends mirror translate-harness's two translation backends:

- **API backend** (``runner.run_judge_suite`` / ``run_judges.py run``) calls the
  LLM directly, behind a dollar cost gate — metered spend, needs an API key.
- **Subagent backend** (this module / ``run_judges.py prepare`` + ``commit``)
  renders each judge's prompt to a file for a spawned ``judge-worker`` subagent,
  then collects and parses the worker's JSON verdict — **zero API spend**, runs
  on the session.

Both backends share the same seam — ``Judge.build_prompt`` (used by ``prepare``
*and* the API ``run``) and ``Judge.parse_response`` (used by ``commit`` *and* the
API ``run``) — so a judge result is byte-for-byte the same whichever backend ran,
and persists identically into ``evaluations/<chunk>.json``.

Unlike translation, judges have no cross-chunk continuity, so there is no
sequential / chapter spawn-mode machinery here — just bounded-batch parallel
spawning (the orchestrator's ``batch_size``). Files live under
``<project>/.harness/judges/``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.evaluators import aggregate_results
from src.judges.base import JudgeTarget
from src.judges.llm_io import JudgeParseError
from src.judges.registry import get_judge
from src.judges.runner import build_run_header, estimate_suite_cost
from src.judges.scope import build_targets

logger = logging.getLogger(__name__)

_DEFAULT_WORKER_MODEL = "sonnet"
_DEFAULT_BATCH_SIZE = 5

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "manifest": "list of work entries: {target_id, target_type, judge, prompt_path, draft_path, source_word_count}",
    "manifest_path": "path to the written manifest.json (commit reads this)",
    "scope": "the --scope that was resolved",
    "judges": "judge names rendered",
    "worker_model": "model tier to pin each spawned judge-worker to (default sonnet)",
    "batch_size": "recommended workers to spawn per wave",
    "usage_summary": "{pairs, targets, source_words, worker_model, batch_size, estimated_api_cost}",
    "instructions": "what to do with the manifest (spawn workers, then commit)",
}

_COMMIT_SCHEMA = {
    "status": "'ok' | 'error'",
    "committed": "list of {target_id, judge} parsed this run",
    "failed": "list of {target_id, judge, problem} — re-spawn these (capped ~3x)",
    "missing": "list of {target_id, judge} whose draft file was absent — re-spawn",
    "counts": "{committed, failed, missing}",
    "summary": "aggregate_results() rollup across committed results",
    "run_header": "reproducibility metadata: judge versions, prompt hashes, backend=subagent, worker_model, git_commit",
    "results": "per committed (target, judge): serialized EvalResult",
    "persisted": "evaluations/*.json paths written when --persist is set, else null",
    "persist_errors": "list of '<target>/<judge>: <error>' strings for failed persists, else null",
}


def _judges_dir(project_dir: Path) -> Path:
    """Subagent working dir: ``<project>/.harness/judges/`` (shared .harness root)."""
    return Path(project_dir) / ".harness" / "judges"


def prepare(
    project_dir: Path,
    judge_names: list[str],
    scope: str,
    *,
    context: Optional[dict[str, Any]] = None,
    worker_model: Optional[str] = None,
    batch_size: Optional[int] = None,
) -> dict[str, Any]:
    """Render one prompt file per ``(target, judge)`` plus a manifest (no spend).

    For every target in ``scope`` and every judge in ``judge_names``, call
    ``judge.build_prompt`` — the *same* prompt the API path would send — and write
    it to ``.harness/judges/<target_id>.<judge>.prompt.txt``; assign a
    ``draft_path`` the worker writes its JSON verdict to. Stale drafts from a prior
    run are cleared so ``commit`` never reads an orphan.

    ``worker_model`` (default ``sonnet``) is the tier the orchestrator pins each
    spawned ``judge-worker`` to; ``batch_size`` (default 5) is the recommended
    fan-out per spawn wave. Nothing here calls an API; ``estimated_api_cost`` in
    ``usage_summary`` is the API-equivalent price shown for context only.
    """
    project_dir = Path(project_dir)
    context = dict(context or {})
    worker_model = worker_model or _DEFAULT_WORKER_MODEL
    batch_size = int(batch_size or _DEFAULT_BATCH_SIZE)

    targets = build_targets(project_dir, scope)

    jdir = _judges_dir(project_dir)
    jdir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    total_words = 0
    for target in targets:
        words = len((target.source_text or "").split())
        for judge_name in judge_names:
            judge = get_judge(judge_name)
            prompt = judge.build_prompt(target, context)
            prompt_path = jdir / f"{target.id}.{judge_name}.prompt.txt"
            draft_path = jdir / f"{target.id}.{judge_name}.draft.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            draft_path.unlink(missing_ok=True)  # clear any stale draft from a prior prepare
            total_words += words
            entries.append(
                {
                    "target_id": target.id,
                    "target_type": target.target_type,
                    "judge": judge_name,
                    "prompt_path": str(prompt_path),
                    "draft_path": str(draft_path),
                    "source_word_count": words,
                }
            )

    estimated_api_cost = estimate_suite_cost(judge_names, targets, context)

    manifest_doc = {
        "scope": scope,
        "judges": judge_names,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "model": context.get("judge_model"),
        "provider": context.get("judge_provider"),
        "entries": entries,
    }
    manifest_path = jdir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "status": "ok",
        "manifest": entries,
        "manifest_path": str(manifest_path),
        "scope": scope,
        "judges": judge_names,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "usage_summary": {
            "pairs": len(entries),
            "targets": len(targets),
            "source_words": total_words,
            "worker_model": worker_model,
            "batch_size": batch_size,
            "estimated_api_cost": estimated_api_cost,
        },
        "instructions": (
            "For each manifest entry spawn one `judge-worker` subagent pinned to "
            "worker_model (Task tool, subagent_type=judge-worker, model=worker_model) "
            "that reads prompt_path and writes ONLY the JSON verdict to draft_path, in "
            "bounded batches of batch_size. Then run `commit`. Nothing here spends or "
            "calls an API."
            if entries
            else "Nothing to judge — scope resolved to no targets."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


def commit(project_dir: Path, *, persist: bool = False) -> dict[str, Any]:
    """Collect ``judge-worker`` drafts, parse them, and (optionally) persist.

    Reads the ``prepare`` manifest; for each entry reads the worker's draft and
    calls ``judge.parse_response`` — the *same* parser the API path uses. A draft
    that parses is ``committed`` (and, with ``persist`` and a chunk target, written
    to ``evaluations/<chunk>.json`` via ``merge_judge_result`` — identical to the
    API ``--persist`` path). A draft that does not parse is ``failed``; an absent
    draft is ``missing``. Re-spawn ``failed`` + ``missing`` and re-run commit
    (idempotent: re-parsing a draft just re-persists the same result).
    """
    project_dir = Path(project_dir)
    jdir = _judges_dir(project_dir)
    manifest_path = jdir / "manifest.json"
    if not manifest_path.exists():
        return {
            "status": "error",
            "error": "no judge manifest — run `prepare` first",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }

    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "error",
            "error": f"unreadable manifest {manifest_path}: {exc}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }

    entries = doc.get("entries", [])
    worker_model = doc.get("worker_model") or _DEFAULT_WORKER_MODEL
    context = {
        "judge_model": doc.get("model"),
        "judge_provider": doc.get("provider"),
    }

    committed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    results = []
    persisted: Optional[list[str]] = [] if persist else None
    persist_errors: Optional[list[str]] = [] if persist else None

    for entry in entries:
        target = JudgeTarget(
            id=entry["target_id"],
            target_type=entry.get("target_type", "chunk"),
            source_text="",
            translated_text="",
            context={},
        )
        judge_name = entry["judge"]
        draft_path = Path(entry["draft_path"])

        if not draft_path.exists():
            missing.append({"target_id": target.id, "judge": judge_name})
            continue

        raw = draft_path.read_text(encoding="utf-8")
        try:
            judge = get_judge(judge_name)
            result = judge.parse_response(target, raw, context)
        except JudgeParseError as exc:
            failed.append(
                {"target_id": target.id, "judge": judge_name, "problem": str(exc)}
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one bad draft must not sink the batch
            logger.error(
                "judge-commit: %s/%s parse crashed: %s", target.id, judge_name, exc
            )
            failed.append(
                {
                    "target_id": target.id,
                    "judge": judge_name,
                    "problem": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        # Stamp how this result was produced so a persisted judge entry is
        # self-describing (the API path records model/provider; here we record
        # the worker tier + backend).
        result.metadata["backend"] = "subagent"
        result.metadata["worker_model"] = worker_model

        results.append(result)
        committed.append({"target_id": target.id, "judge": judge_name})

        if persist and target.target_type == "chunk":
            from web_ui.evaluations import merge_judge_result

            try:
                path = merge_judge_result(
                    project_dir, target.id, judge_name, result.model_dump(mode="json")
                )
                persisted.append(str(path))
            except Exception as exc:  # noqa: BLE001 - persist errors must not corrupt output
                logger.error(
                    "judge-commit: failed to persist %s/%s: %s",
                    target.id,
                    judge_name,
                    exc,
                )
                persist_errors.append(f"{target.id}/{judge_name}: {exc}")

    judge_names = list(dict.fromkeys(e["judge"] for e in entries))
    run_header = build_run_header(
        judge_names,
        target_count=len({e["target_id"] for e in entries}),
        model=context["judge_model"],
        provider=context["judge_provider"],
        backend="subagent",
        worker_model=worker_model,
    )

    return {
        "status": "ok",
        "committed": committed,
        "failed": failed,
        "missing": missing,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "missing": len(missing),
        },
        "summary": aggregate_results(results),
        "run_header": run_header,
        "results": [r.model_dump(mode="json") for r in results],
        "persisted": persisted,
        "persist_errors": persist_errors,
        "_schema": _COMMIT_SCHEMA,
    }
