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
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

    def add_select(p: argparse.ArgumentParser) -> None:
        """Project + judge/suite + scope + model/provider — shared by run & prepare."""
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--judge", help=f"Single judge to run (one of: {', '.join(available_judges())})"
        )
        group.add_argument("--suite", help="Named suite of judges (e.g. 'default')")
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
    add_select(pp)
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

    context: dict = {"judge_model": args.model, "judge_provider": args.provider}
    style_path = project_dir / "style.json"
    if style_path.exists():
        context["style_json_path"] = style_path

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
        _emit({"status": "error", "error": str(exc)})
        return 1

    context = {"judge_model": args.model, "judge_provider": args.provider}
    try:
        payload = subagent.prepare(
            project_dir,
            judge_names,
            args.scope,
            context=context,
            worker_model=args.worker_model,
            batch_size=args.batch_size,
        )
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scope": args.scope})
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
    return 0


_DISPATCH = {
    "run": _cmd_run,
    "prepare": _cmd_prepare,
    "commit": _cmd_commit,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
