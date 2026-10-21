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
    signals   run the coded detectors; write signals.json + a report  (no spend)

    python scripts/book_review.py status  --project pollyanna
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

from src.book_review import review_dir  # noqa: E402
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


def _cmd_signals(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    corpus = load_book(project_dir)
    result = run_all(corpus)
    paths = write_signals(corpus, result)
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
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"status": _cmd_status, "signals": _cmd_signals}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
