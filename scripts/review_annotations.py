#!/usr/bin/env python3
"""
Review the annotations a reader left on a finished book.

Post-human-review counterpart to ``run_judges.py``: instead of checking the
translation against house rules, this reads the reader's own notes from
``annotations.jsonl``, researches each against the style guide, glossary and the
whole book, and drafts a resolution — a recommendation for ``word_choice``, a
book-wide verdict for ``inconsistency``, an actual gloss for ``footnote``, an
investigation for ``flag`` ("Other").

Subcommands, each printing exactly one JSON object with a ``_schema`` block:

    prepare   render one prompt per annotation + a manifest    (no spend)
    fanout    run a headless claude/cursor wave over them      (no API spend)
    commit    parse drafts, write results.json + the report    (no spend)
    run       the API backend, behind a dollar cost gate       (metered)
    apply     write reviewed notes back into annotations.jsonl (the only writer)

Typical no-spend flow:

    python scripts/review_annotations.py prepare --project fabre2
    python scripts/review_annotations.py fanout  --project fabre2
    python scripts/review_annotations.py commit  --project fabre2
    python scripts/review_annotations.py apply   --project fabre2 --dry-run
    python scripts/review_annotations.py apply   --project fabre2 --select <keys>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every raya/guillemet/accent in the JSON we print. Force UTF-8 so annotation
# text survives on every platform. The hasattr guard keeps this safe under
# pytest's captured streams, which lack ``reconfigure``.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time so it can't corrupt the JSON an agent parses.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from src.annotations import review  # noqa: E402
from src.annotations.store import ANNOTATION_TYPES  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_project(arg: str) -> Path:
    """Accept a project id or a path; exit with JSON on failure."""
    candidate = Path(arg)
    if candidate.is_dir():
        return candidate
    resolved = _REPO_ROOT / "projects" / arg
    if resolved.is_dir():
        return resolved
    raise SystemExit(
        json.dumps(
            {
                "status": "error",
                "error": f"project not found: {arg!r} (looked for a directory and projects/{arg})",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _emit(payload: dict, schema: dict | None = None) -> None:
    if schema is not None and "_schema" not in payload:
        payload["_schema"] = schema
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_types(raw: str | None) -> list[str] | None:
    """Parse --type into a validated list, or None for all four."""
    if not raw:
        return None
    wanted = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in ANNOTATION_TYPES]
    if unknown:
        raise SystemExit(
            json.dumps(
                {
                    "status": "error",
                    "error": f"unknown annotation type(s): {unknown}; expected any of {list(ANNOTATION_TYPES)}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return wanted


def _parse_chapters(values: list[str] | None) -> list[str] | None:
    """Parse repeated --scope chapter:<id> into chapter ids, or None for the book."""
    if not values:
        return None
    chapters: list[str] = []
    for value in values:
        kind, sep, rest = value.partition(":")
        if not sep or kind.strip().lower() != "chapter":
            raise SystemExit(
                json.dumps(
                    {
                        "status": "error",
                        "error": f"malformed scope {value!r}; expected 'chapter:<chapter_id>'",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        chapters.append(rest.strip())
    return chapters


def _add_select(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="project id or path")
    parser.add_argument(
        "--type",
        dest="types",
        help=f"comma-separated annotation types to include (default: all — {', '.join(ANNOTATION_TYPES)})",
    )
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        help="chapter:<chapter_id>, repeatable (default: the whole book)",
    )
    parser.add_argument(
        "--target-language",
        help="override the book's target language (default: .harness/config.json)",
    )


def _cmd_prepare(args: argparse.Namespace) -> int:
    out = review.prepare(
        _resolve_project(args.project),
        types=_parse_types(args.types),
        chapters=_parse_chapters(args.scopes),
        worker_model=args.worker_model,
        batch_size=args.batch_size,
        keep_drafts=args.keep_drafts,
        target_language=args.target_language,
    )
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_fanout(args: argparse.Namespace) -> int:
    target_ids = (
        [t.strip() for t in args.target_ids.split(",") if t.strip()]
        if args.target_ids
        else None
    )
    out = review.fanout(
        _resolve_project(args.project),
        target_ids=target_ids,
        concurrency=args.concurrency,
        cli=args.cli,
        cli_bin=args.cli_bin,
    )
    _emit(out)
    return 1 if out.get("error") else 0


def _cmd_commit(args: argparse.Namespace) -> int:
    out = review.commit(_resolve_project(args.project), report=not args.no_report)
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_run(args: argparse.Namespace) -> int:
    out = review.run(
        _resolve_project(args.project),
        types=_parse_types(args.types),
        chapters=_parse_chapters(args.scopes),
        model=args.model,
        provider=args.provider,
        cost_limit=args.cost_limit,
        confirm=args.confirm,
        target_language=args.target_language,
        report=not args.no_report,
    )
    _emit(out)
    if out.get("status") == "cost_exceeded":
        return 2
    return 0 if out.get("status") == "ok" else 1


def _cmd_apply(args: argparse.Namespace) -> int:
    select = (
        [s.strip() for s in args.select.split(",") if s.strip()] if args.select else None
    )
    out = review.apply(
        _resolve_project(args.project),
        select=select,
        dry_run=args.dry_run,
    )
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


_DISPATCH = {
    "prepare": _cmd_prepare,
    "fanout": _cmd_fanout,
    "commit": _cmd_commit,
    "run": _cmd_run,
    "apply": _cmd_apply,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_annotations.py",
        description="Review reader annotations on a translated book.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser(
        "prepare", help="render prompts + manifest for subagent/headless workers (no spend)"
    )
    _add_select(p_prepare)
    p_prepare.add_argument(
        "--worker-model", default=None, help="model tier for each worker (default: sonnet)"
    )
    p_prepare.add_argument(
        "--batch-size", type=int, default=None,
        help="workers per spawn wave / default headless concurrency (default: 5)",
    )
    p_prepare.add_argument(
        "--keep-drafts", action="store_true",
        help="do not clear existing drafts (use when recovering with work in flight)",
    )

    p_fanout = sub.add_parser("fanout", help="run a headless claude/cursor wave (no API spend)")
    p_fanout.add_argument("--project", required=True, help="project id or path")
    p_fanout.add_argument("--target-ids", help="comma-separated keys to re-run (default: all undrafted)")
    p_fanout.add_argument("--concurrency", type=int, default=None, help="max parallel CLI processes")
    p_fanout.add_argument("--cli", choices=("claude", "cursor"), default=None,
                          help="headless CLI (default: .harness/config.json headless_cli, else claude)")
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")

    p_commit = sub.add_parser("commit", help="parse drafts, write results.json + the dated report")
    p_commit.add_argument("--project", required=True, help="project id or path")
    p_commit.add_argument("--no-report", action="store_true", help="skip writing the markdown report")

    p_run = sub.add_parser("run", help="API backend: review every annotation directly (metered)")
    _add_select(p_run)
    p_run.add_argument("--model", default=None, help="LLM model override")
    p_run.add_argument("--provider", default=None, help="LLM provider override")
    p_run.add_argument("--cost-limit", type=float, default=0.50,
                       help="refuse to spend more than this without --confirm (default: 0.50)")
    p_run.add_argument("--confirm", action="store_true", help="proceed past the cost gate")
    p_run.add_argument("--no-report", action="store_true", help="skip writing the markdown report")

    p_apply = sub.add_parser("apply", help="write reviewed notes back into annotations.jsonl")
    p_apply.add_argument("--project", required=True, help="project id or path")
    p_apply.add_argument("--select", help="comma-separated keys to apply; omit for a plan-only dry run")
    p_apply.add_argument("--dry-run", action="store_true", help="force plan mode even with --select")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
