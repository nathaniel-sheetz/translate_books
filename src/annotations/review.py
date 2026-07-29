"""
The annotation-review pipeline: prepare, fanout, commit, run, apply.

Three interchangeable backends, mirroring ``src/judges``:

- **API** (:func:`run`) calls the LLM directly behind a dollar cost gate.
- **Task subagents** (:func:`prepare` → spawn ``annotation-worker`` → :func:`commit`)
  render prompts to files for session-spawned workers. No API spend.
- **Headless** (:func:`prepare` → :func:`fanout` → :func:`commit`) runs the same
  prompts through ``claude -p`` / ``cursor-agent -p``. No API spend.

All three build prompts with ``prompts.build_prompt_parts`` and parse with
:func:`parse_verdict`, so the results are the same shape whichever ran.

:func:`commit` and :func:`run` write ``results.json`` plus a dated markdown
report; neither touches ``annotations.jsonl``. :func:`apply` is the only writer,
it requires an explicit selection, and it re-checks that each annotation still
holds the text ``commit`` saw before replacing it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.annotations import prompts as annprompts
from src.annotations import store
from src.annotations.concordance import BookIndex
from src.annotations.report import write_report
from src.annotations.targets import (
    MANUAL_MULTI_ANCHOR,
    AnnotationTarget,
    SkippedAnnotation,
    build_targets,
)
from src.judges.llm_io import (
    JudgeParseError,
    call_judge,
    estimate_call_cost,
    parse_judge_json,
)

logger = logging.getLogger(__name__)

_DEFAULT_WORKER_MODEL = "sonnet"
_DEFAULT_BATCH_SIZE = 5
_DEFAULT_COST_LIMIT = 0.50

VALID_STATES = ("needs_help", "already_resolved")

# Withheld-from-write reasons decided at commit time (targets.py owns the ones
# decided at prepare time).
MANUAL_NO_NOTE_TEXT = "no_note_text"
MANUAL_CONTENT_CHANGED = "content_changed"

# Marker prefixed to appended notes so the reader can tell model text from their
# own at a glance. Footnotes never get one — that text is published.
_MARKER_WORD_BY_LANGUAGE = {
    "spanish": "IA",
    "español": "IA",
    "espanol": "IA",
    "french": "IA",
    "français": "IA",
    "italian": "IA",
    "italiano": "IA",
    "portuguese": "IA",
    "português": "IA",
}


def ai_marker(target_language: Optional[str]) -> str:
    """The attribution prefix for an appended note, in the book's language."""
    word = _MARKER_WORD_BY_LANGUAGE.get((target_language or "").strip().lower(), "AI")
    return f"— {word}:"


def annotations_dir(project_dir: Path) -> Path:
    """Working dir: ``<project>/.harness/annotations/`` (shared .harness root)."""
    return Path(project_dir) / ".harness" / "annotations"


def _manifest_path(project_dir: Path) -> Path:
    return annotations_dir(project_dir) / "manifest.json"


def _results_path(project_dir: Path) -> Path:
    return annotations_dir(project_dir) / "results.json"


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def parse_verdict(raw: str, *, key: str) -> dict[str, Any]:
    """Parse one worker/API verdict into a normalized dict.

    Raises:
        JudgeParseError: not JSON, missing a required field, or an unknown state.
    """
    data = parse_judge_json(raw, annprompts.REQUIRED_FIELDS)

    state = str(data.get("state") or "").strip()
    if state not in VALID_STATES:
        raise JudgeParseError(
            f"state must be one of {list(VALID_STATES)}, got {state!r}"
        )

    confidence = str(data.get("confidence") or "medium").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        evidence = []

    # The model echoes the key back; a mismatch means a worker answered about the
    # wrong annotation (seen when a batch prompt is mis-split). Trust the key we
    # asked about, but keep the disagreement visible.
    echoed = str(data.get("key") or "").strip()

    return {
        "key": key,
        "echoed_key": echoed or None,
        "key_mismatch": bool(echoed) and echoed != key,
        "state": state,
        "state_reason": str(data.get("state_reason") or "").strip(),
        "recommendation": str(data.get("recommendation") or "").strip(),
        "note_text": str(data.get("note_text") or "").strip(),
        "confidence": confidence,
        "evidence": [str(e) for e in evidence],
    }


def _outcome(
    target_meta: dict[str, Any], verdict: dict[str, Any]
) -> dict[str, Any]:
    """Combine a target's prepare-time metadata with its verdict.

    Decides ``writable`` — whether ``apply`` may write this one back — which is
    the conjunction of: the model asked for a change, it produced text to write,
    and nothing about the annotation's shape forbids an automatic write.
    """
    manual_reason = target_meta.get("manual_reason")
    state = verdict["state"]
    note_text = verdict["note_text"]

    if state == "needs_help" and not note_text and not manual_reason:
        manual_reason = MANUAL_NO_NOTE_TEXT

    writable = bool(
        state == "needs_help" and note_text and manual_reason is None
    )

    return {
        **target_meta,
        **verdict,
        "manual_reason": manual_reason,
        "writable": writable,
    }


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "manifest": "list of work entries, one annotation each: {key, chapter_id, es_idx, sub_id, "
    "type, content, anchors, manual_reason, prompt_path, draft_path, preamble_path, body_path}",
    "manifest_path": "path to the written manifest.json (fanout and commit read this)",
    "types": "annotation types included",
    "chapters": "chapter ids included, or null for the whole book",
    "skipped": "annotations gated out before any LLM call: {key, type, reason, content}",
    "worker_model": "model tier to pin each spawned annotation-worker to (default sonnet)",
    "batch_size": "recommended workers per wave / default headless concurrency",
    "usage_summary": "{targets, workers, skipped, by_type, worker_model, batch_size, estimated_api_cost}",
    "instructions": "what to do with the manifest (spawn workers or fanout, then commit)",
}


def prepare(
    project_dir: Path,
    *,
    types: Optional[Iterable[str]] = None,
    chapters: Optional[Iterable[str]] = None,
    worker_model: Optional[str] = None,
    batch_size: Optional[int] = None,
    keep_drafts: bool = False,
    target_language: Optional[str] = None,
) -> dict[str, Any]:
    """Render one prompt per annotation plus a manifest (no spend).

    Writes, per annotation, a ``prompt.txt`` (preamble + body, what a Task worker
    and the API path read) and — when the template splits — a shared
    ``preamble.<type>.txt`` plus a per-annotation ``body.txt`` for headless
    fan-out's prompt cache.

    Re-preparing clears the drafts for the entries it re-renders so ``commit``
    never reads an orphan; pass ``keep_drafts`` to protect work still in flight.
    """
    project_dir = Path(project_dir)
    worker_model = worker_model or _DEFAULT_WORKER_MODEL
    batch_size = int(batch_size or _DEFAULT_BATCH_SIZE)

    targets, skipped = build_targets(project_dir, types=types, chapters=chapters)
    context = annprompts.build_context(project_dir, target_language=target_language)

    adir = annotations_dir(project_dir)
    adir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    estimated_cost = 0.0
    by_type: dict[str, int] = {}
    shared_preambles: dict[str, str] = {}

    for target in targets:
        ann_type = target.ann_type
        by_type[ann_type] = by_type.get(ann_type, 0) + 1

        prompt_path = adir / f"{target.key}.{ann_type}.prompt.txt"
        draft_path = adir / f"{target.key}.{ann_type}.draft.json"
        body_path = adir / f"{target.key}.{ann_type}.body.txt"
        preamble_path = adir / f"preamble.{ann_type}.txt"

        prefix, suffix = annprompts.build_prompt_parts(target, context)
        prompt = prefix + suffix
        prompt_path.write_text(prompt, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)

        estimated_cost += estimate_call_cost(
            prompt,
            provider=context.get("provider"),
            model=context.get("model"),
        )

        entry: dict[str, Any] = {
            "key": target.key,
            "chapter_id": target.chapter_id,
            "es_idx": target.es_idx,
            "sub_id": target.sub_id,
            "type": ann_type,
            "content": target.content,
            "anchors": target.anchors,
            "manual_reason": target.manual_reason,
            "es_sentence": target.es_sentence,
            "prompt_path": str(prompt_path),
            "draft_path": str(draft_path),
            "prompt_version": annprompts.template_version(ann_type),
        }

        # Cache split: the preamble is per type, so the first target of a type
        # establishes it and the rest must match byte-for-byte. A mismatch means
        # something target-specific leaked above the marker — drop the split for
        # that entry rather than serve a wrong preamble.
        if prefix:
            established = shared_preambles.get(ann_type)
            if established is None:
                shared_preambles[ann_type] = prefix
                preamble_path.write_text(prefix, encoding="utf-8")
                established = prefix
            if prefix == established:
                body_path.write_text(suffix, encoding="utf-8")
                entry["preamble_path"] = str(preamble_path)
                entry["body_path"] = str(body_path)
            else:
                body_path.unlink(missing_ok=True)
        else:
            body_path.unlink(missing_ok=True)

        entries.append(entry)

    skipped_docs = [
        {
            "key": s.key,
            "chapter_id": s.chapter_id,
            "es_idx": s.es_idx,
            "sub_id": s.sub_id,
            "type": s.ann_type,
            "content": s.content,
            "reason": s.reason,
        }
        for s in skipped
    ]

    manifest_doc = {
        "types": sorted(set(types)) if types else list(store.ANNOTATION_TYPES),
        "chapters": sorted(set(chapters)) if chapters else None,
        "target_language": context["target_language"],
        "worker_model": worker_model,
        "batch_size": batch_size,
        "model": context.get("model"),
        "provider": context.get("provider"),
        "prepared_at": datetime.now().isoformat(),
        "entries": entries,
        "skipped": skipped_docs,
    }
    manifest_path = _manifest_path(project_dir)
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "status": "ok",
        "manifest": entries,
        "manifest_path": str(manifest_path),
        "types": manifest_doc["types"],
        "chapters": manifest_doc["chapters"],
        "target_language": context["target_language"],
        "skipped": skipped_docs,
        "worker_model": worker_model,
        "batch_size": batch_size,
        "usage_summary": {
            "targets": len(entries),
            "workers": len(entries),
            "skipped": len(skipped_docs),
            "by_type": by_type,
            "worker_model": worker_model,
            "batch_size": batch_size,
            "estimated_api_cost": round(estimated_cost, 4),
        },
        "instructions": (
            "For each manifest entry spawn one `annotation-worker` subagent pinned to "
            "worker_model (Task tool, subagent_type=annotation-worker) that reads "
            "prompt_path and writes ONLY the JSON verdict to draft_path, in waves of "
            "batch_size — or run `fanout` for a headless CLI wave (claude|cursor). "
            "Then run `commit`. Nothing here spends or calls an API."
            if entries
            else "Nothing to review — no eligible annotations in scope."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


# ---------------------------------------------------------------------------
# fanout
# ---------------------------------------------------------------------------

_FANOUT_SCHEMA = {
    "wrote": "list of keys whose drafts were written this wave",
    "failed": "list of {id, error} — re-run fanout for these",
    "skipped": "list of keys that already had a non-empty draft",
    "worker_model": "model tier used for the headless CLI",
    "concurrency": "max parallel headless CLI processes per wave",
    "cwd": "neutral empty cwd used for the workers",
    "cli": "headless CLI used (claude|cursor)",
    "warning": "optional non-fatal notice (e.g. Cursor paired with a Claude model alias)",
    "counts": "{wrote, failed, skipped, todo}",
    "instructions": "next step (commit, or re-fanout failed/missing)",
}


def _fanout_error(message: str, **extra: Any) -> dict[str, Any]:
    out = {
        "error": message,
        "wrote": [],
        "failed": [],
        "skipped": [],
        "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
        "_schema": _FANOUT_SCHEMA,
    }
    out.update(extra)
    return out


def fanout(
    project_dir: Path,
    *,
    target_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless CLI wave over prepared entries.

    Each job passes the shared preamble as ``system_prompt_file`` and the
    per-annotation body as ``input_text``. ``run_headless_wave`` handles the CLI
    difference: Claude gets ``--system-prompt-file`` (and the prompt cache),
    Cursor has no such flag so the launcher folds the preamble into stdin.

    ``runner`` is a test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    from src.harness import state as hstate
    from src.harness.headless import run_headless_wave, warn_cursor_claude_model

    project_dir = Path(project_dir)
    cfg = hstate.load_config(project_dir)
    cli_name = (cli or cfg.get("headless_cli") or "claude").strip().lower()

    manifest_path = _manifest_path(project_dir)
    if not manifest_path.exists():
        return _fanout_error("no annotation manifest — run `prepare` first")
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _fanout_error(f"unreadable manifest {manifest_path}: {exc}")

    entries = [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
    if target_ids is not None:
        wanted = set(target_ids)
        entries = [e for e in entries if e.get("key") in wanted]
        missing_ids = wanted - {e.get("key") for e in entries}
        if missing_ids:
            return _fanout_error(f"keys not in manifest: {sorted(missing_ids)}")

    worker_model = doc.get("worker_model") or _DEFAULT_WORKER_MODEL
    model_warning = warn_cursor_claude_model(cli_name, worker_model)

    if concurrency is None:
        try:
            concurrency = int(doc.get("batch_size") or _DEFAULT_BATCH_SIZE)
        except (TypeError, ValueError):
            concurrency = _DEFAULT_BATCH_SIZE
    if concurrency < 1:
        return _fanout_error(f"invalid concurrency {concurrency!r}; must be >= 1")

    skipped: list[str] = []
    ready: list[dict[str, Any]] = []
    pre_failed: list[dict[str, str]] = []

    for entry in entries:
        key = entry.get("key")
        if not key:
            pre_failed.append({"id": "?", "error": "malformed manifest entry: no key"})
            continue
        draft_path = Path(entry["draft_path"])
        if draft_path.exists():
            try:
                if draft_path.read_text(encoding="utf-8").strip():
                    skipped.append(key)
                    continue
            except (OSError, UnicodeDecodeError):
                pass

        preamble = entry.get("preamble_path")
        body = entry.get("body_path")
        prompt_path = Path(entry["prompt_path"])
        try:
            if preamble and body and Path(preamble).exists() and Path(body).exists():
                ready.append(
                    {
                        "id": key,
                        "input_text": Path(body).read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": preamble,
                    }
                )
            elif not prompt_path.exists():
                pre_failed.append(
                    {"id": key, "error": f"missing prompt_path: {prompt_path}"}
                )
            else:
                ready.append(
                    {
                        "id": key,
                        "input_text": prompt_path.read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": None,
                    }
                )
        except OSError as exc:
            pre_failed.append({"id": key, "error": f"{type(exc).__name__}: {exc}"[:500]})

    base = {
        "worker_model": worker_model,
        "cli": cli_name,
        "concurrency": concurrency,
        "_schema": _FANOUT_SCHEMA,
    }
    if model_warning:
        base["warning"] = model_warning

    if not ready and not pre_failed:
        return {
            **base,
            "wrote": [],
            "failed": [],
            "skipped": skipped,
            "cwd": None,
            "counts": {"wrote": 0, "failed": 0, "skipped": len(skipped), "todo": 0},
            "instructions": (
                "Run `commit` to land drafts."
                if skipped
                else "Nothing to fan out — no matching manifest entries."
            ),
        }

    wave = run_headless_wave(
        ready,
        model=worker_model,
        concurrency=concurrency,
        cli=cli_name,
        cli_bin=cli_bin,
        runner=runner,
    )

    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        return {
            **base,
            "error": wave["error"],
            "wrote": [],
            "failed": [],
            "skipped": skipped,
            "cwd": wave.get("cwd"),
            "counts": {"wrote": 0, "failed": 0, "skipped": len(skipped), "todo": 0},
            "instructions": "Fix the launcher error, then re-run `fanout`.",
        }

    failed = list(pre_failed) + list(wave.get("failed") or [])
    wrote = list(wave.get("wrote") or [])
    return {
        **base,
        "wrote": wrote,
        "failed": failed,
        "skipped": skipped,
        "cwd": wave.get("cwd"),
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "skipped": len(skipped),
            "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Run `commit` to land drafts. Re-run `fanout` (optionally with "
            "--target-ids) for any failed/missing, then commit again."
        ),
    }


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

_COMMIT_SCHEMA = {
    "status": "'ok' | 'error'",
    "committed": "list of {key, type, state, writable, confidence}",
    "failed": "list of {key, type, problem} — re-spawn these (capped ~3x)",
    "missing": "list of {key, type} whose draft file was absent — re-spawn",
    "counts": "{committed, failed, missing, writable, already_resolved, manual}",
    "report_path": "the dated markdown report written for this run",
    "results_path": "results.json — what `apply` reads",
    "results": "per-annotation outcome: content at run time, state, recommendation, note_text, "
    "the exact new_content apply would write, and manual_reason when it may not",
    "skipped": "annotations gated out before any LLM call (carried from the manifest)",
}


def _load_manifest(project_dir: Path) -> tuple[Optional[dict], Optional[dict]]:
    manifest_path = _manifest_path(project_dir)
    if not manifest_path.exists():
        return None, {
            "status": "error",
            "error": "no annotation manifest — run `prepare` first",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, {
            "status": "error",
            "error": f"unreadable manifest {manifest_path}: {exc}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }


def _merge_results(
    project_dir: Path, fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge this commit's results over whatever ``results.json`` already holds.

    ``results.json`` is the apply plan, and committing is expected to happen once
    per worker wave. A wholesale overwrite would mean the wave-2 commit throws
    away wave 1's plan — and a commit that found only missing drafts (the state
    right after a re-prepare) would erase the plan entirely. Merging by key makes
    repeated commits accumulate, which is what the wave workflow assumes.

    Fresh results win; prior entries for keys not in this run are preserved.
    """
    merged: dict[str, dict[str, Any]] = {}
    results_path = _results_path(project_dir)
    if results_path.exists():
        try:
            prior = json.loads(results_path.read_text(encoding="utf-8"))
            for item in prior.get("results") or []:
                if isinstance(item, dict) and item.get("key"):
                    merged[item["key"]] = item
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ignoring unreadable prior results %s: %s", results_path, exc)
    for item in fresh:
        merged[item["key"]] = item
    return list(merged.values())


def _planned_content(
    entry: dict[str, Any], note_text: str, marker: str
) -> tuple[str, str]:
    """Return ``(mode, new_content)`` for an annotation given its note text.

    ``footnote`` is REPLACE because its content *is* the published endnote text
    (``src/endnotes.py`` strips the first bracket and publishes the rest), so
    appending a gloss after an instruction word would print that word into the
    book. The bracket anchor is re-attached here; everything else is dropped, and
    the original survives in the append-only log and in the report.

    The other three types are APPEND — nothing downstream publishes them, so the
    reader's own words are kept and the model's addition is marked.
    """
    original = entry.get("content") or ""
    if entry.get("type") == "footnote":
        anchors = entry.get("anchors") or []
        if anchors:
            return "replace", f"[{anchors[0]}] {note_text}"
        return "replace", note_text
    if original.strip():
        return "append", f"{original}\n{marker} {note_text}"
    return "append", f"{marker} {note_text}"


def commit(
    project_dir: Path,
    *,
    report: bool = True,
) -> dict[str, Any]:
    """Collect worker drafts, parse them, and write results plus a report.

    Never touches ``annotations.jsonl`` — that is :func:`apply`'s job alone.
    """
    project_dir = Path(project_dir)
    doc, error = _load_manifest(project_dir)
    if error:
        return error

    entries = doc.get("entries") or []
    if not isinstance(entries, list):
        return {
            "status": "error",
            "error": f"manifest 'entries' is not a list: {type(entries).__name__}",
            "committed": [],
            "failed": [],
            "missing": [],
            "_schema": _COMMIT_SCHEMA,
        }

    target_language = doc.get("target_language") or "Spanish"
    marker = ai_marker(target_language)
    adir = annotations_dir(project_dir).resolve()

    committed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("key"):
            failed.append({"key": "?", "type": "?", "problem": "malformed manifest entry"})
            continue
        key = entry["key"]
        ann_type = entry.get("type") or "flag"
        draft_path = Path(entry.get("draft_path") or "")

        if not draft_path or not draft_path.resolve().is_relative_to(adir):
            failed.append(
                {"key": key, "type": ann_type, "problem": f"draft_path escapes annotations dir: {draft_path}"}
            )
            continue
        if not draft_path.exists():
            missing.append({"key": key, "type": ann_type})
            continue
        try:
            raw = draft_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failed.append({"key": key, "type": ann_type, "problem": f"unreadable draft: {exc}"})
            continue

        try:
            verdict = parse_verdict(raw, key=key)
        except JudgeParseError as exc:
            failed.append({"key": key, "type": ann_type, "problem": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - one bad draft must not sink the run
            logger.error("annotation-commit: %s parse crashed: %s", key, exc)
            failed.append(
                {"key": key, "type": ann_type, "problem": f"{type(exc).__name__}: {exc}"}
            )
            continue

        outcome = _outcome(entry, verdict)
        if outcome["writable"]:
            mode, new_content = _planned_content(entry, outcome["note_text"], marker)
            outcome["mode"] = mode
            outcome["new_content"] = new_content
        else:
            outcome["mode"] = None
            outcome["new_content"] = None

        results.append(outcome)
        committed.append(
            {
                "key": key,
                "type": ann_type,
                "state": outcome["state"],
                "writable": outcome["writable"],
                "confidence": outcome["confidence"],
            }
        )

    # Two different documents, on purpose. results.json is the durable apply
    # plan and accumulates across commits; the report is a dated record of *one*
    # run and must show only what this commit reviewed. Feeding the merged plan
    # to the report made a 3-annotation run render 15 results carried over from
    # an earlier run, with content quoted as of that run rather than this one —
    # which breaks the report's one hard guarantee (see src/annotations/report.py).
    plan_results = _merge_results(project_dir, results)
    results_doc = {
        "project": Path(project_dir).name,
        "committed_at": datetime.now().isoformat(),
        "target_language": target_language,
        "marker": marker,
        "backend": "subagent",
        "worker_model": doc.get("worker_model"),
        "types": doc.get("types"),
        "chapters": doc.get("chapters"),
        "results": plan_results,
        "skipped": doc.get("skipped") or [],
        "failed": failed,
        "missing": missing,
    }
    results_path = _results_path(project_dir)
    results_path.write_text(
        json.dumps(results_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_path = (
        write_report(
            project_dir,
            {**results_doc, "results": results, "carried_over": len(plan_results) - len(results)},
        )
        if report
        else None
    )

    writable = sum(1 for r in results if r["writable"])
    already = sum(1 for r in results if r["state"] == "already_resolved")
    manual = sum(1 for r in results if r.get("manual_reason"))

    return {
        "status": "ok",
        "committed": committed,
        "failed": failed,
        "missing": missing,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "missing": len(missing),
            "writable": writable,
            "already_resolved": already,
            "manual": manual,
        },
        "report_path": str(report_path) if report_path else None,
        "results_path": str(results_path),
        "results": results,
        "skipped": results_doc["skipped"],
        "_schema": _COMMIT_SCHEMA,
    }


# ---------------------------------------------------------------------------
# run (API backend)
# ---------------------------------------------------------------------------

_RUN_SCHEMA = {
    "status": "'ok' | 'cost_exceeded' | 'error'",
    "estimated_cost": "USD estimate for the whole scope",
    "cost_limit": "the limit that gated this run",
    "committed": "list of {key, type, state, writable, confidence}",
    "failed": "list of {key, type, problem}",
    "counts": "{committed, failed, writable, already_resolved, manual}",
    "report_path": "the dated markdown report written for this run",
    "results_path": "results.json — what `apply` reads",
    "results": "per-annotation outcome (same shape as commit)",
    "skipped": "annotations gated out before any LLM call",
}


def run(
    project_dir: Path,
    *,
    types: Optional[Iterable[str]] = None,
    chapters: Optional[Iterable[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    cost_limit: float = _DEFAULT_COST_LIMIT,
    confirm: bool = False,
    target_language: Optional[str] = None,
    report: bool = True,
) -> dict[str, Any]:
    """Review every in-scope annotation through the API, behind a cost gate.

    Returns ``status: "cost_exceeded"`` without spending when the estimate is over
    ``cost_limit`` and ``confirm`` is not set.
    """
    project_dir = Path(project_dir)
    targets, skipped = build_targets(project_dir, types=types, chapters=chapters)
    context = annprompts.build_context(project_dir, target_language=target_language)
    target_language = context["target_language"]
    marker = ai_marker(target_language)

    built: list[tuple[AnnotationTarget, str]] = []
    estimated = 0.0
    for target in targets:
        prompt = annprompts.build_prompt(target, context)
        built.append((target, prompt))
        estimated += estimate_call_cost(prompt, provider=provider, model=model)
    estimated = round(estimated, 4)

    if not confirm and estimated > cost_limit:
        return {
            "status": "cost_exceeded",
            "estimated_cost": estimated,
            "cost_limit": cost_limit,
            "targets": len(built),
            "message": (
                f"Estimated ${estimated:.4f} to review {len(built)} annotation(s), "
                f"over the ${cost_limit:.2f} limit. Re-run with --confirm to proceed."
            ),
            "_schema": _RUN_SCHEMA,
        }

    committed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []

    for target, prompt in built:
        entry = {
            "key": target.key,
            "chapter_id": target.chapter_id,
            "es_idx": target.es_idx,
            "sub_id": target.sub_id,
            "type": target.ann_type,
            "content": target.content,
            "anchors": target.anchors,
            "manual_reason": target.manual_reason,
            "es_sentence": target.es_sentence,
            "prompt_version": annprompts.template_version(target.ann_type),
        }
        try:
            raw = call_judge(
                prompt,
                provider=provider,
                model=model,
                call_type=f"annotation_{target.ann_type}",
            )
            verdict = parse_verdict(raw, key=target.key)
        except JudgeParseError as exc:
            failed.append({"key": target.key, "type": target.ann_type, "problem": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - one failure must not sink the run
            logger.error("annotation-run: %s failed: %s", target.key, exc)
            failed.append(
                {"key": target.key, "type": target.ann_type, "problem": f"{type(exc).__name__}: {exc}"}
            )
            continue

        outcome = _outcome(entry, verdict)
        if outcome["writable"]:
            mode, new_content = _planned_content(entry, outcome["note_text"], marker)
            outcome["mode"] = mode
            outcome["new_content"] = new_content
        else:
            outcome["mode"] = None
            outcome["new_content"] = None
        results.append(outcome)
        committed.append(
            {
                "key": target.key,
                "type": target.ann_type,
                "state": outcome["state"],
                "writable": outcome["writable"],
                "confidence": outcome["confidence"],
            }
        )

    skipped_docs = [
        {
            "key": s.key,
            "chapter_id": s.chapter_id,
            "es_idx": s.es_idx,
            "sub_id": s.sub_id,
            "type": s.ann_type,
            "content": s.content,
            "reason": s.reason,
        }
        for s in skipped
    ]

    plan_results = _merge_results(project_dir, results)
    results_doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "target_language": target_language,
        "marker": marker,
        "backend": "api",
        "model": model,
        "provider": provider,
        "types": sorted(set(types)) if types else list(store.ANNOTATION_TYPES),
        "chapters": sorted(set(chapters)) if chapters else None,
        # Same split as commit(): the plan accumulates, the report is one run.
        # This path used to overwrite, so an API run scoped to one type silently
        # discarded the apply plan for every annotation it did not cover.
        "results": plan_results,
        "skipped": skipped_docs,
        "failed": failed,
        "missing": [],
    }
    annotations_dir(project_dir).mkdir(parents=True, exist_ok=True)
    results_path = _results_path(project_dir)
    results_path.write_text(
        json.dumps(results_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = (
        write_report(
            project_dir,
            {**results_doc, "results": results, "carried_over": len(plan_results) - len(results)},
        )
        if report
        else None
    )

    return {
        "status": "ok",
        "estimated_cost": estimated,
        "cost_limit": cost_limit,
        "committed": committed,
        "failed": failed,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "writable": sum(1 for r in results if r["writable"]),
            "already_resolved": sum(1 for r in results if r["state"] == "already_resolved"),
            "manual": sum(1 for r in results if r.get("manual_reason")),
        },
        "report_path": str(report_path) if report_path else None,
        "results_path": str(results_path),
        "results": results,
        "skipped": skipped_docs,
        "_schema": _RUN_SCHEMA,
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

_APPLY_SCHEMA = {
    "status": "'ok' | 'error'",
    "dry_run": "true when nothing was written (no --select, or --dry-run)",
    "applicable": "fixes that can be written: {key, type, mode, old, new, confidence}",
    "manual": "reviewed but not auto-writable: {key, type, reason, recommendation}",
    "applied": "keys actually written this run",
    "already_applied": "keys whose annotation already holds the planned text (no-op)",
    "stale": "keys whose annotation changed since the review — re-run prepare for these",
    "unknown_ids": "selected keys with no reviewed result",
    "annotations_path": "the file appended to, when anything was applied",
    "counts": "{applicable, manual, applied, already_applied, stale}",
}


def _live_records(project_dir: Path) -> dict[str, dict]:
    """Live annotations keyed by target key, for drift detection at apply time."""
    return {store.target_key(r): r for r in store.load_active(project_dir)}


def apply(
    project_dir: Path,
    *,
    select: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write reviewed notes back into ``annotations.jsonl``.

    The only writer in this package. Requires an explicit ``select``; without one
    it plans and writes nothing.

    Before writing, each annotation's *live* content is compared against what the
    review saw. A note edited in the reader since then is reported ``stale`` and
    skipped rather than silently overwritten — the review it is based on no longer
    describes the text on disk.
    """
    project_dir = Path(project_dir)
    results_path = _results_path(project_dir)
    if not results_path.exists():
        return {
            "status": "error",
            "error": "no reviewed results — run `commit` (or `run`) first",
            "_schema": _APPLY_SCHEMA,
        }
    try:
        doc = json.loads(results_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "error",
            "error": f"unreadable results {results_path}: {exc}",
            "_schema": _APPLY_SCHEMA,
        }

    results = {r["key"]: r for r in (doc.get("results") or []) if r.get("key")}
    live = _live_records(project_dir)

    applicable = [
        {
            "key": r["key"],
            "type": r["type"],
            "mode": r["mode"],
            "old": r.get("content") or "",
            "new": r["new_content"],
            "confidence": r["confidence"],
            "recommendation": r["recommendation"],
        }
        for r in results.values()
        if r.get("writable")
    ]
    manual = [
        {
            "key": r["key"],
            "type": r["type"],
            "reason": r.get("manual_reason") or (
                "already_resolved" if r["state"] == "already_resolved" else "not_writable"
            ),
            "recommendation": r["recommendation"],
        }
        for r in results.values()
        if not r.get("writable")
    ]

    plan_only = dry_run or not select
    base = {
        "status": "ok",
        "dry_run": plan_only,
        "applicable": applicable,
        "manual": manual,
        "_schema": _APPLY_SCHEMA,
    }
    if plan_only:
        return {
            **base,
            "applied": [],
            "already_applied": [],
            "stale": [],
            "unknown_ids": [],
            "counts": {
                "applicable": len(applicable),
                "manual": len(manual),
                "applied": 0,
                "already_applied": 0,
                "stale": 0,
            },
            "instructions": (
                "Plan only — nothing written. Re-run with --select <key,key,...> to apply."
            ),
        }

    selected = list(dict.fromkeys(select))
    applied: list[str] = []
    already_applied: list[str] = []
    stale: list[dict[str, str]] = []
    unknown: list[str] = []

    run_id = doc.get("committed_at") or datetime.now().isoformat()
    now = datetime.now().isoformat()

    for key in selected:
        result = results.get(key)
        if result is None or not result.get("writable"):
            # Not in the current plan. Before calling it unknown, check whether a
            # previous run already wrote it — after a re-prepare an applied note
            # is skipped as already_reviewed and so drops out of results.json,
            # and reporting that as a bad key would read like a failure.
            record = live.get(key)
            sidecar = (record or {}).get(store.AI_REVIEW_KEY)
            if isinstance(sidecar, dict) and sidecar.get("written_content") == (
                record.get("content") or ""
            ):
                already_applied.append(key)
            else:
                unknown.append(key)
            continue
        record = live.get(key)
        if record is None:
            stale.append({"key": key, "reason": "annotation no longer present"})
            continue

        current = record.get("content") or ""
        if current == result["new_content"]:
            # Re-running the same --select is a no-op, not a double write.
            already_applied.append(key)
            continue
        if current != (result.get("content") or ""):
            stale.append({"key": key, "reason": "annotation edited since the review"})
            continue

        new_record = {
            "project_id": record.get("project_id") or project_dir.name,
            "chapter_id": record.get("chapter_id"),
            "es_idx": record.get("es_idx"),
            "type": record.get("type"),
            "content": result["new_content"],
            "timestamp": now,
        }
        sub = store.storage_sub_id(record.get("sub_id"))
        if sub is not None:
            new_record["sub_id"] = sub
        if record.get("origin"):
            new_record["origin"] = record["origin"]
        new_record[store.AI_REVIEW_KEY] = {
            "run_id": run_id,
            "at": now,
            "mode": result["mode"],
            "prompt_version": result.get("prompt_version"),
            "original_content": result.get("content") or "",
            "written_content": result["new_content"],
        }
        store.append_record(project_dir, new_record)
        applied.append(key)

    return {
        **base,
        "dry_run": False,
        "applied": applied,
        "already_applied": already_applied,
        "stale": stale,
        "unknown_ids": unknown,
        "annotations_path": str(store.annotations_path(project_dir)) if applied else None,
        "counts": {
            "applicable": len(applicable),
            "manual": len(manual),
            "applied": len(applied),
            "already_applied": len(already_applied),
            "stale": len(stale),
        },
        "instructions": (
            "Applied notes are appended records; the originals remain in the "
            "append-only log. Re-run `prepare` to confirm they now report "
            "already_reviewed. Rebuild the EPUB if footnotes changed."
        ),
    }
