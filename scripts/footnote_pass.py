#!/usr/bin/env python3
"""
Author new editorial footnotes for a translated book.

The create counterpart to ``review_annotations.py``, which only *reviews* notes a
reader already left — its ``apply`` replaces the text of an existing annotation and
cannot mint one. This CLI infers the book's own footnote style from the notes it
already carries, scans chapters for more spots deserving the same treatment, and
appends validated ``type: "footnote"`` records.

Subcommands, each printing exactly one JSON object with a ``_schema`` block:

    style          the style-inference corpus for the profile gate   (no spend)
    scan-prepare   render one scan prompt per chapter + a manifest   (no spend)
    scan-fanout    run a headless claude/cursor wave over them       (no API spend)
    scan-commit    parse drafts, validate, write candidates + report (no spend)
    add            append validated footnote records                 (the writer)
    verify         audit every active footnote for silent breakage   (no spend)

``add`` is the front door the 2026-09-10 fabre2 run did not have. Its validation is
the point: ``src/endnotes.py`` drops a footnote silently when the ``es_idx`` has no
alignment row, when the aligned sentence is not findable in ``chapters/<id>.txt``,
or — logging nothing at all — when the note is empty once the ``[anchor]`` bracket
is stripped. Each is a named error here, before the record reaches the file.

Typical flow (the skill drives it, with a STOP gate before each wave):

    python scripts/footnote_pass.py style        --project fabre2
    python scripts/footnote_pass.py scan-prepare --project fabre2 --chapters 1-20 \
        --profile-file projects/fabre2/.harness/footnotes/profile.md
    python scripts/footnote_pass.py scan-fanout  --project fabre2
    python scripts/footnote_pass.py scan-commit  --project fabre2
    python scripts/footnote_pass.py add          --project fabre2 --dry-run \
        --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…"
    python scripts/footnote_pass.py verify       --project fabre2
    python scripts/harness.py epub --project fabre2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every raya/guillemet/accent in the JSON we print — the exact bytes a Spanish
# gloss is made of. The hasattr guard keeps this safe under pytest's captured
# streams, which lack ``reconfigure``.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time so it can't corrupt the JSON an agent parses.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from src.footnote_pass import corpus as fp_corpus  # noqa: E402
from src.footnote_pass import scan as fp_scan  # noqa: E402
from src.footnote_pass import write as fp_write  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Commands that write nothing and so get no sidecar (their whole contract is that
# they touch nothing on disk).
_NO_SIDECAR_COMMANDS: frozenset[str] = frozenset()

_OUTPUT_DIR: Path | None = None


def _die(message: str) -> None:
    """Exit with one JSON error object — never a bare traceback."""
    raise SystemExit(
        json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2)
    )


def _resolve_project(arg: str) -> Path:
    """Accept a project id or a path; exit with JSON on failure."""
    candidate = Path(arg)
    if candidate.is_dir():
        return candidate
    resolved = _REPO_ROOT / "projects" / arg
    if resolved.is_dir():
        return resolved
    _die(f"project not found: {arg!r} (looked for a directory and projects/{arg})")
    raise AssertionError("unreachable")  # pragma: no cover


def _set_output_dir(args: argparse.Namespace) -> None:
    """Point the ``last_output.json`` sidecar at this invocation's project."""
    global _OUTPUT_DIR
    _OUTPUT_DIR = None
    if getattr(args, "command", None) in _NO_SIDECAR_COMMANDS:
        return
    project = getattr(args, "project", None)
    if not project:
        return
    candidate = Path(project)
    found = candidate if candidate.is_dir() else _REPO_ROOT / "projects" / project
    if found.is_dir():
        _OUTPUT_DIR = found / ".harness" / "footnotes"


def _write_output_artifact(payload: dict) -> None:
    """Mirror a command's JSON result to ``.harness/footnotes/last_output.json``.

    The same contract ``harness.py`` and ``run_judges.py`` grew: a file on disk is
    ``Read``-able without a second interpreter in the loop, which is what stops an
    agent hand-rolling a ``python -c`` filter over stdout and mangling every raya
    in the process. Best-effort by design — the artifact must never break a command.
    """
    if _OUTPUT_DIR is None:
        return
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out = _OUTPUT_DIR / "last_output.json"
        disk = dict(payload)
        disk.pop("_schema", None)
        out.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OUTPUT_JSON: {out}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a convenience, never a hard dependency
        print(f"warning: could not write last_output.json: {exc}", file=sys.stderr)


def _emit(payload: dict, schema: dict | None = None) -> None:
    """Print exactly one JSON object, after mirroring it to the sidecar."""
    if schema is not None and "_schema" not in payload:
        payload["_schema"] = schema
    _write_output_artifact(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_chapters(spec: Optional[str]) -> Optional[list[str]]:
    """Parse ``1-20`` / ``3,7,12`` / ``chapter_04,chapter_09`` into chapter ids.

    Accepts both the numeric range form every other CLI in the repo takes and
    explicit chapter ids, because a scan scope is often "the ones I just read".
    """
    if not spec:
        return None
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("chapter_"):
            out.append(part)
            continue
        if "-" in part:
            start, _sep, end = part.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError:
                _die(f"malformed --chapters range {part!r}; expected e.g. 1-20")
            if hi < lo:
                _die(f"--chapters range {part!r} counts backwards")
            out.extend(f"chapter_{n:02d}" for n in range(lo, hi + 1))
            continue
        try:
            out.append(f"chapter_{int(part):02d}")
        except ValueError:
            _die(
                f"malformed --chapters value {part!r}; expected a number, a range "
                "(1-20), or a chapter id (chapter_04)"
            )
    # Deduplicate while keeping book order.
    return sorted(set(out))


def _load_json_file(path_arg: str) -> list[dict[str, Any]]:
    """Read ``--json-file`` into a list of note dicts.

    Accepts a bare list, or the ``{"notes": [...]}`` / ``{"approved": [...]}``
    wrappers an agent is likely to write.
    """
    path = Path(path_arg)
    if not path.exists():
        _die(f"--json-file not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _die(f"unreadable --json-file {path}: {exc}")
        raise AssertionError("unreachable")  # pragma: no cover
    if isinstance(doc, dict):
        for key in ("notes", "approved", "candidates"):
            if isinstance(doc.get(key), list):
                doc = doc[key]
                break
    if not isinstance(doc, list):
        _die(
            f"--json-file {path} must hold a list of notes (or a "
            '{"notes": [...]} wrapper)'
        )
    rows = [row for row in doc if isinstance(row, dict)]
    if not rows:
        _die(f"--json-file {path} holds no note objects")
    return rows


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_style(args: argparse.Namespace) -> int:
    out = fp_corpus.style(
        _resolve_project(args.project), chapters=_parse_chapters(args.chapters)
    )
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_scan_prepare(args: argparse.Namespace) -> int:
    out = fp_scan.scan_prepare(
        _resolve_project(args.project),
        profile_file=Path(args.profile_file),
        chapters=_parse_chapters(args.chapters),
        worker_model=args.worker_model,
        batch_size=args.batch_size,
        keep_drafts=args.keep_drafts,
    )
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_scan_fanout(args: argparse.Namespace) -> int:
    target_ids = (
        [t.strip() for t in args.target_ids.split(",") if t.strip()]
        if args.target_ids
        else None
    )
    out = fp_scan.scan_fanout(
        _resolve_project(args.project),
        target_ids=target_ids,
        concurrency=args.concurrency,
        cli=args.cli,
        cli_bin=args.cli_bin,
        effort=args.effort,
        cache=args.prompt_cache,
    )
    _emit(out)
    return 1 if out.get("error") else 0


def _cmd_scan_commit(args: argparse.Namespace) -> int:
    out = fp_scan.scan_commit(_resolve_project(args.project), report=not args.no_report)
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_add(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    if args.json_file:
        if args.chapter or args.es_idx is not None or args.note:
            _die("--json-file is exclusive with --chapter/--es-idx/--anchor/--note")
        notes = _load_json_file(args.json_file)
    else:
        missing = [
            flag
            for flag, value in (
                ("--chapter", args.chapter),
                ("--es-idx", args.es_idx),
                ("--note", args.note),
            )
            if value is None or value == ""
        ]
        if missing:
            _die(
                f"add needs {', '.join(missing)} (or --json-file for a batch). "
                "--anchor is optional; without it the marker falls to the end of "
                "the sentence."
            )
        notes = [
            {
                "chapter_id": args.chapter,
                "es_idx": args.es_idx,
                "anchor": args.anchor,
                "note": args.note,
            }
        ]
    out = fp_write.add(project_dir, notes, dry_run=args.dry_run)
    _emit(out, _ADD_SCHEMA)
    return 0 if out.get("status") == "ok" else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    out = fp_write.verify(
        _resolve_project(args.project), chapters=_parse_chapters(args.chapters)
    )
    _emit(out, _VERIFY_SCHEMA)
    return 0 if out.get("status") == "ok" else 1


_ADD_SCHEMA = {
    "status": "'ok' (everything landed) | 'partial' (at least one refused) | 'error'",
    "dry_run": "true when nothing was written",
    "added": "notes appended, each with the minted sub_id",
    "planned": "--dry-run only: what would be appended, with the injection preview",
    "refused": "notes NOT written, each with the problems that blocked it",
    "warnings": "notes written despite a degraded anchor (anchor_not_found, "
    "ambiguous_anchor) — the note publishes, the marker may sit in the wrong place",
    "counts": "{requested, added, planned, refused, warnings}",
    "annotations_path": "the append-only file written to",
    "instructions": "what to fix and what to run next",
}

_VERIFY_SCHEMA = {
    "status": "'ok' | 'broken' (at least one active footnote publishes nothing)",
    "counts": "{audited, ok, broken, warned}",
    "by_code": "{failure code: count} across every problem found",
    "broken": "footnotes that publish NOTHING today, with the reason",
    "warned": "footnotes that publish but whose marker placement is degraded",
    "instructions": "what the codes mean and what to do about them",
}


_DISPATCH = {
    "style": _cmd_style,
    "scan-prepare": _cmd_scan_prepare,
    "scan-fanout": _cmd_scan_fanout,
    "scan-commit": _cmd_scan_commit,
    "add": _cmd_add,
    "verify": _cmd_verify,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="footnote_pass.py",
        description="Author new editorial footnotes for a translated book.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_style = sub.add_parser(
        "style", help="the style-inference corpus for the profile gate (no spend)"
    )
    p_style.add_argument("--project", required=True, help="project id or path")
    p_style.add_argument(
        "--chapters",
        help="restrict the corpus (1-20, 3,7,12, or chapter_04); default is the "
        "whole book, which is what style inference wants",
    )

    p_prepare = sub.add_parser(
        "scan-prepare", help="render one scan prompt per chapter + a manifest (no spend)"
    )
    p_prepare.add_argument("--project", required=True, help="project id or path")
    p_prepare.add_argument(
        "--profile-file",
        required=True,
        help="the approved footnote profile from the Gate 1 conversation. Required: "
        "without it there is nothing to scan FOR",
    )
    p_prepare.add_argument(
        "--chapters", help="chapters to scan (1-20, 3,7,12, chapter_04); default all"
    )
    p_prepare.add_argument(
        "--worker-model", default=None, help="model tier per worker (default: sonnet)"
    )
    p_prepare.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="workers per spawn wave / default headless concurrency (default: 5)",
    )
    p_prepare.add_argument(
        "--keep-drafts",
        action="store_true",
        help="do not clear existing drafts (use when recovering with work in flight)",
    )

    p_fanout = sub.add_parser(
        "scan-fanout", help="run a headless claude/cursor wave (no API spend)"
    )
    p_fanout.add_argument("--project", required=True, help="project id or path")
    p_fanout.add_argument(
        "--target-ids", help="comma-separated chapter ids to re-run (default: all undrafted)"
    )
    p_fanout.add_argument(
        "--concurrency", type=int, default=None, help="max parallel CLI processes"
    )
    p_fanout.add_argument(
        "--cli",
        choices=("claude", "cursor"),
        default=None,
        help="headless CLI (default: .harness/config.json headless_cli, else claude)",
    )
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")
    p_fanout.add_argument(
        "--effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "default"],
        help="Per-run Claude --effort override (default: config "
        "headless_effort_footnote_scan, else medium; 'default' emits no --effort flag)",
    )
    p_fanout.add_argument(
        "--prompt-cache",
        default=None,
        choices=["auto", "5m", "1h", "off"],
        help="Per-run Claude prompt-cache TTL (default: config headless_prompt_cache / auto)",
    )

    p_commit = sub.add_parser(
        "scan-commit", help="parse drafts, validate candidates, write the dated report"
    )
    p_commit.add_argument("--project", required=True, help="project id or path")
    p_commit.add_argument(
        "--no-report", action="store_true", help="skip writing the markdown report"
    )

    p_add = sub.add_parser("add", help="append validated footnote records (the writer)")
    p_add.add_argument("--project", required=True, help="project id or path")
    p_add.add_argument("--chapter", default=None, help="chapter id, e.g. chapter_04")
    p_add.add_argument("--es-idx", type=int, default=None, help="aligned sentence index")
    p_add.add_argument(
        "--anchor",
        default=None,
        help="verbatim span from the sentence to attach the marker after; omit and "
        "the marker falls to the end of the sentence",
    )
    p_add.add_argument("--note", default=None, help="the gloss, in the book's language")
    p_add.add_argument(
        "--json-file",
        default=None,
        help="a list of {chapter_id, es_idx, anchor, note} — lands a whole approved "
        "batch in one call, with a per-note result",
    )
    p_add.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the resolved anchor + injection preview; write nothing",
    )

    p_verify = sub.add_parser(
        "verify", help="audit every active footnote for silent breakage (no spend)"
    )
    p_verify.add_argument("--project", required=True, help="project id or path")
    p_verify.add_argument("--chapters", help="restrict the audit; default is the whole book")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _set_output_dir(args)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
