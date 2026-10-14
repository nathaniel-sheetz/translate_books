#!/usr/bin/env python3
"""
Scan a book's English source for the translation decisions it will force.

The scan half of the decisions stage (``src/decisions/``). Not yet wired into
``harness.py``: it runs standalone until its recall has been checked against
``comparisons/decision-archaeology/gold.jsonl``.

Subcommands, each printing exactly one JSON object with a ``_schema`` block:

    scan-prepare   render one prompt per chapter + a manifest       (no spend)
    scan-fanout    run a headless claude/cursor/codex wave          (no API spend)
    scan-commit    parse drafts, verify quotes, write candidates    (no spend)

    python scripts/decisions.py scan-prepare --project midnight
    python scripts/decisions.py scan-fanout  --project midnight
    python scripts/decisions.py scan-commit  --project midnight
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles accents in the JSON.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.decisions import decisions_dir  # noqa: E402
from src.decisions import scan as dscan  # noqa: E402
from src.harness import state as hstate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _die(message: str) -> None:
    raise SystemExit(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))


def _resolve_project(arg: str) -> Path:
    candidate = Path(arg)
    if candidate.is_dir():
        return candidate.resolve()
    resolved = _REPO_ROOT / "projects" / arg
    if resolved.is_dir():
        return resolved.resolve()
    _die(f"project not found: {arg!r} (looked for a directory and projects/{arg})")
    raise AssertionError("unreachable")  # pragma: no cover


def _emit(project_dir: Path, payload: dict) -> None:
    """Print one JSON object and mirror it to ``.harness/decisions/last_output.json``."""
    try:
        out = decisions_dir(project_dir) / "last_output.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        disk = {k: v for k, v in payload.items() if k != "_schema"}
        out.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OUTPUT_JSON: {out}", file=sys.stderr)
    except OSError as exc:
        print(f"warning: could not write last_output.json: {exc}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_chapters(spec: Optional[str]) -> Optional[list[str]]:
    """``1-20`` / ``3,7`` / ``chapter_04`` into chapter ids."""
    if not spec:
        return None
    out: list[str] = []
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if part.startswith("chapter_"):
            out.append(part)
        elif "-" in part:
            lo, _sep, hi = part.partition("-")
            try:
                out.extend(f"chapter_{n:02d}" for n in range(int(lo), int(hi) + 1))
            except ValueError:
                _die(f"malformed --chapters range {part!r}")
        else:
            try:
                out.append(f"chapter_{int(part):02d}")
            except ValueError:
                _die(f"malformed --chapters value {part!r}")
    return sorted(set(out))


def _cmd_scan_prepare(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = dscan.scan_prepare(
        project_dir,
        chapters=_parse_chapters(args.chapters),
        worker_model=args.worker_model,
        cli=args.cli,
        keep_drafts=args.keep_drafts,
    )
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_scan_fanout(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    job_ids = [j.strip() for j in args.job_ids.split(",") if j.strip()] if args.job_ids else None
    out = dscan.scan_fanout(
        project_dir,
        job_ids=job_ids,
        concurrency=args.concurrency,
        cli=args.cli,
        cli_bin=args.cli_bin,
        effort=args.effort,
    )
    _emit(project_dir, out)
    counts = out.get("counts") or {}
    all_failed = bool(counts.get("failed")) and not counts.get("wrote")
    return 1 if out.get("error") or all_failed else 0


def _cmd_scan_commit(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = dscan.scan_commit(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


_DISPATCH = {
    "scan-prepare": _cmd_scan_prepare,
    "scan-fanout": _cmd_scan_fanout,
    "scan-commit": _cmd_scan_commit,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decisions.py",
        description="Scan a book's English source for the translation decisions it will force.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("scan-prepare", help="render one prompt per chapter (no spend)")
    p_prepare.add_argument("--project", required=True, help="project id or path")
    p_prepare.add_argument("--chapters", help="chapters to scan (1-20, 3,7, chapter_04); default all")
    p_prepare.add_argument("--worker-model", default=None, help="model pin; default from the profile")
    p_prepare.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None,
                           help="headless CLI the manifest records (default: config, else host)")
    p_prepare.add_argument(
        "--keep-drafts", action="store_true", help="do not clear existing drafts"
    )

    p_fanout = sub.add_parser("scan-fanout", help="run a headless wave (no API spend)")
    p_fanout.add_argument("--project", required=True, help="project id or path")
    p_fanout.add_argument("--job-ids", help="comma-separated job ids to run (default: all undrafted)")
    p_fanout.add_argument("--concurrency", type=int, default=None, help="max parallel CLI processes")
    p_fanout.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None)
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")
    p_fanout.add_argument(
        "--effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "default"],
        help="per-run effort (default: config headless_effort_decisions_scan, else medium)",
    )

    p_commit = sub.add_parser("scan-commit", help="parse drafts, verify quotes, write candidates")
    p_commit.add_argument("--project", required=True, help="project id or path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
