#!/usr/bin/env python3
"""Non-interactive CLI for tailored LLM judges (two backends).

Four subcommands:

  * ``run``     — **API backend**. Run one judge or a named suite over a chunk or
                  chapter NOW, calling the LLM behind a dollar cost gate, and
                  (optionally) persist findings into ``evaluations/<chunk>.json``.
  * ``prepare`` — **subagent backend**, phase 1. Render one prompt file per
                  ``(target, judge)`` plus a manifest, for spawned ``judge-worker``
                  subagents to answer. Zero API spend. Solo entries may also get
                  ``preamble_path`` / ``body_path`` for headless caching.
  * ``fanout``  — **subagent backend**, opt-in headless wave. Bounded ``claude -p``
                  processes write drafts from the prepare manifest (no Task workers).
  * ``commit``  — **subagent backend**, phase 2. Collect the workers' JSON drafts,
                  parse them, and (optionally) persist — identical output to ``run``.

Both backends share the same prompt builder and response parser, so a judge result
is byte-for-byte the same whichever backend produced it.

Cost / usage safety:
  * ``run`` cost-estimates the suite up front; if the estimate exceeds
    ``--cost-limit`` (default $0.50) it refuses to spend and returns
    ``status: "cost_exceeded"`` — re-run with ``--confirm``.
  * ``prepare`` / ``fanout`` / ``commit`` never call a metered API. The gate there
    is the conversational usage check the skill does before spawning N workers
    or running a headless wave.

Every command prints one JSON object with a ``_schema`` block documenting its keys.

Examples:
    python scripts/run_judges.py run --project understood-betsy \\
        --judge dialogue --scope chapter:chapter_03
    python scripts/run_judges.py prepare --project understood-betsy \\
        --judge dialogue --scope chapter:chapter_03
    python scripts/run_judges.py fanout --project understood-betsy
    python scripts/run_judges.py commit --project understood-betsy --persist
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
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

# ``apply`` realigns through a BERT model, and huggingface/transformers write
# their download bars and load banners to **stdout** — which would land in the
# middle of the one JSON object every subcommand promises. Quiet them at the
# source; ``_cmd_apply`` additionally redirects that block's stdout to stderr.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
# ``Warning: You are sending unauthenticated requests to the HF Hub`` on stderr
# during realign. Harmless, but it is the third distinct "stderr noise that isn't
# an error" the judge-review skill has to pre-warn about, and every such line
# trains a reader to skim stderr — which is where the real apply progress goes.
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

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
    "persisted_dir": "directory the --persist files were written to, else null",
    "persisted": "evaluations/*.json FILENAMES (join with persisted_dir) written when "
    "--persist is set, else null",
    "persist_errors": "list of '<chunk>/<judge>: <error>' strings for any failed persists, else null",
}

_APPLY_SCHEMA = {
    "status": "'ok' | 'error' | 'partial'",
    "mode": "'plan' (nothing changed) | 'applied' | 'realign' (--realign-only: no text changed)",
    "project": "resolved project directory",
    "judge": "the single judge considered, or null when --judge was repeated (see judges)",
    "judges": "every judge whose persisted findings were considered, in the order applied",
    "scopes": "the --scope args resolved ('chunk:<id>' | 'chapter:<id>' | 'book')",
    "applicable": "plan mode: {id, judge, qualified_id, chunk_id, chapter_id, rule, severity, "
    "old, new, char_start, char_end} for each finding that is a clean, uniquely-locatable text "
    "swap. 'id' is bare ('<chunk_id>#<i>'); 'qualified_id' prefixes the judge and is what "
    "--select needs when both judges have a finding with the same bare id",
    "manual": "plan mode: {id, judge, qualified_id, chunk_id, chapter_id, rule, severity, reason, "
    "excerpt, suggestion, message} for findings withheld from auto-apply (reason: no_suggestion | "
    "no_excerpt | suggestion_equals_excerpt | suggestion_not_literal | suggestion_placeholder ('N/A' "
    "and friends — the judge means 'no fix', so a swap would delete the line) | excerpt_not_found | "
    "excerpt_ambiguous | suggestion_restates_context | suggestion_adds_ellipsis (elides text it is "
    "not replacing) | suggestion_too_long | suggestion_too_short (a span rewrite does not grow or "
    "shrink that far — it is quoting or dropping prose) | suggestion_unbalanced_raya (the splice "
    "would leave a closing inciso raya with nothing opened) | mixed_register_remains (an "
    "inconsistent-address fix that leaves the form it replaces standing elsewhere in the chunk))",
    "chunks_without_findings": "target chunks with no persisted findings for any requested judge",
    "applied": "applied mode: fix ids that were actually applied",
    "already_applied": "applied mode: selected ids whose exact edit is already in the chunk, "
    "proved by a corrections_applied.jsonl row OR by a .chunk_edits/ snapshot. The snapshot path "
    "is what makes a retry after an interrupted run resume (the audit log may not have been "
    "written yet) instead of reporting the applied ids as manual/excerpt_not_found",
    "manual_ids": "error: selected ids the plan classified as manual (see manual[].reason)",
    "unknown_ids": "error: selected ids that match no finding in scope",
    "ambiguous_ids": "error: bare selected ids that exist for more than one requested judge — "
    "re-select them as '<judge>:<chunk_id>#<i>'",
    "failed": "applied mode: selected fix ids that did not locate (omitted when empty)",
    "chapters_realigned": "applied mode: chapters recombined + realigned. Includes chapters "
    "realigned as *repair* — an already-applied edit whose alignment is older than its chunks "
    "means an earlier run was interrupted before its realign step (see warnings)",
    "chapters_pending_realign": "--no-realign: chapters whose chapters/*.txt and alignment are "
    "now stale and must be realigned later ('apply --realign-only'), else null",
    "epub": "applied mode: rebuilt EPUB path, or null if not requested / nothing changed",
    "stale_marked": "applied mode: chunks whose persisted evaluation was stale-stamped. The flag "
    "is written at the TOP LEVEL of evaluations/<chunk>.json (stale, stale_since, stale_reason) — "
    "not inside judges[<judge>] beside that judge's score/issues — and stale_reason is "
    "single-valued, so a chunk edited by two judges' applies names only the most recent",
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
            prose = (amap.content or "").strip() or (amap.global_rules or "").strip()
            if prose:
                context["address_map"] = prose
                address_map_loaded = True
            elif "address" in judge_names:
                return context, (
                    f"address_map.json at {map_path} has empty content and "
                    "global_rules — the address judge has nothing to check against. "
                    "Re-draft with non-empty `content`, then:\n"
                    f"  python scripts/harness.py address-map commit --project {project_dir.name}"
                )
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
                help="Target scope: 'chunk:<chunk_id>', 'chapter:<chapter_id>' or 'book'. "
                "Repeatable — pass --scope multiple times to stage several chapters "
                "in one manifest for a single commit.",
            )
        else:
            p.add_argument(
                "--scope",
                required=True,
                help="Target scope: 'chunk:<chunk_id>', 'chapter:<chapter_id>' or 'book'",
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
    pp.add_argument(
        "--quiet",
        action="store_true",
        help="Omit the manifest echo from stdout, keeping manifest_path + usage_summary. "
        "The manifest was just written to disk and `fanout` reads it from there, so the "
        "echo (4 absolute paths per entry) is pure duplication on the headless path. Task "
        "workers need the paths — don't use --quiet for that branch.",
    )
    pp.add_argument("--verbose", action="store_true", help="Debug logging")

    # fanout — subagent backend, headless wave (opt-in) ---------------------
    fp = sub.add_parser(
        "fanout",
        help="Headless CLI wave for prepare drafts (opt-in; no Task workers)",
    )
    fp.add_argument("--project", required=True, help="Project id (under projects/) or path")
    fp.add_argument(
        "--target-ids",
        dest="target_ids",
        default=None,
        help="Comma-separated target_id / batch_id values to run "
        "(default: all manifest entries still lacking a draft)",
    )
    fp.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max parallel headless CLI processes per wave (default: manifest batch_size)",
    )
    fp.add_argument(
        "--cli",
        dest="cli",
        default=None,
        choices=["claude", "cursor"],
        help="Headless CLI family (default: config headless_cli, else claude)",
    )
    fp.add_argument(
        "--cli-bin",
        dest="cli_bin",
        default=None,
        help="Headless CLI binary override (default: claude or cursor-agent)",
    )
    fp.add_argument(
        "--claude-bin",
        dest="claude_bin",
        default=None,
        help="Back-compat alias for --cli-bin (Claude profile)",
    )
    fp.add_argument(
        "--estimate",
        action="store_true",
        help="Project the wave's token cost from the measured per-job baseline and "
        "print the argv, without spawning anything. Use it to make the usage gate "
        "quote the headless path rather than the API price of the path declined.",
    )
    fp.add_argument(
        "--effort",
        dest="effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "default"],
        help="Per-run Claude --effort override (default: config "
        "headless_effort_judges, else medium; 'default' emits no --effort flag)",
    )
    fp.add_argument(
        "--prompt-cache",
        dest="prompt_cache",
        default=None,
        choices=["auto", "5m", "1h", "off"],
        help="Per-run Claude prompt-cache TTL (default: config headless_prompt_cache / "
        "auto). auto picks 5m|1h|off from job shapes; off disables caching",
    )
    fp.add_argument("--verbose", action="store_true", help="Debug logging")

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
        action="append",
        default=None,
        metavar="JUDGE",
        help="Judge whose persisted findings to apply (default: dialogue). Repeatable: pass "
        "--judge twice to apply both judges in one run, which realigns once and re-checks each "
        "judge's excerpts against the text the earlier judge left behind. With more than one "
        "judge, --select needs the '<judge>:<chunk_id>#<i>' form for any bare id both judges "
        "have.",
    )
    ap.add_argument(
        "--scope",
        required=True,
        action="append",
        metavar="SCOPE",
        help="Target scope: 'chunk:<chunk_id>', 'chapter:<chapter_id>' or 'book' (the whole "
        "project). Repeatable.",
    )
    ap.add_argument(
        "--select",
        default=None,
        help="Comma-separated fix ids (from the plan's applicable[].id or .qualified_id) to "
        "apply. Omit to preview the plan without changing anything.",
    )
    ap.add_argument(
        "--rebuild-epub",
        dest="rebuild_epub",
        action="store_true",
        help="Rebuild the EPUB after applying (implies recombine + realign; "
        "incompatible with --no-realign)",
    )
    ap.add_argument(
        "--no-realign",
        dest="no_realign",
        action="store_true",
        help="Apply the edits but skip recombine + realign, which loads a BERT model per "
        "chapter. The owed chapters come back in chapters_pending_realign; settle them with "
        "--realign-only. Cannot be combined with --rebuild-epub (the EPUB builds from "
        "chapters/).",
    )
    ap.add_argument(
        "--realign-only",
        dest="realign_only",
        action="store_true",
        help="Change no text: recombine + realign every chapter in scope whose alignment is "
        "older than its chunks. The repair path for an apply that was interrupted before its "
        "realign step, and the way to settle a --no-realign debt. Add --dry-run to see what "
        "would be realigned.",
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

    # --schema on every subcommand. The blocks cost real tokens on every call
    # (_APPLY_SCHEMA alone is ~910), so they are opt-in on success — and, per
    # _emit, automatic on any error, where the caller most needs the shape.
    for subparser in sub.choices.values():
        subparser.add_argument(
            "--schema",
            action="store_true",
            help="Include the _schema block documenting every output key (omitted "
            "from successful output by default; always present on errors)",
        )

    return parser


# Set from ``--schema`` in main(). Off by default: see _emit.
_SHOW_SCHEMA = False


def _emit(payload: dict, schema: dict | None = None) -> None:
    """Print exactly one JSON object, with ``_schema`` only where it earns its keep.

    The schema blocks are the CLI's self-documentation and they are not free:
    ``_APPLY_SCHEMA`` is ~910 tokens and used to be re-sent on every invocation —
    twice per apply session (plan, then the real run), where it was ~52% of the
    payload an agent had to read. So it is emitted on ``--schema``, and on any
    error, where a caller most needs to know the shape it is looking at. Success
    payloads carry a one-line pointer instead.

    Some payloads arrive with ``_schema`` already embedded by the subagent layer;
    that is normalized here so there is one rule, not two.
    """
    embedded = payload.pop("_schema", None)
    schema = schema if schema is not None else embedded
    if schema is None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if _SHOW_SCHEMA or payload.get("status") == "error" or payload.get("error"):
        payload["_schema"] = schema
    else:
        payload["_schema_hint"] = "re-run with --schema for per-key documentation"
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
            # Basenames + one directory, matching `commit` — N absolute paths
            # sharing a 95-char prefix is duplication the caller pays for.
            "persisted_dir": str(project_dir / "evaluations") if args.persist else None,
            "persisted": [Path(p).name for p in persisted] if persisted is not None else None,
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
    if args.quiet:
        payload["manifest_entries"] = len(payload.pop("manifest", []))
    _emit(payload)
    return 0


def _cmd_fanout(args: argparse.Namespace) -> int:
    """Headless CLI wave for prepare drafts (opt-in; no Task workers)."""
    project_dir = _resolve_project(args.project)
    target_ids = None
    if args.target_ids:
        target_ids = [t.strip() for t in args.target_ids.split(",") if t.strip()]
    payload = subagent.fanout(
        project_dir,
        target_ids=target_ids,
        concurrency=args.concurrency,
        cli=args.cli,
        cli_bin=args.cli_bin,
        claude_bin=args.claude_bin,
        estimate=args.estimate,
        effort=getattr(args, "effort", None),
        cache=getattr(args, "prompt_cache", None),
    )
    payload["project"] = str(project_dir)
    _emit(payload)
    return 1 if payload.get("error") else 0


def _cmd_commit(args: argparse.Namespace) -> int:
    """Subagent backend phase 2: collect worker drafts, parse, optionally persist."""
    project_dir = _resolve_project(args.project)
    payload = subagent.commit(project_dir, persist=args.persist)
    payload["project"] = str(project_dir)
    _emit(payload)
    return 1 if payload.get("status") == "error" else 0


def _find_all(haystack: str, needle: str) -> list[int]:
    """Every start offset of ``needle`` in ``haystack`` (empty list if absent)."""
    out: list[int] = []
    i = haystack.find(needle)
    while i != -1:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _restated_after_splice(text: str, records: list[dict]) -> str | None:
    """Post-splice backstop: does ``text`` now repeat prose at a spliced ``new``?

    ``classify_fix`` already rejects suggestions that restate adjacent context,
    but it judges each fix alone against the *pre-edit* text. Two things can
    still slip past it: several selected fixes on one chunk interacting at a
    replacement *boundary*, and ``_resolve_correction_span``'s anchored/
    first-match fallback landing on a span the classifier never validated.
    This re-checks every occurrence of each applied ``corrected_es`` (not just
    the one nearest the pre-edit hint), measuring boundary overlap only — it
    does not scan for interior / between-fix duplication away from those
    edges. Returns the repeated words, or ``None`` when the result is clean.
    """
    from src.judges.fixes import restated_context

    for rec in records:
        new = rec.get("corrected_es") or ""
        old = rec.get("original_es") or ""
        if not new:
            continue
        # Score every hit: the pre-edit offset hint can point at a twin of
        # ``new`` that was already in the chunk while the real splice landed
        # elsewhere (fallback span). Missing that occurrence would green-light
        # a corrupt write.
        for start in _find_all(text, new):
            repeated = restated_context(text, start, start + len(new), new, baseline=old)
            if repeated:
                return repeated
    return None


def _desired_state_holds(chunk_text: str, excerpt: str, suggestion: str) -> bool:
    """True if the chunk already reads the way this fix wants it to.

    A short ``suggestion`` that merely occurs somewhere (``el``, ``dijo``) is not
    enough — it must appear exactly once, and when the excerpt is not itself a
    substring of the suggestion the excerpt must be gone. On its own this cannot
    distinguish "the edit was made" from "the judge's excerpt was never in the
    text and the suggestion happens to occur once", which is why callers also
    require proof that the excerpt once existed.
    """
    if not suggestion:
        return False
    if chunk_text.count(suggestion) != 1:
        return False
    if excerpt and excerpt not in suggestion and excerpt in chunk_text:
        return False
    return True


def _archive_has_edit(
    archived: list[dict], chunk_id: str, excerpt: str, suggestion: str
) -> bool:
    """True if ``corrections_applied.jsonl`` records this exact swap."""
    return any(
        rec.get("chunk_id") == chunk_id
        and (rec.get("original_es") or "").strip() == excerpt
        and (rec.get("corrected_es") or "").strip() == suggestion
        for rec in archived
    )


def _snapshot_proves_edit(
    project_dir: Path, chunk_id: str, excerpt: str, suggestion: str
) -> bool:
    """True if a pre-edit snapshot shows this edit had not been made yet.

    ``.chunk_edits/<chapter>/<chunk>/<ts>.json`` is a verbatim copy of the chunk
    file taken *before* each edit, so a snapshot holding ``excerpt`` but not
    ``suggestion`` proves the excerpt really was in the book and that whatever
    now stands in its place got there by an edit. That is the same thing an
    archive row proves — but the snapshot is written *before* the chunk is saved,
    whereas the archive row is written after, so this is the proof that survives
    a run killed mid-apply. Recovering that case by hand is what the 2026-07-29
    pollyanna friction log (item 1) was about.
    """
    if not excerpt or not suggestion:
        return False
    for snap in sorted((project_dir / ".chunk_edits").glob(f"*/{chunk_id}/*.json")):
        try:
            payload = json.loads(snap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        text = payload.get("translated_text") if isinstance(payload, dict) else None
        if isinstance(text, str) and excerpt in text and suggestion not in text:
            return True
    return False


def _alignment_is_stale(project_dir: Path, chapter_id: str, chunk_ids: list[str]) -> bool:
    """True if ``alignments/<chapter>.json`` is missing or older than these chunks.

    ``realign_chapter`` writes that file last, so its mtime is the receipt that
    an apply's expensive tail (recombine -> realign) actually ran. A chunk file
    newer than the alignment means an edit landed and the tail did not — either
    because the run was killed, or because it was told ``--no-realign``.
    """
    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    if not align_path.exists():
        return True
    try:
        aligned_at = align_path.stat().st_mtime
    except OSError:
        return True
    for chunk_id in chunk_ids:
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        try:
            if chunk_path.stat().st_mtime > aligned_at:
                return True
        except OSError:
            continue
    return False


def _write_chunk_snapshot(
    project_dir: Path, chapter_id: str, chunk_id: str, chunk_path: Path, ts: str
) -> Path:
    """Copy the pre-edit chunk into ``.chunk_edits/`` (web-UI editor convention).

    Must be called before the edited chunk is saved: these snapshots are the only
    complete record of the pre-edit text, and the sole reason an interrupted
    apply is recoverable. Keeps the last 10 per chunk.
    """
    backup_root = project_dir / ".chunk_edits" / chapter_id / chunk_id
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{ts}.json"
    backup_path.write_text(chunk_path.read_text(encoding="utf-8"), encoding="utf-8")
    for old_backup in sorted(backup_root.glob("*.json"))[:-10]:
        try:
            old_backup.unlink()
        except OSError:
            pass
    return backup_path


def _load_archived_records(project_dir: Path) -> list[dict]:
    """Read ``corrections_applied.jsonl`` (best effort — it is an audit log)."""
    from src.corrections_apply import CORRECTIONS_APPLIED_FILENAME

    path = project_dir / CORRECTIONS_APPLIED_FILENAME
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _cmd_apply(args: argparse.Namespace) -> int:
    """Apply user-approved judge findings to chunk text (careful, plan-first).

    Plan-first: without ``--select`` (or with ``--dry-run``) it only *reports*
    which persisted findings are a clean, uniquely-locatable text swap
    (``applicable``) and which are withheld (``manual``) — nothing is written.
    With ``--select`` it applies only the chosen ids, reusing the reader-
    corrections pipeline (backup -> edit -> archive -> recombine -> realign) so
    the edit is logged exactly like other chunk edits, then stale-marks the
    edited chunks' evaluations. The reader's ``corrections.jsonl`` queue is never
    touched.

    Crash consistency: each chunk's snapshot, edit, audit rows and stale stamp
    are finished for that chunk before the next is touched (sequential steps —
    not one fsync/transaction; the pre-edit snapshot is the recovery proof if a
    kill lands mid-sequence). A kill after a chunk finishes leaves a consistent
    *prefix* rather than edits nothing recorded. What a kill can still skip is
    the per-chapter tail (recombine -> realign -> EPUB); a later run detects
    that from the alignment mtime and finishes it. See ``_alignment_is_stale``
    and ``_snapshot_proves_edit``.
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

    judges: list[str] = list(dict.fromkeys(args.judge or ["dialogue"]))

    # 0. Flag combinations that cannot mean anything. These are emitted as JSON
    #    rather than raised through argparse, which would print usage to stderr
    #    and exit 2 — breaking the one-JSON-object contract every caller parses.
    conflict = None
    if args.no_realign and args.rebuild_epub:
        conflict = (
            "--no-realign cannot be combined with --rebuild-epub: the EPUB is built from "
            "chapters/, which recombine writes. Apply with --no-realign, then run "
            "'apply --realign-only --rebuild-epub'."
        )
    elif args.realign_only and args.select:
        conflict = "--realign-only changes no text, so it cannot take --select."
    elif args.realign_only and args.no_realign:
        conflict = "--realign-only and --no-realign are opposites; pass neither or one."
    if conflict:
        _emit({"status": "error", "error": conflict, "scopes": args.scope}, _APPLY_SCHEMA)
        return 1

    # 1. Resolve scopes -> unique, translated chunk targets (preserve order).
    targets: dict[str, object] = {}
    order: list[str] = []
    chapter_of: dict[str, str] = {}
    try:
        for scope in args.scope:
            for target in build_targets(project_dir, scope):
                if target.target_type != "chunk" or target.id in targets:
                    continue
                targets[target.id] = target
                order.append(target.id)
                chapter_of[target.id] = (
                    target.context.get("chapter_id") or target.id.rsplit("_chunk_", 1)[0]
                )
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scopes": args.scope}, _APPLY_SCHEMA)
        return 1

    def _chapters_in_order(chunk_ids: list[str]) -> dict[str, list[str]]:
        """chapter_id -> its chunk_ids, in scope order."""
        grouped: dict[str, list[str]] = {}
        for cid in chunk_ids:
            grouped.setdefault(chapter_of.get(cid, ""), []).append(cid)
        return grouped

    # 1b. ``--realign-only``: no findings needed, no text touched. This is the
    #     repair verb — for an apply interrupted before its realign step, and for
    #     settling a --no-realign debt.
    if args.realign_only:
        stale_chapters = [
            chapter_id
            for chapter_id, chunk_ids in _chapters_in_order(order).items()
            if _alignment_is_stale(project_dir, chapter_id, chunk_ids)
        ]
        if args.dry_run:
            _emit(
                {
                    "status": "ok", "mode": "realign", "project": str(project_dir),
                    "judge": None, "judges": [], "scopes": args.scope,
                    "chapters_realigned": [], "chapters_pending_realign": stale_chapters,
                    "epub": None, "warnings": None,
                },
                _APPLY_SCHEMA,
            )
            return 0
        epub_path = None
        with contextlib.redirect_stdout(sys.stderr):
            for i, chapter_id in enumerate(stale_chapters, start=1):
                print(f"[apply] realigning {chapter_id} ({i}/{len(stale_chapters)})")
                recombine_chapter(project_dir, chapter_id)
                realign_chapter(project_dir, chapter_id, args.source_lang, args.target_lang)
            if args.rebuild_epub and stale_chapters:
                epub_path = rebuild_epub(project_dir)
        _emit(
            {
                "status": "ok", "mode": "realign", "project": str(project_dir),
                "judge": None, "judges": [], "scopes": args.scope,
                "chapters_realigned": stale_chapters,
                "chapters_pending_realign": None,
                "epub": str(epub_path) if epub_path else None,
                "warnings": None if stale_chapters else ["Every chapter in scope was already "
                                                        "aligned; nothing to repair."],
            },
            _APPLY_SCHEMA,
        )
        return 0

    # 2. Classify every requested judge's persisted findings against the chunk's
    #    current text. Fix ids stay bare (``<chunk_id>#<i>``) so single-judge runs
    #    and every documented example are unchanged, and each finding also carries
    #    a ``qualified_id`` (``<judge>:<chunk_id>#<i>``) — with two judges the same
    #    bare id routinely exists for both, and guessing which one was meant is
    #    not something a text-rewriting command should do.
    applicable: dict[str, tuple[str, str, str, ProposedFix]] = {}
    applicable_list: list[dict] = []
    manual_list: list[dict] = []
    manual_qids: set[str] = set()
    # qualified_id -> everything needed to re-classify it later, to decide whether
    # a selected id is already applied, and to rebuild a lost audit row.
    seen_issues: dict[str, dict] = {}
    bare_index: dict[str, list[str]] = {}
    chunks_without: list[str] = []
    judges_with_findings: set[str] = set()
    warnings_out: list[str] = []

    for chunk_id in order:
        chapter_id = chapter_of[chunk_id]
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
        persisted = payload.get("judges") if isinstance(payload, dict) else None
        found_any = False
        for judge in judges:
            judge_entry = persisted.get(judge) if isinstance(persisted, dict) else None
            issues = judge_entry.get("issues") if isinstance(judge_entry, dict) else None
            if not issues:
                continue
            found_any = True
            judges_with_findings.add(judge)
            for i, issue in enumerate(issues):
                fid = f"{chunk_id}#{i}"
                qid = f"{judge}:{fid}"
                result = classify_fix(issue, translated_text)
                seen_issues[qid] = {
                    "id": fid, "judge": judge, "chunk_id": chunk_id,
                    "chapter_id": chapter_id, "issue": issue,
                    "excerpt": result.excerpt or "", "suggestion": result.suggestion or "",
                    "text": translated_text, "rule": result.rule,
                    "severity": result.severity, "message": result.message,
                }
                bare_index.setdefault(fid, []).append(qid)
                entry = {
                    "id": fid, "judge": judge, "qualified_id": qid,
                    "chunk_id": chunk_id, "chapter_id": chapter_id,
                    "rule": result.rule, "severity": result.severity,
                }
                if isinstance(result, ProposedFix):
                    applicable[qid] = (judge, chunk_id, chapter_id, result)
                    applicable_list.append(
                        {
                            **entry,
                            "old": result.excerpt, "new": result.suggestion,
                            "char_start": result.char_start, "char_end": result.char_end,
                        }
                    )
                else:
                    manual_qids.add(qid)
                    manual_list.append(
                        {
                            **entry,
                            "reason": result.reason, "excerpt": result.excerpt,
                            "suggestion": result.suggestion, "message": result.message,
                        }
                    )
        if not found_any:
            chunks_without.append(chunk_id)

    if not judges_with_findings:
        named = "'" + "', '".join(judges) + "'"
        _emit(
            {
                "status": "error",
                "error": f"No persisted {named} findings for the given scope. Run the "
                "judge with --persist first (run/commit) before applying.",
                "scopes": args.scope,
                "chunks_without_findings": chunks_without,
            },
            _APPLY_SCHEMA,
        )
        return 1

    single_judge = judges[0] if len(judges) == 1 else None

    def _report_id(value: str) -> str:
        """Echo an id back in the form the operator can re-select with.

        Bare while one judge is in play (unchanged for every existing caller),
        judge-qualified once more than one is, because that is then the only form
        that unambiguously names a finding.
        """
        if single_judge and value in seen_issues:
            return seen_issues[value]["id"]
        return value

    # 3. Plan mode — report, change nothing.
    if args.dry_run or not args.select:
        _emit(
            {
                "status": "ok", "mode": "plan", "project": str(project_dir),
                "judge": single_judge, "judges": judges, "scopes": args.scope,
                "applicable": applicable_list, "manual": manual_list,
                "chunks_without_findings": chunks_without,
            },
            _APPLY_SCHEMA,
        )
        return 0

    # 4. Apply mode — only the explicitly-selected, applicable ids.
    #
    # Resolve each selected token to one finding first. A qualified id names its
    # judge; a bare id is only usable while it belongs to exactly one of the
    # requested judges.
    selected_tokens = list(dict.fromkeys(s.strip() for s in args.select.split(",") if s.strip()))
    selected_qids: list[str] = []
    ambiguous_ids: list[str] = []
    unknown_ids: list[str] = []
    for token in selected_tokens:
        if token in seen_issues:  # already qualified — qids always contain ':'
            selected_qids.append(token)
            continue
        matches = bare_index.get(token, [])
        if len(matches) == 1:
            selected_qids.append(matches[0])
        elif matches:
            ambiguous_ids.append(token)
        else:
            unknown_ids.append(token)

    # A selected id that isn't applicable is one of three different situations,
    # and only two of them are errors. Check "already applied" first: an id whose
    # edit already landed classifies as ``excerpt_not_found`` (its old text is
    # gone, by design), so a plain manual-list test would report a completed
    # apply as a failure and invite the operator to re-do it.
    archived = _load_archived_records(project_dir)
    already_applied: list[str] = []
    # Already-applied ids the archive has no row for: the edit is in the book and
    # a snapshot proves we made it, but the run that made it died before writing
    # the audit log. Recover the row rather than leave the edit untraceable.
    recovered_qids: list[str] = []
    manual_ids: list[str] = []
    for qid in selected_qids:
        if qid in applicable:
            continue
        known = seen_issues[qid]
        excerpt, suggestion = known["excerpt"], known["suggestion"]
        if _desired_state_holds(known["text"], excerpt, suggestion):
            if _archive_has_edit(archived, known["chunk_id"], excerpt, suggestion):
                already_applied.append(qid)
                continue
            if _snapshot_proves_edit(project_dir, known["chunk_id"], excerpt, suggestion):
                already_applied.append(qid)
                recovered_qids.append(qid)
                continue
        if qid in manual_qids:
            manual_ids.append(qid)
        else:
            unknown_ids.append(qid)

    # A mixed select (good already_applied + one bad id) must still run the
    # repair path below — that is how an interrupted apply recovers. New edits
    # wait for a clean select; do not apply applicable ids alongside bad ones.
    bad_select = bool(manual_ids or unknown_ids or ambiguous_ids)
    pending_ids = (
        [] if bad_select else [qid for qid in selected_qids if qid in applicable]
    )

    # Group selected fixes by chunk, then by judge in the order the judges were
    # requested — one judge's edit can invalidate the next judge's excerpt in the
    # same chunk, so each judge's fixes are re-classified against the text the
    # previous judge left behind rather than against the plan's text.
    by_chunk: dict[str, dict] = {}
    for qid in pending_ids:
        judge, chunk_id, chapter_id, _fix = applicable[qid]
        entry = by_chunk.setdefault(chunk_id, {"chapter_id": chapter_id, "by_judge": {}})
        entry["by_judge"].setdefault(judge, []).append(qid)

    ts = time.strftime("%Y%m%dT%H%M%S")
    applied_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    applied_ids: list[str] = []
    failed_ids: list[str] = []
    affected_chapters: list[str] = []
    archive_path: Path | None = None
    stale_marked: list[str] = []
    backups: list[str] = []

    for chunk_id, entry in by_chunk.items():
        chapter_id = entry["chapter_id"]
        # Judge order comes from the --judge flags, never from the order ids were
        # pasted into --select: which judge edits the chunk first decides whose
        # excerpt can be superseded, so it must be a property of the command.
        judge_batches = [(j, entry["by_judge"][j]) for j in judges if j in entry["by_judge"]]
        chunk_qids = [qid for _judge, qids in judge_batches for qid in qids]
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not chunk_path.exists():
            failed_ids.extend(chunk_qids)
            warnings_out.append(f"{chunk_id}: chunk file missing")
            continue
        try:
            chunk = load_chunk(chunk_path)
        except Exception as exc:
            failed_ids.extend(chunk_qids)
            warnings_out.append(f"{chunk_id}: failed to load chunk ({exc})")
            continue

        chunk_records: list[dict] = []
        chunk_applied: list[str] = []
        judges_that_edited: list[str] = []

        for judge, qids in judge_batches:
            text = chunk.translated_text or ""
            items: list[tuple[str, ProposedFix]] = []
            for qid in qids:
                result = classify_fix(seen_issues[qid]["issue"], text)
                if isinstance(result, ProposedFix):
                    items.append((qid, result))
                else:
                    # Usually an earlier judge in this same run rewrote the span;
                    # can also fire if the chunk changed on disk mid-run. Never
                    # fall back to a loose search: that is how a fix lands wrong.
                    failed_ids.append(qid)
                    warnings_out.append(
                        f"{chunk_id}: {_report_id(qid)} ({judge}) no longer applies "
                        f"({result.reason}) — the excerpt is gone (superseded by "
                        "another edit); re-plan and re-select if it is still wanted"
                    )
            if not items:
                continue

            records = [
                to_correction_record(
                    fix, chunk_id=chunk_id, chapter_id=chapter_id,
                    project_id=project_dir.name, judge_name=judge,
                )
                for _qid, fix in items
            ]
            updated, applied_count, applied_indices = apply_to_chunk(chunk, records)
            applied_set = set(applied_indices)

            batch_failed = [items[i][0] for i in range(len(items)) if i not in applied_set]
            batch_applied = [items[i][0] for i in applied_indices]
            if batch_failed:
                failed_ids.extend(batch_failed)
                warnings_out.append(
                    f"{chunk_id}: {applied_count}/{len(records)} selected {judge} fixes located"
                )
            if applied_count == 0:
                continue

            # Never keep a chunk whose spliced text duplicates prose. Checked
            # against every record applied to this chunk so far, so a later
            # judge's batch cannot create a duplication at an earlier one's
            # boundary. Should be unreachable now that classify_fix rejects
            # restating suggestions; it exists so that if it ever isn't, the book
            # is not the thing that finds out.
            candidate = list(chunk_records) + [records[i] for i in applied_indices]
            repeated = _restated_after_splice(updated.translated_text or "", candidate)
            if repeated is not None:
                failed_ids.extend(batch_applied)
                warnings_out.append(
                    f"{chunk_id}: refused to save the {judge} fixes — the result repeats "
                    f"adjacent text ({repeated!r}); those fixes were dropped"
                )
                continue

            chunk = updated
            chunk_records.extend(records[i] for i in applied_indices)
            chunk_applied.extend(batch_applied)
            judges_that_edited.append(judge)

        if not chunk_records:
            continue

        # Everything this chunk needs, before the next chunk is touched:
        # snapshot (pre-edit) -> edit -> audit rows -> stale stamp. Sequential,
        # not atomic: a kill mid-sequence can leave this one chunk partial, but
        # the snapshot (written first) still proves the edit. A kill after this
        # block leaves a consistent prefix of finished chunks.
        backups.append(
            str(_write_chunk_snapshot(project_dir, chapter_id, chunk_id, chunk_path, ts))
        )
        save_chunk(chunk, chunk_path)
        archive_path = archive_applied_records(project_dir, chunk_records, applied_at=applied_at)
        if mark_evaluation_stale(
            project_dir, chunk_id,
            "translated_text edited by judge-review apply "
            f"({', '.join(judges_that_edited)})",
        ) is not None:
            stale_marked.append(chunk_id)

        applied_ids.extend(chunk_applied)
        if chapter_id not in affected_chapters:
            affected_chapters.append(chapter_id)
        # A killed run used to leave no trace at all on stdout (the JSON is
        # emitted only at the end). This is how far it got.
        print(
            f"[apply] {chunk_id}: {len(chunk_applied)} fix(es) written + archived",
            file=sys.stderr,
        )

    # 5. Repair pass. An id that was *already* applied, in a chapter whose
    #    alignment is older than its chunks, is the signature of a run that died
    #    between the edit and the recombine/realign tail — so finish that run's
    #    work instead of reporting a no-op over a half-finished book.
    resume_chapters: dict[str, list[str]] = {}
    for qid in already_applied:
        info = seen_issues[qid]
        resume_chapters.setdefault(info["chapter_id"], []).append(info["chunk_id"])
    for chapter_id, chunk_ids in resume_chapters.items():
        if chapter_id in affected_chapters:
            continue
        if not _alignment_is_stale(project_dir, chapter_id, chunk_ids):
            continue
        affected_chapters.append(chapter_id)
        if args.no_realign:
            warnings_out.append(
                f"{chapter_id}: the selected edits are already in the chunks, but the alignment "
                "is older than them — the recombine/realign tail never ran (an interrupted "
                "apply, or a prior --no-realign). Deferred again under --no-realign; settle "
                "with --realign-only (see chapters_pending_realign)."
            )
        else:
            warnings_out.append(
                f"{chapter_id}: the selected edits are already in the chunks, but the alignment "
                "is older than them — the recombine/realign tail never ran for this chapter "
                "(an interrupted apply, or --no-realign). Finishing it now: recombine, "
                "realign, re-stale-mark."
            )
        for chunk_id in dict.fromkeys(chunk_ids):
            evaluation = load_chunk_evaluation(project_dir, chunk_id)
            if isinstance(evaluation, dict) and evaluation.get("stale"):
                continue
            if mark_evaluation_stale(
                project_dir, chunk_id,
                "translated_text edited by judge-review apply "
                f"({', '.join(judges)}); stamped by a resumed run",
            ) is not None:
                stale_marked.append(chunk_id)

    # Recover audit rows for edits whose run died before writing them. Only for
    # ids the archive has no row for, so this can never duplicate a row.
    recovered_rows = [
        {
            "chunk_id": seen_issues[qid]["chunk_id"],
            "chapter_id": seen_issues[qid]["chapter_id"],
            "project_id": project_dir.name,
            "original_es": seen_issues[qid]["excerpt"],
            "corrected_es": seen_issues[qid]["suggestion"],
            "chunk_offset_start": None,
            "chunk_offset_end": None,
            "es_idx": None,
            "timestamp": applied_at,
            "source": f"judge:{seen_issues[qid]['judge']}",
            "rule": seen_issues[qid]["rule"],
            "severity": seen_issues[qid]["severity"],
            "message": seen_issues[qid]["message"],
            "recovered": True,
        }
        for qid in recovered_qids
    ]
    if recovered_rows:
        archive_path = archive_applied_records(project_dir, recovered_rows, applied_at=applied_at)
        warnings_out.append(
            f"{len(recovered_rows)} already-applied edit(s) had no audit row — an earlier run "
            "was interrupted before writing one. Recovered from the .chunk_edits/ snapshots and "
            "appended with \"recovered\": true: "
            + ", ".join(_report_id(qid) for qid in recovered_qids)
        )

    # 6-7. The aligner and the EPUB builder print progress to stdout. ``_emit``
    # runs after this block, so stdout stays exactly one JSON object and the
    # chatter is still visible on stderr.
    pending_realign: list[str] = []
    epub_path = None
    if args.no_realign:
        pending_realign = list(affected_chapters)
        affected_chapters = []
    with contextlib.redirect_stdout(sys.stderr):
        for i, chapter_id in enumerate(affected_chapters, start=1):
            print(f"[apply] realigning {chapter_id} ({i}/{len(affected_chapters)})")
            recombine_chapter(project_dir, chapter_id)
            realign_chapter(project_dir, chapter_id, args.source_lang, args.target_lang)

        if args.rebuild_epub and affected_chapters:
            epub_path = rebuild_epub(project_dir)

    if bad_select:
        _emit(
            {
                "status": "error",
                "error": "Selected id(s) are not in the applicable set (unknown, ambiguous "
                "across judges, or classified as manual). Re-check the plan's "
                "applicable[].id / .qualified_id. Already-applied ids still ran the "
                "repair path (audit recovery / resume realign) when owed.",
                "unknown_ids": [_report_id(v) for v in unknown_ids],
                "manual_ids": [_report_id(v) for v in manual_ids],
                "ambiguous_ids": ambiguous_ids,
                "already_applied": [_report_id(qid) for qid in already_applied],
                "applicable_ids": [_report_id(qid) for qid in applicable],
                "chapters_realigned": affected_chapters,
                "chapters_pending_realign": pending_realign or None,
                "epub": str(epub_path) if epub_path else None,
                "stale_marked": stale_marked,
                "archived_to": str(archive_path) if archive_path else None,
                "warnings": warnings_out or None,
            },
            _APPLY_SCHEMA,
        )
        return 1

    # Hard error only when nothing was already done and nothing newly applied.
    # A resume that realigned (or deferred) for already_applied ids is useful
    # work even if every still-pending id failed to locate — report that as
    # partial/ok below, not as a blank failure.
    if not applied_ids and pending_ids and not already_applied:
        _emit(
            {
                "status": "error",
                "mode": "applied",
                "error": "None of the selected fixes were applied — they did not locate in the "
                "current chunk text, or the result was rejected. See warnings.",
                "project": str(project_dir),
                "judge": single_judge,
                "judges": judges,
                "scopes": args.scope,
                "applied": [],
                "already_applied": [_report_id(qid) for qid in already_applied],
                "failed": [_report_id(qid) for qid in failed_ids],
                "chapters_realigned": affected_chapters,
                "chapters_pending_realign": pending_realign or None,
                "epub": str(epub_path) if epub_path else None,
                "stale_marked": stale_marked,
                "archived_to": str(archive_path) if archive_path else None,
                "backups": backups,
                "warnings": warnings_out or None,
            },
            _APPLY_SCHEMA,
        )
        return 1

    apply_status = "partial" if failed_ids else "ok"
    _emit(
        {
            "status": apply_status, "mode": "applied", "project": str(project_dir),
            "judge": single_judge, "judges": judges, "scopes": args.scope,
            "applied": [_report_id(qid) for qid in applied_ids],
            "already_applied": [_report_id(qid) for qid in already_applied],
            "failed": [_report_id(qid) for qid in failed_ids] or None,
            "chapters_realigned": affected_chapters,
            "chapters_pending_realign": pending_realign or None,
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
    "fanout": _cmd_fanout,
    "commit": _cmd_commit,
    "apply": _cmd_apply,
}


def main(argv: list[str] | None = None) -> int:
    global _SHOW_SCHEMA

    args = _build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    _SHOW_SCHEMA = bool(getattr(args, "schema", False))
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
