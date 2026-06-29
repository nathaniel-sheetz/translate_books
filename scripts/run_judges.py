#!/usr/bin/env python3
"""Non-interactive CLI for tailored LLM judges.

Run one judge or a named suite over a chunk or chapter, print findings as JSON,
and (optionally) persist them into ``evaluations/<chunk>.json`` so the web
dashboard badges pick them up.

Cost safety: the suite is cost-estimated up front. If the estimate exceeds
``--cost-limit`` (default $0.50) the command refuses to spend and returns
``status: "cost_exceeded"`` — re-run with ``--confirm`` to proceed.

Examples:
    python scripts/run_judges.py --project understood-betsy \\
        --judge dialogue --scope chapter:chapter_03
    python scripts/run_judges.py --project understood-betsy \\
        --suite default --scope chunk:chapter_03_chunk_000 --persist --confirm
"""

from __future__ import annotations

import argparse
import json
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
from src.judges.registry import available_judges  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA = {
    "status": "'ok' | 'cost_exceeded' | 'error'",
    "project": "resolved project directory",
    "scope": "the --scope argument that was resolved",
    "judges": "list of judge names that ran",
    "estimated_cost": "coarse USD pre-run estimate (guardrail, not an invoice)",
    "run_header": "reproducibility metadata: judge versions, prompt hashes, model, git_commit",
    "summary": "aggregate_results() rollup across all (judge x target) results",
    "results": "per (target, judge): target_id, judge, passed, score, issues[], metadata",
    "persisted": "evaluations/*.json paths written when --persist is set, else null",
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
    parser.add_argument("--project", required=True, help="Project id (under projects/) or path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--judge", help=f"Single judge to run (one of: {', '.join(available_judges())})")
    group.add_argument("--suite", help="Named suite of judges (e.g. 'default')")
    parser.add_argument(
        "--scope",
        required=True,
        help="Target scope: 'chunk:<chunk_id>' or 'chapter:<chapter_id>'",
    )
    parser.add_argument("--model", default=None, help="Judge model id override")
    parser.add_argument("--provider", default=None, help="Judge provider override")
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=0.50,
        help="Max estimated USD before --confirm is required (default 0.50)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Proceed even if the estimate exceeds --cost-limit",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write findings into evaluations/<chunk>.json (dashboard badges)",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    return parser


def _emit(payload: dict) -> None:
    payload["_schema"] = _SCHEMA
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    project_dir = _resolve_project(args.project)

    # Resolve which judges to run.
    try:
        judge_names = [args.judge] if args.judge else resolve_suite(args.suite)
    except ValueError as exc:
        _emit({"status": "error", "error": str(exc)})
        return 1

    # Resolve targets.
    try:
        targets = build_targets(project_dir, args.scope)
    except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc), "scope": args.scope})
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
                "project": str(project_dir),
                "scope": args.scope,
                "judges": judge_names,
                "estimated_cost": outcome["estimated_cost"],
                "cost_limit": outcome["cost_limit"],
                "message": outcome["message"],
            }
        )
        return 0

    results = outcome["results"]
    serialized = [r.model_dump(mode="json") for r in results]

    persisted: list[str] | None = None
    if args.persist:
        from web_ui.evaluations import merge_judge_result

        persisted = []
        for result, payload in zip(results, serialized):
            # Only chunk-keyed results map to an evaluations/<chunk>.json file.
            if result.target_type == "chunk":
                path = merge_judge_result(
                    project_dir, result.target_id, result.eval_name, payload
                )
                persisted.append(str(path))

    _emit(
        {
            "status": "ok",
            "project": str(project_dir),
            "scope": args.scope,
            "judges": judge_names,
            "estimated_cost": outcome["estimated_cost"],
            "run_header": outcome["run_header"],
            "summary": outcome["aggregated"],
            "results": serialized,
            "persisted": persisted,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
