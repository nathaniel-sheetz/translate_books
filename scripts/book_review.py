#!/usr/bin/env python3
"""
Read one book's judge findings together and report what is systemic.

A single judge finding says one sentence is wrong. Read across a whole book,
the findings say more: a glossary entry that never reached the translator, a
style guide that contradicts the address map, a rule the translator keeps
missing, a judge whose findings keep being dismissed. This command runs the
coded detectors in ``src/book_review/signals.py`` over every dialogue, address
and editorial finding the book has, and writes a report naming those patterns
with the findings behind each.

Each subcommand prints one JSON object with a ``_schema`` block:

    status    coverage, owed chapters, last review   (no spend, no writes)
    judge     run every judge over the chapters that owe it, then editorial
              pass 2: --estimate prepares and projects tokens (no spawns),
              --yes runs the headless wave (subscription)
    signals   run the coded detectors; write signals.json + a report  (no spend)
    skip      mark the current translations reviewed without reviewing them,
              so the harness stops routing here until new chunks land

    python scripts/book_review.py status  --project pollyanna
    python scripts/book_review.py judge   --project pollyanna --estimate
    python scripts/book_review.py judge   --project pollyanna --yes
    python scripts/book_review.py signals --project pollyanna

Nothing here edits the book or deletes a finding.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.book_review import judging, progress, review_dir  # noqa: E402
from src.book_review.corpus import load_book  # noqa: E402
from src.book_review.report import write_signals  # noqa: E402
from src.book_review.signals import coverage, run_all  # noqa: E402
from src.harness import state as hstate  # noqa: E402

_STATUS_SCHEMA = {
    "status": "'ok' | 'error'",
    "project": "project directory name",
    "findings_by_judge": "judge findings on disk (kept, retracted and filtered), per judge",
    "coverage": "{address_map, judges: {<judge>: {state, owed, done, last_run}}, "
    "unverified_editorial_chunks}: what has not been judged yet",
    "last_signals": "{path, generated_at} of the last signals.json, or null",
    "instructions": "next step",
}

_SIGNALS_SCHEMA = {
    "status": "'ok' | 'error'",
    "project": "project directory name",
    "findings": "judge findings read",
    "counts": "signals per detector",
    "headline": "the highest-scoring signals, one line each, worst first",
    "signals_path": "full detector output (JSON)",
    "report_path": "the markdown report to show the operator",
    "coverage": "same shape as status.coverage",
    "instructions": "next step",
}

_JUDGE_SCHEMA = {
    "status": "'ok' | 'needs_confirm' | 'error'",
    "plan": "{judges, address_map, owed: {judge: [chapter ids]}, owed_chunks, "
    "pending_pass2_chunks, nothing_to_do}",
    "effective": "the profile the wave runs under (cli, worker_model, effort, ...) with provenance",
    "pass1": "--estimate only: {jobs, prompt_tokens, projected_tokens, output_tokens, "
    "baseline_tokens, ...} for the judge wave",
    "adjudication": "--estimate only: editorial pass 2 ceiling {jobs_max, tokens_max, "
    "baseline_tokens, baseline_source}; pass 2 cannot be measured before pass 1 runs",
    "summary": "--yes only: {ran, total, wrote, failed, missing, adjudication, error_count, "
    "errors, usage, effective}",
    "instructions": "next step",
}

HEADLINE = 10

# Upstream causes first: a conflict or a dead glossary key is one fix for many
# findings, while a plain recurring cluster may only be a rule written after
# the chapters it is judged against.
_DETECTOR_RANK = {
    "address_style_conflicts": 0,
    "dead_glossary_keys": 1,
    "false_positive_clusters": 2,
    "recurring_rules": 3,
}
_LABEL_RANK = {"resisted": 0, "disputed": 1, "recurring": 2, "unreviewed": 3}


def _headline_rank(signal: dict) -> tuple:
    detector = signal["detector"]
    label = (signal.get("metrics") or {}).get("label")
    if detector == "dead_glossary_keys" and (signal.get("metrics") or {}).get("state") == "absent":
        return (9, 0, 0)
    return (_DETECTOR_RANK.get(detector, 5), _LABEL_RANK.get(label, 0), -signal["score"])


def _print(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "error" else 0


def _resolve_project(arg: str) -> Path:
    try:
        return hstate.resolve_project_dir(arg).resolve()
    except FileNotFoundError:
        raise SystemExit(_print({
            "status": "error",
            "error": f"Project not found: {arg!r} (looked for a directory, projects/{arg}, "
                     "and projects/*/{arg}).",
        }))


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    corpus = load_book(project_dir)
    signals_path = review_dir(project_dir) / "signals.json"
    last = None
    if signals_path.exists():
        last = {"path": str(signals_path), "generated_at": signals_path.stat().st_mtime}
    counts: dict[str, int] = {}
    for f in corpus.findings:
        counts[f.judge] = counts.get(f.judge, 0) + 1
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "findings_by_judge": counts,
        "coverage": coverage(corpus),
        "last_signals": last,
        "instructions": "Run `signals` to write the coded report (free, no model).",
        "_schema": _STATUS_SCHEMA,
    })


def _cmd_judge(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    options = dict(judges=args.judge, cli=args.cli, worker_model=args.worker_model,
                   effort=args.effort, cache=args.prompt_cache)
    if args.yes:
        out = judging.run(project_dir, **options)
        out["instructions"] = "Run `signals` next to read the new findings."
    elif args.estimate:
        out = judging.estimate(project_dir, **options)
        out["instructions"] = (
            "Relay plan, pass1 and adjudication as one consent gate and stop. "
            "On approval run `judge --yes` with the same flags."
        )
    else:
        out = {"status": "needs_confirm", "plan": judging.plan(project_dir, judges=args.judge),
               "instructions": "Run with --estimate for a token projection, then --yes."}
    out["project"] = project_dir.name
    out["_schema"] = _JUDGE_SCHEMA
    return _print(out)


def _cmd_signals(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    corpus = load_book(project_dir)
    result = run_all(corpus)
    paths = write_signals(corpus, result)
    progress.record_review(project_dir, kind="signals", report_path=paths["report_path"])
    counts: dict[str, int] = {}
    for s in result["signals"]:
        counts[s["detector"]] = counts.get(s["detector"], 0) + 1
    ranked = sorted(result["signals"], key=_headline_rank)[:HEADLINE]
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "findings": result["findings"],
        "counts": counts,
        "headline": [s["title"] for s in ranked],
        **paths,
        "coverage": result["coverage"],
        "instructions": "Show the operator the headline and the report path.",
        "_schema": _SIGNALS_SCHEMA,
    })


def _cmd_skip(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    recorded = progress.record_review(project_dir, kind="skipped")
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "recorded": recorded,
        "instructions": "The router moves on until new chunks are translated. To stop "
        "it for good: harness.py config-set --key book_review_after_translate --value off "
        "(and book_review_canary).",
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="book_review.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "coverage, owed chapters, last review (no spend, no writes)"),
        ("signals", "run the coded detectors; write signals.json + a report (no spend)"),
        ("skip", "mark the current translations reviewed without reviewing them"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    judge = sub.add_parser(
        "judge", help="run every judge over owed chapters, then editorial pass 2 (subscription)"
    )
    judge.add_argument("--project", required=True, help="Project id (under projects/) or path")
    gate = judge.add_mutually_exclusive_group()
    gate.add_argument("--estimate", action="store_true",
                      help="prepare and project tokens; spawns nothing")
    gate.add_argument("--yes", action="store_true", help="run the headless wave")
    judge.add_argument("--judge", action="append", choices=judging.SWEEP_JUDGES, default=None,
                       help="limit to these judges (repeatable; default: all the book supports)")
    judge.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None, help="headless CLI")
    judge.add_argument("--worker-model", default=None, help="pin the judge model")
    judge.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "default"),
                       default=None, help="default: headless_effort_judges")
    judge.add_argument("--prompt-cache", dest="prompt_cache", default=None,
                       choices=["auto", "5m", "1h", "off"], help="Claude prompt-cache TTL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "status": _cmd_status,
        "judge": _cmd_judge,
        "signals": _cmd_signals,
        "skip": _cmd_skip,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
