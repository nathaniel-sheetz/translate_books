#!/usr/bin/env python3
"""
Audit the reader's own edits with a panel of model families (Phase 0).

Reads the rows ``scripts/ledger_census.py --export`` writes. For each edit, every
panel model says whether it was an improvement, taste or a regression, names the
defect class, and offers what a native Mexican reader would write. Each
subcommand prints one JSON object with a ``_schema`` block:

    prepare   render batched prompts + a manifest into a new run dir  (no spend)
    fanout    run one headless wave per panel model                   (Cursor allotment)
    commit    parse the drafts; write results.jsonl + report.md       (no spend)

A pilot:

    python scripts/ledger_census.py --project fabre2 --export audit/pilot/input.jsonl
    python scripts/panel_audit.py prepare --input audit/pilot/input.jsonl \\
        --run audit/pilot/rows20 --limit 50 --exclude-ids-file audit/pilot/probe_ids.txt
    python scripts/panel_audit.py fanout --run audit/pilot/rows20
    python scripts/panel_audit.py commit --run audit/pilot/rows20

``fanout`` without ``--model`` runs every model on the run's panel in turn. It
skips jobs that already have a draft, so re-running it resumes. Nothing here
writes into ``projects/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every raya and
# guillemet in the JSON we print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.audit import panel  # noqa: E402


def _print(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "error" or payload.get("error") else 0


def _read_ids(path: str | None) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        exclude = _read_ids(args.exclude_ids_file)
    except OSError as exc:
        return _print({"status": "error", "error": f"unreadable --exclude-ids-file: {exc}"})
    return _print(panel.prepare(
        Path(args.input),
        Path(args.run),
        models=args.model or panel.PANEL_MODELS,
        rows_per_job=args.rows_per_job,
        projects=args.project,
        limit=args.limit,
        exclude_ids=exclude,
        projects_root=Path(args.projects_root) if args.projects_root else None,
    ))


def _cmd_fanout(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    if args.model:
        models = [args.model]
    else:
        manifest, error = panel.load_manifest(run_dir)
        if error:
            return _print({"status": "error", "error": error})
        models = manifest["models"]
    job_ids = [j.strip() for j in args.job_ids.split(",") if j.strip()] if args.job_ids else None

    waves = []
    schema = None
    for model in models:
        wave = panel.fanout(
            run_dir, model=model, concurrency=args.concurrency, job_ids=job_ids,
            cli=args.cli, cli_bin=args.cli_bin,
        )
        schema = wave.pop("_schema", schema)
        waves.append(wave)
        if wave.get("error"):
            break
    errored = any(w.get("error") for w in waves)
    return _print({
        "status": "error" if errored else "ok",
        "run_dir": str(run_dir),
        "waves": waves,
        "instructions": (
            "Fix the launcher error, then re-run fanout."
            if errored
            else f"Run `commit --run {run_dir}`; it lists any failed or missing jobs."
        ),
        "_schema": {"waves": "one fanout payload per model", **(schema or {})},
    })


def _cmd_commit(args: argparse.Namespace) -> int:
    return _print(panel.commit(Path(args.run)))


_DISPATCH = {"prepare": _cmd_prepare, "fanout": _cmd_fanout, "commit": _cmd_commit}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panel_audit.py",
        description="Audit the reader's edits with a panel of model families.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="render prompts + manifest into a new run dir (no spend)")
    p_prepare.add_argument("--input", required=True, help="audit rows from ledger_census.py --export")
    p_prepare.add_argument("--run", required=True, help="new or empty run directory, e.g. audit/<date>-pilot")
    p_prepare.add_argument(
        "--model", action="append",
        help=f"panel model; repeatable (default: {', '.join(panel.PANEL_MODELS)})",
    )
    p_prepare.add_argument(
        "--rows-per-job", type=int, default=panel.DEFAULT_ROWS_PER_JOB,
        help=f"edits per prompt (default: {panel.DEFAULT_ROWS_PER_JOB})",
    )
    p_prepare.add_argument("--project", action="append", help="only this book's rows, by slug; repeatable")
    p_prepare.add_argument("--limit", type=int, default=None, help="first N rows by audit_id, after filters")
    p_prepare.add_argument(
        "--exclude-ids-file", default=None,
        help="audit_ids to leave out, one per line (# comments allowed)",
    )
    p_prepare.add_argument("--projects-root", default=None, help="directory holding the books (default: projects/)")

    p_fanout = sub.add_parser("fanout", help="run one headless wave per panel model (Cursor allotment)")
    p_fanout.add_argument("--run", required=True, help="run directory from prepare")
    p_fanout.add_argument("--model", default=None, help="one panel model (default: every model in turn)")
    p_fanout.add_argument(
        "--concurrency", type=int, default=None,
        help=f"max parallel CLI processes (default: {panel.DEFAULT_CONCURRENCY})",
    )
    p_fanout.add_argument("--job-ids", default=None, help="comma-separated job ids (default: every undrafted job)")
    p_fanout.add_argument("--cli", choices=("cursor", "claude"), default="cursor", help="headless CLI (default: cursor)")
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")

    p_commit = sub.add_parser("commit", help="parse drafts; write results.jsonl + report.md (no spend)")
    p_commit.add_argument("--run", required=True, help="run directory from prepare")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
