#!/usr/bin/env python3
"""Non-interactive CLI for tailored LLM judges (two backends).

Three subcommands:

  * ``run``     — **API backend**. Run one judge or a named suite over a chunk or
                  chapter NOW, calling the LLM behind a dollar cost gate, and
                  (optionally) persist findings into ``evaluations/<chunk>.json``.
  * ``prepare`` — **subagent backend**, phase 1. Render one prompt file per
                  ``(target, judge)`` plus a manifest, for spawned ``judge-worker``
                  subagents to answer. Zero API spend.
  * ``commit``  — **subagent backend**, phase 2. Collect the workers' JSON drafts,
                  parse them, and (optionally) persist — identical output to ``run``.

Both backends share the same prompt builder and response parser, so a judge result
is byte-for-byte the same whichever backend produced it.

Cost / usage safety:
  * ``run`` cost-estimates the suite up front; if the estimate exceeds
    ``--cost-limit`` (default $0.50) it refuses to spend and returns
    ``status: "cost_exceeded"`` — re-run with ``--confirm``.
  * ``prepare`` / ``commit`` never call an API. The gate there is the
    conversational usage check the skill does before spawning N workers.

Every command prints one JSON object with a ``_schema`` block documenting its keys.

Examples:
    python scripts/run_judges.py run --project understood-betsy \\
        --judge dialogue --scope chapter:chapter_03
    python scripts/run_judges.py prepare --project understood-betsy \\
        --judge dialogue --scope chapter:chapter_03
    python scripts/run_judges.py commit --project understood-betsy --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every raya/guillemet/accent in the JSON we print (harness friction #5). Force
# UTF-8 so dialogue excerpts survive on every platform. The hasattr guard keeps
# this safe under pytest's captured streams, which lack ``reconfigure``.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time so it can't corrupt the JSON an agent parses (harness friction #4).
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.judges import (  # noqa: E402
    ScopeError,
    build_targets,
    resolve_suite,
    run_judge_suite,
)
from src.judges import subagent  # noqa: E402
from src.judges.registry import available_judges  # noqa: E402
from src.judges.subagent import _PREPARE_SCHEMA  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

_RUN_SCHEMA = {
    "status": "'ok' | 'cost_exceeded' | 'error'",
    "backend": "'api'",
    "project": "resolved project directory",
    "scope": "the --scope argument that was resolved",
    "judges": "list of judge names that ran",
    "estimated_cost": "coarse USD pre-run estimate (guardrail, not an invoice)",
    "run_header": "reproducibility metadata: judge versions, prompt hashes, model, git_commit",
    "summary": "aggregate_results() rollup across all (judge x target) results",
    "results": "per (target, judge): target_id, judge, passed, score, issues[], metadata",
    "persisted": "evaluations/*.json paths written when --persist is set, else null",
    "persist_errors": "list of '<chunk>/<judge>: <error>' strings for any failed persists, else null",
}

_APPLY_SCHEMA = {
    "status": "'ok' | 'error' | 'partial'",
    "mode": "'plan' (nothing changed) | 'applied'",
    "project": "resolved project directory",
    "judge": "judge whose persisted findings were considered",
    "scopes": "the --scope args resolved",
    "applicable": "plan mode: {id, chunk_id, chapter_id, rule, severity, old, new, char_start, char_end} "
    "for each finding that is a clean, uniquely-locatable text swap",
    "manual": "plan mode: {id, chunk_id, chapter_id, rule, severity, reason, excerpt, suggestion, message} "
    "for findings withheld from auto-apply (reason: no_suggestion | no_excerpt | "
    "suggestion_equals_excerpt | suggestion_not_literal | excerpt_not_found | excerpt_ambiguous)",
    "chunks_without_findings": "target chunks with no persisted findings for this judge",
    "applied": "applied mode: fix ids that were actually applied",
    "failed": "applied mode: selected fix ids that did not locate (omitted when empty)",
    "chapters_realigned": "applied mode: chapters recombined + realigned",
    "epub": "applied mode: rebuilt EPUB path, or null if not requested / nothing changed",
    "stale_marked": "applied mode: chunks whose persisted evaluation was stale-stamped",
    "archived_to": "applied mode: corrections_applied.jsonl path (shared reader/judge audit log)",
    "backups": "applied mode: pre-edit chunk backup paths under .chunk_edits/",
    "warnings": "applied mode: non-fatal notes (e.g. a fix that no longer located), else null",
}


def _build_judge_context(
    project_dir: Path, judge_names: list[str], model: str | None, provider: str | None
) -> tuple[dict, str | None]:
    """Build the judge ``context`` for both backends (API run + subagent prepare).

    Loads the per-project inputs judges read from disk so the API and subagent
    paths render byte-identical prompts:
      * ``style_json_path`` — for judges that use the style guide (existing).
      * ``address_map`` — the ``content`` prose of ``address_map.json`` for the
        forms-of-address judge.

    Returns ``(context, error)``. ``error`` is a human-readable string when the
    ``address`` judge is requested but no ``address_map.json`` exists (the caller
    emits it and refuses to run); otherwise ``None``.
    """
    context: dict = {"judge_model": model, "judge_provider": provider}

    style_path = project_dir / "style.json"
    if style_path.exists():
        context["style_json_path"] = style_path

    map_path = project_dir / "address_map.json"
    address_map_loaded = False
    if map_path.exists():
        try:
            from src.utils.file_io import load_address_map

            amap = load_address_map(map_path)
            # v1 the judge reads the prose ``content``; fall back to global_rules
            # if a committed map left content empty.
            context["address_map"] = (amap.content or "").strip() or amap.global_rules
            address_map_loaded = True
        except Exception as exc:  # noqa: BLE001 - surface as a clean CLI error
            return context, (
                f"address_map.json at {map_path} failed to load: {exc}. "
                f"Re-run: python scripts/harness.py address-map commit --project {project_dir.name}"
            )

    if "address" in judge_names and not address_map_loaded:
        return context, (
            "The 'address' judge needs a per-book address map, but "
            f"{map_path} does not exist. Build it first:\n"
            f"  python scripts/harness.py address-map prepare --project {project_dir.name}\n"
            f"  python scripts/harness.py address-map commit  --project {project_dir.name}"
        )

    return context, None


def _resolve_project(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p
    candidate = _REPO_ROOT / "projects" / arg
    if candidate.is_dir():
        return candidate
    raise SystemExit(
        json.dumps(
            {
                "status": "error",
                "error": f"Project not found: {arg!r} (looked for a directory and "
                f"projects/{arg}).",
            },
            ensure_ascii=False,
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_judges",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_select(p: argparse.ArgumentParser, *, multi_scope: bool = False) -> None:
        """Project + judge/suite + scope + model/provider — shared by run & prepare.

        ``multi_scope`` (prepare only) lets ``--scope`` repeat, so several chapters
        render into one manifest and a single ``commit`` collects them all.
        """
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--judge", help=f"Single judge to run (one of: {', '.join(available_judges())})"
        )
        group.add_argument("--suite", help="Named suite of judges (e.g. 'default')")
        if multi_scope:
            p.add_argument(
                "--scope",
                required=True,
                action="append",
                metavar="SCOPE",
                help="Target scope: 'chunk:<chunk_id>' or 'chapter:<chapter_id>'. "
                "Repeatable — pass --scope multiple times to stage several chapters "
                "in one manifest for a single commit.",
            )
        else:
            p.add_argument(
                "--scope",
                required=True,
                help="Target scope: 'chunk:<chunk_id>' or 'chapter:<chapter_id>'",
            )
        p.add_argument("--model", default=None, help="Judge model id override")
        p.add_argument("--provider", default=None, help="Judge provider override")

    # run — API backend ------------------------------------------------------
    rp = sub.add_parser("run", help="API backend: run judges now (metered, cost-gated)")
    add_select(rp)
    rp.add_argument(
        "--cost-limit",
        type=float,
        default=0.50,
        help="Max estimated USD before --confirm is required (default 0.50)",
    )
    rp.add_argument(
        "--confirm",
        action="store_true",
        help="Proceed even if the estimate exceeds --cost-limit",
    )
    rp.add_argument(
        "--persist",
        action="store_true",
        help="Write findings into evaluations/<chunk>.json (dashboard badges)",
    )
    rp.add_argument("--verbose", action="store_true", help="Debug logging")

    # prepare — subagent backend, phase 1 -----------------------------------
    pp = sub.add_parser(
        "prepare",
        help="Subagent backend: render per-(target,judge) prompts + manifest (no spend)",
    )
    add_select(pp, multi_scope=True)
    pp.add_argument(
        "--worker-model",
        dest="worker_model",
        default=None,
        help="Model tier to pin spawned judge-workers to (default: sonnet)",
    )
    pp.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=None,
        help="Recommended workers to spawn per wave (default 5)",
    )
    pp.add_argument(
        "--targets-per-worker",
        dest="targets_per_worker",
        type=int,
        default=1,
        help="Group up to N low-dialogue-density targets into one worker prompt to "
        "amortize per-worker overhead (default 1 = one target per worker). "
        "Dialogue-dense targets are always judged solo. Recovery re-prepares should "
        "leave this at 1 so a bad chunk never drags its group-mates.",
    )
    pp.add_argument(
        "--keep-drafts",
        dest="keep_drafts",
        action="store_true",
        help="Don't clear existing worker drafts (re-prepare is otherwise destructive). "
        "Use when re-preparing to recover a manifest without re-spawning workers.",
    )
    pp.add_argument("--verbose", action="store_true", help="Debug logging")

    # commit — subagent backend, phase 2 ------------------------------------
    cp = sub.add_parser(
        "commit",
        help="Subagent backend: collect worker drafts, parse, optionally persist",
    )
    cp.add_argument("--project", required=True, help="Project id (under projects/) or path")
    cp.add_argument(
        "--persist",
        action="store_true",
        help="Write findings into evaluations/<chunk>.json (dashboard badges)",
    )
    cp.add_argument("--verbose", action="store_true", help="Debug logging")

    # apply — turn approved findings into chunk edits -----------------------
    ap = sub.add_parser(
        "apply",
        help="Apply user-approved judge findings to chunk text (careful, plan-first)",
    )
    ap.add_argument("--project", required=True, help="Project id (under projects/) or path")
    ap.add_argument(
        "--judge",
        default="dialogue",
        help="Judge whose persisted findings to apply (default: dialogue)",
    )
    ap.add_argument(
        "--scope",
        required=True,
        action="append",
        metavar="SCOPE",
        help="Target scope: 'chunk:<chunk_id>' or 'chapter:<chapter_id>'. Repeatable.",
    )
    ap.add_argument(
        "--select",
        default=None,
        help="Comma-separated fix ids (from the plan's applicable[].id) to apply. "
        "Omit to preview the plan without changing anything.",
    )
    ap.add_argument(
        "--rebuild-epub",
        dest="rebuild_epub",
        action="store_true",
        help="Rebuild the EPUB after applying (recombine + realign always run)",
    )
    ap.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview the plan and change nothing, even if --select is given",
    )
    ap.add_argument(
        "--source-lang", dest="source_lang", default="en",
        help="Source language code for realignment (default: en)",
    )
    ap.add_argument(
        "--target-lang", dest="target_lang", default="es",
        help="Target language code for realignment (default: es)",
    )
    ap.add_argument("--verbose", action="store_true", help="Debug logging")

    return parser


def _emit(payload: dict, schema: dict | None = None) -> None:
    if schema is not None:
        payload["_schema"] = schema
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_run(args: argparse.Namespace) -> int:
    """API backend: cost-gated suite run, optional persist (the original behavior)."""
    project_dir = _resolve_project(args.project)

    try:
        judge_names = [args.judge] if args.judge else resolve_suite(args.suite)
    except ValueError as exc:
        _emit({"status": "error", "error": str(exc)}, _RUN_SCHEMA)
        return 1

    try:
        targets = build_targets(project_dir, args.scope)
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scope": args.scope}, _RUN_SCHEMA)
        return 1

    context, ctx_error = _build_judge_context(
        project_dir, judge_names, args.model, args.provider
    )
    if ctx_error:
        _emit({"status": "error", "error": ctx_error, "scope": args.scope}, _RUN_SCHEMA)
        return 1

    outcome = run_judge_suite(
        judge_names,
        targets,
        context,
        cost_limit=args.cost_limit,
        confirm=args.confirm,
    )

    if outcome["status"] == "cost_exceeded":
        _emit(
            {
                "status": "cost_exceeded",
                "backend": "api",
                "project": str(project_dir),
                "scope": args.scope,
                "judges": judge_names,
                "estimated_cost": outcome["estimated_cost"],
                "cost_limit": outcome["cost_limit"],
                "message": outcome["message"],
            },
            _RUN_SCHEMA,
        )
        return 0

    results = outcome["results"]
    serialized = [r.model_dump(mode="json") for r in results]

    persisted: list[str] | None = None
    persist_errors: list[str] | None = None
    if args.persist:
        from web_ui.evaluations import merge_judge_result

        persisted = []
        persist_errors = []
        for result, payload in zip(results, serialized):
            # Only chunk-keyed results map to an evaluations/<chunk>.json file.
            if result.target_type == "chunk":
                try:
                    path = merge_judge_result(
                        project_dir, result.target_id, result.eval_name, payload
                    )
                    persisted.append(str(path))
                except Exception as exc:  # noqa: BLE001 - persist errors must not corrupt stdout JSON
                    logging.getLogger(__name__).error(
                        "Failed to persist %s/%s: %s", result.target_id, result.eval_name, exc
                    )
                    persist_errors.append(f"{result.target_id}/{result.eval_name}: {exc}")

    _emit(
        {
            "status": "ok",
            "backend": "api",
            "project": str(project_dir),
            "scope": args.scope,
            "judges": judge_names,
            "estimated_cost": outcome["estimated_cost"],
            "run_header": outcome["run_header"],
            "summary": outcome["aggregated"],
            "results": serialized,
            "persisted": persisted,
            "persist_errors": persist_errors,
        },
        _RUN_SCHEMA,
    )
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    """Subagent backend phase 1: render prompts + manifest (no spend)."""
    project_dir = _resolve_project(args.project)

    try:
        judge_names = [args.judge] if args.judge else resolve_suite(args.suite)
    except ValueError as exc:
        _emit({"status": "error", "error": str(exc)}, _PREPARE_SCHEMA)
        return 1

    context, ctx_error = _build_judge_context(
        project_dir, judge_names, args.model, args.provider
    )
    if ctx_error:
        _emit({"status": "error", "error": ctx_error, "scopes": args.scope}, _PREPARE_SCHEMA)
        return 1
    try:
        payload = subagent.prepare(
            project_dir,
            judge_names,
            args.scope,
            context=context,
            worker_model=args.worker_model,
            batch_size=args.batch_size,
            targets_per_worker=args.targets_per_worker,
            keep_drafts=args.keep_drafts,
        )
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scopes": args.scope}, _PREPARE_SCHEMA)
        return 1

    payload["project"] = str(project_dir)
    _emit(payload)
    return 0


def _cmd_commit(args: argparse.Namespace) -> int:
    """Subagent backend phase 2: collect worker drafts, parse, optionally persist."""
    project_dir = _resolve_project(args.project)
    payload = subagent.commit(project_dir, persist=args.persist)
    payload["project"] = str(project_dir)
    _emit(payload)
    return 1 if payload.get("status") == "error" else 0


def _cmd_apply(args: argparse.Namespace) -> int:
    """Apply user-approved judge findings to chunk text (careful, plan-first).

    Plan-first: without ``--select`` (or with ``--dry-run``) it only *reports*
    which persisted findings are a clean, uniquely-locatable text swap
    (``applicable``) and which are withheld (``manual``) — nothing is written.
    With ``--select`` it applies only the chosen ids, reusing the reader-
    corrections pipeline (backup -> edit -> recombine -> realign -> archive) so
    the edit is logged exactly like other chunk edits, then stale-marks the
    edited chunks' evaluations. The reader's ``corrections.jsonl`` queue is never
    touched.
    """
    project_dir = _resolve_project(args.project)

    from src.corrections_apply import (
        apply_to_chunk,
        archive_applied_records,
        realign_chapter,
        rebuild_epub,
        recombine_chapter,
    )
    from src.judges.fixes import ProposedFix, classify_fix, to_correction_record
    from src.utils.file_io import load_chunk, save_chunk
    from web_ui.evaluations import load_chunk_evaluation, mark_evaluation_stale

    judge = args.judge

    # 1. Resolve scopes -> unique, translated chunk targets (preserve order).
    targets: dict[str, object] = {}
    order: list[str] = []
    try:
        for scope in args.scope:
            for target in build_targets(project_dir, scope):
                if target.target_type != "chunk" or target.id in targets:
                    continue
                targets[target.id] = target
                order.append(target.id)
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scopes": args.scope}, _APPLY_SCHEMA)
        return 1

    # 2. Classify each chunk's persisted findings against its current text.
    applicable: dict[str, tuple[str, str, ProposedFix]] = {}
    applicable_list: list[dict] = []
    manual_list: list[dict] = []
    chunks_without: list[str] = []
    any_findings = False
    warnings_out: list[str] = []

    for chunk_id in order:
        target = targets[chunk_id]
        chapter_id = target.context.get("chapter_id") or chunk_id.rsplit("_chunk_", 1)[0]
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not chunk_path.exists():
            chunks_without.append(chunk_id)
            continue
        try:
            chunk = load_chunk(chunk_path)
        except Exception as exc:
            warnings_out.append(f"{chunk_id}: failed to load chunk ({exc})")
            chunks_without.append(chunk_id)
            continue
        translated_text = chunk.translated_text or ""
        payload = load_chunk_evaluation(project_dir, chunk_id)
        judges = payload.get("judges") if isinstance(payload, dict) else None
        judge_entry = judges.get(judge) if isinstance(judges, dict) else None
        issues = judge_entry.get("issues") if isinstance(judge_entry, dict) else None
        if not issues:
            chunks_without.append(chunk_id)
            continue
        any_findings = True
        for i, issue in enumerate(issues):
            fid = f"{chunk_id}#{i}"
            result = classify_fix(issue, translated_text)
            if isinstance(result, ProposedFix):
                applicable[fid] = (chunk_id, chapter_id, result)
                applicable_list.append(
                    {
                        "id": fid, "chunk_id": chunk_id, "chapter_id": chapter_id,
                        "rule": result.rule, "severity": result.severity,
                        "old": result.excerpt, "new": result.suggestion,
                        "char_start": result.char_start, "char_end": result.char_end,
                    }
                )
            else:
                manual_list.append(
                    {
                        "id": fid, "chunk_id": chunk_id, "chapter_id": chapter_id,
                        "rule": result.rule, "severity": result.severity,
                        "reason": result.reason, "excerpt": result.excerpt,
                        "suggestion": result.suggestion, "message": result.message,
                    }
                )

    if not any_findings:
        _emit(
            {
                "status": "error",
                "error": f"No persisted '{judge}' findings for the given scope. Run the "
                "judge with --persist first (run/commit) before applying.",
                "scopes": args.scope,
                "chunks_without_findings": chunks_without,
            },
            _APPLY_SCHEMA,
        )
        return 1

    # 3. Plan mode — report, change nothing.
    if args.dry_run or not args.select:
        _emit(
            {
                "status": "ok", "mode": "plan", "project": str(project_dir),
                "judge": judge, "scopes": args.scope,
                "applicable": applicable_list, "manual": manual_list,
                "chunks_without_findings": chunks_without,
            },
            _APPLY_SCHEMA,
        )
        return 0

    # 4. Apply mode — only the explicitly-selected, applicable ids.
    selected_ids = [s.strip() for s in args.select.split(",") if s.strip()]
    unknown = [s for s in selected_ids if s not in applicable]
    if unknown:
        _emit(
            {
                "status": "error",
                "error": "Selected id(s) are not in the applicable set (unknown, or "
                "classified as manual). Re-check the plan's applicable[].id.",
                "unknown_ids": unknown,
                "applicable_ids": list(applicable.keys()),
            },
            _APPLY_SCHEMA,
        )
        return 1

    # Group selected fixes by chunk (keep their ids for reporting).
    by_chunk: dict[str, dict] = {}
    for fid in dict.fromkeys(selected_ids):
        chunk_id, chapter_id, fix = applicable[fid]
        entry = by_chunk.setdefault(chunk_id, {"chapter_id": chapter_id, "items": []})
        entry["items"].append((fid, fix))

    ts = time.strftime("%Y%m%dT%H%M%S")
    applied_ids: list[str] = []
    failed_ids: list[str] = []
    edited_chunks: list[str] = []
    affected_chapters: list[str] = []
    all_records: list[dict] = []
    backups: list[str] = []

    for chunk_id, entry in by_chunk.items():
        chapter_id = entry["chapter_id"]
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not chunk_path.exists():
            failed_ids.extend(fid for fid, _ in entry["items"])
            warnings_out.append(f"{chunk_id}: chunk file missing")
            continue
        try:
            chunk = load_chunk(chunk_path)
        except Exception as exc:
            failed_ids.extend(fid for fid, _ in entry["items"])
            warnings_out.append(f"{chunk_id}: failed to load chunk ({exc})")
            continue

        items: list[tuple[str, ProposedFix]] = entry["items"]
        records = [
            to_correction_record(
                fix, chunk_id=chunk_id, chapter_id=chapter_id,
                project_id=project_dir.name, judge_name=judge,
            )
            for _fid, fix in items
        ]
        updated, applied_count, applied_indices = apply_to_chunk(chunk, records)
        applied_set = set(applied_indices)

        chunk_failed = [items[i][0] for i in range(len(items)) if i not in applied_set]
        chunk_applied = [items[i][0] for i in applied_indices]
        if chunk_failed:
            failed_ids.extend(chunk_failed)
            warnings_out.append(
                f"{chunk_id}: {applied_count}/{len(records)} selected fixes located"
            )

        if applied_count > 0:
            # Pre-edit backup (reuse the web-UI editor convention; keep last 10).
            backup_root = project_dir / ".chunk_edits" / chapter_id / chunk_id
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_path = backup_root / f"{ts}.json"
            backup_path.write_text(chunk_path.read_text(encoding="utf-8"), encoding="utf-8")
            for old_backup in sorted(backup_root.glob("*.json"))[:-10]:
                try:
                    old_backup.unlink()
                except OSError:
                    pass
            backups.append(str(backup_path))

            save_chunk(updated, chunk_path)
            all_records.extend(records[i] for i in applied_indices)
            applied_ids.extend(chunk_applied)
            edited_chunks.append(chunk_id)
            if chapter_id not in affected_chapters:
                affected_chapters.append(chapter_id)

    # 5. Recombine + realign the touched chapters (always).
    for chapter_id in affected_chapters:
        recombine_chapter(project_dir, chapter_id)
        realign_chapter(project_dir, chapter_id, args.source_lang, args.target_lang)

    # 6. Archive (shared audit log) + stale-guard the edited evaluations.
    archive_path = archive_applied_records(project_dir, all_records) if all_records else None
    stale_marked: list[str] = []
    for chunk_id in edited_chunks:
        if mark_evaluation_stale(
            project_dir, chunk_id,
            f"translated_text edited by judge-review apply ({judge})",
        ) is not None:
            stale_marked.append(chunk_id)

    # 7. Optional EPUB rebuild.
    epub_path = None
    if args.rebuild_epub and affected_chapters:
        epub_path = rebuild_epub(project_dir)

    if not applied_ids:
        _emit(
            {
                "status": "error",
                "mode": "applied",
                "error": "None of the selected fixes could be located in the current chunk text.",
                "project": str(project_dir),
                "judge": judge,
                "scopes": args.scope,
                "applied": [],
                "failed": failed_ids,
                "chapters_realigned": [],
                "epub": None,
                "stale_marked": [],
                "archived_to": None,
                "backups": [],
                "warnings": warnings_out or None,
            },
            _APPLY_SCHEMA,
        )
        return 1

    apply_status = "partial" if failed_ids else "ok"
    _emit(
        {
            "status": apply_status, "mode": "applied", "project": str(project_dir),
            "judge": judge, "scopes": args.scope,
            "applied": applied_ids,
            "failed": failed_ids or None,
            "chapters_realigned": affected_chapters,
            "epub": str(epub_path) if epub_path else None,
            "stale_marked": stale_marked,
            "archived_to": str(archive_path) if archive_path else None,
            "backups": backups,
            "warnings": warnings_out or None,
        },
        _APPLY_SCHEMA,
    )
    return 0


_DISPATCH = {
    "run": _cmd_run,
    "prepare": _cmd_prepare,
    "commit": _cmd_commit,
    "apply": _cmd_apply,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
