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
from src.judges.scope import _ID_RE, build_targets

logger = logging.getLogger(__name__)

_DEFAULT_WORKER_MODEL = "sonnet"
_DEFAULT_BATCH_SIZE = 5

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "manifest": "list of work entries: {target_id, target_type, judge, prompt_path, draft_path, source_word_count}",
    "manifest_path": "path to the written manifest.json (commit reads this)",
    "scopes": "the list of --scope values resolved into this one manifest",
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
    scopes: str | list[str],
    *,
    context: Optional[dict[str, Any]] = None,
    worker_model: Optional[str] = None,
    batch_size: Optional[int] = None,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render one prompt file per ``(target, judge)`` plus a manifest (no spend).

    ``scopes`` may be a single scope string or a list of them; all are resolved
    into **one** manifest so a multi-chapter request is a single ``prepare`` ->
    spawn -> ``commit`` (no manifest clobbering). For every target across all
    scopes and every judge in ``judge_names``, call ``judge.build_prompt`` — the
    *same* prompt the API path would send — and write it to
    ``.harness/judges/<target_id>.<judge>.prompt.txt``; assign a ``draft_path`` the
    worker writes its JSON verdict to. ``(target_id, judge)`` pairs are deduped, so
    overlapping scopes (e.g. ``chapter:X`` + ``chunk:X_chunk_000``) render once.

    By default stale drafts from a prior run are cleared so ``commit`` never reads
    an orphan; pass ``keep_drafts=True`` to preserve already-written worker output
    during a recovery re-prepare (re-prepare is otherwise destructive).

    ``worker_model`` (default ``sonnet``) is the tier the orchestrator pins each
    spawned ``judge-worker`` to; ``batch_size`` (default 5) is the recommended
    fan-out per spawn wave. Nothing here calls an API; ``estimated_api_cost`` in
    ``usage_summary`` is the API-equivalent price shown for context only.
    """
    project_dir = Path(project_dir)
    context = dict(context or {})
    worker_model = worker_model or _DEFAULT_WORKER_MODEL
    batch_size = int(batch_size or _DEFAULT_BATCH_SIZE)
    scope_list = [scopes] if isinstance(scopes, str) else list(scopes)

    # Resolve every scope into one ordered target list, deduped by target_id so an
    # overlapping chapter:/chunk: pair doesn't render (or get cost-estimated) twice.
    targets = []
    seen_targets: set[str] = set()
    for scope in scope_list:
        try:
            scope_targets = build_targets(project_dir, scope)
        except (NotImplementedError, FileNotFoundError, ValueError) as exc:
            # ScopeError subclasses ValueError; surface which scope failed.
            raise type(exc)(f"scope {scope!r}: {exc}") from exc
        for target in scope_targets:
            if target.id not in seen_targets:
                seen_targets.add(target.id)
                targets.append(target)

    for target in targets:
        if not _ID_RE.match(target.id):
            raise ValueError(f"target id contains unsafe characters: {target.id!r}")

    jdir = _judges_dir(project_dir)
    jdir.mkdir(parents=True, exist_ok=True)

    judge_instances = {name: get_judge(name) for name in judge_names}

    entries: list[dict[str, Any]] = []
    total_words = 0
    for target in targets:
        words = len((target.source_text or "").split())
        for judge_name in judge_names:
            judge = judge_instances[judge_name]
            prompt = judge.build_prompt(target, context)
            prompt_path = jdir / f"{target.id}.{judge_name}.prompt.txt"
            draft_path = jdir / f"{target.id}.{judge_name}.draft.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            if not keep_drafts:
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
        "scopes": scope_list,
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
        "scopes": scope_list,
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
    if not isinstance(entries, list):
        return {
            "status": "error",
            "error": f"manifest 'entries' is not a list: {type(entries).__name__}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }
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
    judge_cache: dict[str, Any] = {}

    for entry in entries:
        try:
            target_id = entry["target_id"]
            judge_name = entry["judge"]
            draft_path = Path(entry["draft_path"])
        except (KeyError, TypeError) as exc:
            failed.append({"target_id": "?", "judge": "?", "problem": f"malformed manifest entry: {exc}"})
            continue

        target = JudgeTarget(
            id=target_id,
            target_type=entry.get("target_type", "chunk"),
            source_text="",
            translated_text="",
            context={},
        )

        if not draft_path.resolve().is_relative_to(_judges_dir(project_dir).resolve()):
            failed.append({"target_id": target_id, "judge": judge_name, "problem": f"draft_path escapes judges dir: {draft_path}"})
            continue

        if not draft_path.exists():
            missing.append({"target_id": target.id, "judge": judge_name})
            continue

        try:
            raw = draft_path.read_text(encoding="utf-8")
            judge = judge_cache.setdefault(judge_name, get_judge(judge_name))
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

    judge_names = doc.get("judges") or list(dict.fromkeys(e["judge"] for e in entries if "judge" in e))
    run_header = build_run_header(
        judge_names,
        target_count=len({e["target_id"] for e in entries if "target_id" in e}),
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
