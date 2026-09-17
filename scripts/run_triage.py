#!/usr/bin/env python3
"""
Triage one book's dictionary and grammar findings before a human reads them.

Those two checkers produce roughly 70% of all finding-clearing work and are
right about one time in ten. This asks a model, for each finding, whether the
flagged word is really a defect *in the sentence it sits in* — and records the
answer beside the finding rather than deleting anything.

Each subcommand prints one JSON object with a ``_schema`` block:

    prepare   render batched prompts + a manifest under .harness/triage  (no spend)
    fanout    run one headless wave over those jobs                      (subscription)
    commit    parse the drafts; append verdicts to _triage.jsonl         (no spend)

A run:

    python scripts/run_triage.py prepare --project five-little-peppers \\
        --worker-model "grok-4.6[effort=medium,fast=false]"
    python scripts/run_triage.py fanout  --project five-little-peppers
    python scripts/run_triage.py commit  --project five-little-peppers

The model is pinned at ``prepare`` and recorded in the manifest, so ``fanout``
inherits it instead of the book's default ``worker_model``. That is the whole
point of the pass having its own wave type: pointing it at a different model, or
later at local inference, never touches how the book is translated or judged.

``fanout`` skips jobs that already have a draft, so re-running it resumes.
Nothing here edits the book: a verdict suppresses a finding at read time, and
the finding stays in ``evaluations/<chunk_id>.json`` exactly as written.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every raya and
# guillemet in the JSON we print — and a reason quoting the prose is routine
# here. Guarded with hasattr to stay safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.harness import state as hstate  # noqa: E402
from src.triage import pass_ as triage  # noqa: E402


def _print(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "error" or payload.get("error") else 0


class ProjectNotFound(Exception):
    """``--project`` named nothing on disk. Carries the payload to print."""

    def __init__(self, arg: str):
        super().__init__(arg)
        self.payload = {
            "status": "error",
            "error": (
                f"Project not found: {arg!r} (looked for a directory, "
                f"projects/{arg}, and projects/*/{arg})."
            ),
        }


def _resolve_project(arg: str) -> Path:
    """Project dir for ``--project``: a path, a flat slug, or a nested one.

    ``hstate.resolve_project_dir`` is the one lookup, so a book filed under a
    grouping folder (``projects/.macdonald/photogen-nycteris``) answers to its
    bare slug here exactly as it does to ``harness.py``. Resolved, because
    ``prepare`` writes paths into the manifest and a later command run from
    another cwd must read the same files.

    Raises :class:`ProjectNotFound` rather than ``SystemExit`` with a string:
    ``SystemExit`` carrying a non-integer prints it and exits **0**, so a
    scripted run could not tell a missing book from a successful one. ``main``
    catches this and returns 1 through the same ``_print`` every other error
    uses.
    """
    try:
        return hstate.resolve_project_dir(arg).resolve()
    except FileNotFoundError:
        raise ProjectNotFound(arg) from None


def _parse_chapters(raw: str | None) -> list[str] | None:
    """``--chapters chapter_01,chapter_02`` -> a list, or ``None`` for all."""
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _cmd_prepare(args: argparse.Namespace) -> int:
    return _print(triage.prepare(
        _resolve_project(args.project),
        chapters=_parse_chapters(args.chapters),
        items_per_job=args.items_per_job,
        worker_model=args.worker_model,
        cli=args.cli,
        effort=args.effort,
    ))


def _cmd_fanout(args: argparse.Namespace) -> int:
    job_ids = (
        [j.strip() for j in args.job_ids.split(",") if j.strip()]
        if args.job_ids else None
    )
    return _print(triage.fanout(
        _resolve_project(args.project),
        concurrency=args.concurrency,
        job_ids=job_ids,
        worker_model=args.worker_model,
        cli=args.cli,
        effort=args.effort,
        cli_bin=args.cli_bin,
    ))


def _cmd_commit(args: argparse.Namespace) -> int:
    return _print(triage.commit(_resolve_project(args.project)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_triage.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser(
        "prepare", help="render prompts + manifest under .harness/triage (no spend)"
    )
    p_prepare.add_argument("--project", required=True, help="Project id (under projects/) or path")
    p_prepare.add_argument(
        "--chapters", default=None,
        help="comma-separated chapter ids (default: every chapter with an alignment)",
    )
    p_prepare.add_argument(
        "--items-per-job", type=int, default=triage.DEFAULT_ITEMS_PER_JOB,
        help=f"findings per prompt (default: {triage.DEFAULT_ITEMS_PER_JOB})",
    )
    p_prepare.add_argument(
        "--worker-model", default=None,
        help="pin the triage model, e.g. 'grok-4.6[effort=medium,fast=false]'. "
             "Recorded in the manifest; fanout inherits it. Default: the CLI's own default",
    )
    p_prepare.add_argument("--cli", choices=("claude", "cursor"), default=None, help="headless CLI")
    p_prepare.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh", "default"), default=None,
        help="default: headless_effort_triage, else medium",
    )

    p_fanout = sub.add_parser("fanout", help="run one headless wave over the prepared jobs")
    p_fanout.add_argument("--project", required=True, help="Project id (under projects/) or path")
    p_fanout.add_argument(
        "--concurrency", type=int, default=None,
        help=f"max parallel CLI processes (default: {triage.DEFAULT_CONCURRENCY})",
    )
    p_fanout.add_argument(
        "--job-ids", default=None,
        help="comma-separated job ids (default: every job without a draft)",
    )
    p_fanout.add_argument(
        "--worker-model", default=None,
        help="override the manifest's model; the manifest is rewritten so commit "
             "records what actually ran",
    )
    p_fanout.add_argument("--cli", choices=("claude", "cursor"), default=None, help="headless CLI")
    p_fanout.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh", "default"), default=None,
        help="default: inherited from the manifest",
    )
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")

    p_commit = sub.add_parser(
        "commit", help="parse drafts; append verdicts to _triage.jsonl (no spend)"
    )
    p_commit.add_argument("--project", required=True, help="Project id (under projects/) or path")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {
            "prepare": _cmd_prepare,
            "fanout": _cmd_fanout,
            "commit": _cmd_commit,
        }[args.command](args)
    except ProjectNotFound as exc:
        return _print(exc.payload)


if __name__ == "__main__":
    raise SystemExit(main())
