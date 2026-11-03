#!/usr/bin/env python3
"""
The quotation pass: put the NBLA into a finished translation.

``src/quotations/``. Subcommands, each printing exactly one JSON object (mirrored
to ``projects/<slug>/.harness/quotations/last_output.json`` when a project is named):

    index          build the per-user Bible index from your own EPUB   (no spend)
    status         what the pass has done for a book so far            (no spend)
    scan-prepare   render one detection prompt per chapter + manifest  (no spend)
    scan-fanout    run the detection wave headless                     (no API spend)
    scan-commit    validate candidates, add coded reference cards      (no spend)
    resolve        look the verses up, work out each reference fix     (no spend)
    map-prepare    render one mapping prompt per chapter + manifest    (no spend)
    map-fanout     run the mapping wave headless                       (no API spend)
    map-commit     validate every mapping, write the board's cards     (no spend)
    board          the board's URL and what is picked / approved       (no spend)
    asks           the cards where the user asked for other wording    (no spend)
    revise         validate a revised mapping for one card, store it   (no spend)
    reref          make the card for another verse on a card's sentence (no spend)
    apply          apply the approved board through the judge path     (no spend)
    verify         check every applied quotation is still in the text  (no spend)
    record         rewrite quotations.json from the ledger             (no spend)

    python -X utf8 scripts/quotation_pass.py index --epub "C:/path/NBLA.epub" --version nbla
    python -X utf8 scripts/quotation_pass.py scan-prepare --project short-sermons-to-little-children
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

from src.harness import state as hstate  # noqa: E402
from src.quotations import quotations_dir  # noqa: E402
from src.quotations import apply as qapply  # noqa: E402
from src.quotations import board as qboard  # noqa: E402
from src.quotations import map as qmap  # noqa: E402
from src.quotations import record as qrecord  # noqa: E402
from src.quotations import resolve as qresolve  # noqa: E402
from src.quotations import reref as qreref  # noqa: E402
from src.quotations import revise as qrevise  # noqa: E402
from src.quotations import scan as qscan  # noqa: E402
from src.quotations.index import IndexError_, build_index, index_path, load_index, write_index  # noqa: E402
from src.quotations.versions import VERSIONS  # noqa: E402

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


def _emit(project_dir: Optional[Path], payload: dict) -> None:
    """Print one JSON object and mirror it to ``.harness/quotations/last_output.json``."""
    if project_dir is not None:
        try:
            out = quotations_dir(project_dir) / "last_output.json"
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


def _cmd_index(args: argparse.Namespace) -> int:
    epub = Path(args.epub)
    if not epub.exists():
        _die(f"EPUB not found: {epub}")
    version = VERSIONS.get(args.version)
    if version is None:
        _die(f"unknown version {args.version!r}; known: {sorted(VERSIONS)}")
    doc = build_index(epub, version=version.id)
    if version.name.lower() not in (doc.get("title") or "").lower() and not args.force:
        _die(
            f"the EPUB's title is {doc.get('title')!r}, not {version.name!r}. Refusing to "
            f"index it as {version.id.upper()}; pass --force if it really is that version."
        )
    path = write_index(doc)
    _emit(None, {
        "status": "ok",
        "version": version.id,
        "title": doc["title"],
        "verse_count": doc["verse_count"],
        "proper_words": len(doc["proper_words"]),
        "epub_sha256": doc["epub_sha256"],
        "index_path": str(path),
        "instructions": "The index is gitignored and never leaves this machine. Run `scan-prepare` next.",
    })
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    qdir = quotations_dir(project_dir)
    try:
        index = load_index("nbla")
        index_state = {"ok": True, "path": str(index_path("nbla")), "verses": len(index.verses)}
    except IndexError_ as exc:
        index_state = {"ok": False, "error": str(exc)}
    cands = qscan.candidates_path(project_dir)
    payload = {
        "status": "ok",
        "index": index_state,
        "chapters": len(qscan.chapter_ids(project_dir)),
        "scan_manifest": (qdir / "scan.manifest.json").exists(),
        "candidates": None,
    }
    if cands.exists():
        doc = json.loads(cands.read_text(encoding="utf-8"))
        by_kind: dict[str, int] = {}
        for row in doc.get("candidates") or []:
            by_kind[row.get("kind", "?")] = by_kind.get(row.get("kind", "?"), 0) + 1
        payload["candidates"] = {
            "committed_at": doc.get("committed_at"),
            "total": len(doc.get("candidates") or []),
            "by_kind": by_kind,
        }
    mapped = qboard.load_mapped(project_dir)
    if mapped is not None:
        by_status: dict[str, int] = {}
        for card in mapped.get("cards") or []:
            by_status[card["status"]] = by_status.get(card["status"], 0) + 1
        payload["cards"] = {"committed_at": mapped.get("committed_at"), "by_status": by_status,
                            "missing": mapped.get("missing") or []}
    board = qboard.load(project_dir)
    payload["board"] = {"saved_at": board["saved_at"], "approved": board["approved"]}
    payload["applied_cards"] = len(qapply.applied_cards(project_dir))
    payload["asks_open"] = sum(1 for c in board["choices"].values() if str(c.get("ask") or "").strip())
    revisions = qmap.load_revisions(project_dir)
    card_keys = {c["key"] for c in (mapped or {}).get("cards") or []}
    payload["revisions"] = len(revisions)
    payload["orphan_revisions"] = sorted(k for k in revisions if k not in card_keys)
    record = qrecord.read_record(project_dir)
    payload["record"] = None if record is None else {
        k: record.get(k) for k in ("verses_unique", "verse_occurrences", "share", "within_limits", "generated_at")
    }
    _emit(project_dir, payload)
    return 0


def _cmd_scan_prepare(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qscan.scan_prepare(
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
    out = qscan.scan_fanout(
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
    out = qscan.scan_commit(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_resolve(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qresolve.resolve(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_map_prepare(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qmap.map_prepare(
        project_dir,
        chapters=_parse_chapters(args.chapters),
        worker_model=args.worker_model,
        cli=args.cli,
        keep_drafts=args.keep_drafts,
    )
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_map_fanout(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    job_ids = [j.strip() for j in args.job_ids.split(",") if j.strip()] if args.job_ids else None
    out = qmap.map_fanout(
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


def _cmd_map_commit(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qmap.map_commit(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_board(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    state = qboard.build(project_dir)
    if state.get("status") != "ok":
        _emit(project_dir, {"status": "error", "error": "no mapped.json; run `map-commit` first"})
        return 1
    _emit(project_dir, {
        "status": "ok",
        "url": f"{args.base_url.rstrip('/')}/quotations/{project_dir.name}",
        "counts": state["counts"],
        "approved": state["approved"],
        "saved_at": state["saved_at"],
        "instructions": (
            "Give the user the url. Restart the reader (`scripts/reader.ps1 restart`) if "
            "it predates this pass. When they say it is approved, run `apply`."
        ),
    })
    return 0


def _cmd_asks(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qrevise.asks(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_revise(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"could not read the mapping file: {exc}")
    if not isinstance(data, dict):
        _die("the mapping file must hold one JSON object")
    mapping = data.get("mapping") if isinstance(data.get("mapping"), dict) else data
    out = qrevise.revise(project_dir, args.card, mapping, ask=data.get("ask"))
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_reref(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qreref.reref(project_dir, args.card, args.ref, why=args.why or "")
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_apply(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qapply.apply(project_dir, no_realign=args.no_realign)
    _emit(project_dir, out)
    return 0 if out.get("status") in ("ok", "partial") else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = qapply.verify(project_dir)
    _emit(project_dir, out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_record(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    doc = qrecord.write_record(project_dir)
    _emit(project_dir, {
        "status": "ok",
        "record_path": str(qrecord.record_path(project_dir)),
        **{k: doc.get(k) for k in ("verses_unique", "verse_occurrences", "quoted_words",
                                   "book_words", "share", "within_limits", "whole_book_quoted", "adapted")},
        "rows": len(doc.get("rows") or []),
    })
    return 0


_DISPATCH = {
    "index": _cmd_index,
    "status": _cmd_status,
    "scan-prepare": _cmd_scan_prepare,
    "scan-fanout": _cmd_scan_fanout,
    "scan-commit": _cmd_scan_commit,
    "resolve": _cmd_resolve,
    "map-prepare": _cmd_map_prepare,
    "map-fanout": _cmd_map_fanout,
    "map-commit": _cmd_map_commit,
    "board": _cmd_board,
    "asks": _cmd_asks,
    "revise": _cmd_revise,
    "reref": _cmd_reref,
    "apply": _cmd_apply,
    "verify": _cmd_verify,
    "record": _cmd_record,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotation_pass.py",
        description="Put the NBLA into a finished translation's Bible quotations.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build the per-user Bible index from an EPUB")
    p_index.add_argument("--epub", required=True, help="path to your own copy of the Bible EPUB")
    p_index.add_argument("--version", default="nbla", help="version id (default nbla)")
    p_index.add_argument("--force", action="store_true",
                         help="index even when the EPUB's title does not name the version")

    p_status = sub.add_parser("status", help="what the pass has done for a book")
    p_status.add_argument("--project", required=True, help="project id or path")

    p_prepare = sub.add_parser("scan-prepare", help="render one detection prompt per chapter")
    p_prepare.add_argument("--project", required=True, help="project id or path")
    p_prepare.add_argument("--chapters", help="chapters to scan (1-20, 3,7, chapter_04); default all")
    p_prepare.add_argument("--worker-model", default=None, help="model pin; default from the profile")
    p_prepare.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None,
                           help="headless CLI the manifest records (default: config, else host)")
    p_prepare.add_argument("--keep-drafts", action="store_true", help="do not clear existing drafts")

    p_fanout = sub.add_parser("scan-fanout", help="run the detection wave (no API spend)")
    p_fanout.add_argument("--project", required=True, help="project id or path")
    p_fanout.add_argument("--job-ids", help="comma-separated job ids (default: all undrafted)")
    p_fanout.add_argument("--concurrency", type=int, default=None, help="max parallel CLI processes")
    p_fanout.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None)
    p_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")
    p_fanout.add_argument(
        "--effort", default=None, choices=["low", "medium", "high", "xhigh", "default"],
        help="per-run effort (default: config headless_effort_quotations_scan, else medium)",
    )

    p_commit = sub.add_parser("scan-commit", help="validate candidates, add coded reference cards")
    p_commit.add_argument("--project", required=True, help="project id or path")

    p_resolve = sub.add_parser("resolve", help="look the verses up, work out each reference fix")
    p_resolve.add_argument("--project", required=True, help="project id or path")

    m_prepare = sub.add_parser("map-prepare", help="render one mapping prompt per chapter")
    m_prepare.add_argument("--project", required=True, help="project id or path")
    m_prepare.add_argument("--chapters", help="chapters to map (1-20, 3,7, chapter_04); default all")
    m_prepare.add_argument("--worker-model", default=None, help="model pin; default from the profile")
    m_prepare.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None)
    m_prepare.add_argument("--keep-drafts", action="store_true", help="do not clear existing drafts")

    m_fanout = sub.add_parser("map-fanout", help="run the mapping wave (no API spend)")
    m_fanout.add_argument("--project", required=True, help="project id or path")
    m_fanout.add_argument("--job-ids", help="comma-separated job ids (default: all undrafted)")
    m_fanout.add_argument("--concurrency", type=int, default=None, help="max parallel CLI processes")
    m_fanout.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None)
    m_fanout.add_argument("--cli-bin", default=None, help="path to the CLI binary if not on PATH")
    m_fanout.add_argument(
        "--effort", default=None, choices=["low", "medium", "high", "xhigh", "default"],
        help="per-run effort (default: config headless_effort_quotations_map, else high)",
    )

    m_commit = sub.add_parser("map-commit", help="validate every mapping, write the board's cards")
    m_commit.add_argument("--project", required=True, help="project id or path")

    p_board = sub.add_parser("board", help="the board's URL and what is picked / approved")
    p_board.add_argument("--project", required=True, help="project id or path")
    p_board.add_argument("--base-url", default="http://localhost:5000", help="where the reader runs")

    p_asks = sub.add_parser("asks", help="the cards where the user asked for other wording")
    p_asks.add_argument("--project", required=True, help="project id or path")

    p_revise = sub.add_parser("revise", help="validate a revised mapping for one card, store it")
    p_revise.add_argument("--project", required=True, help="project id or path")
    p_revise.add_argument("--card", required=True, help="the card key, as `asks` prints it")
    p_revise.add_argument("--file", required=True,
                          help="JSON: the mapping (replace/echo/drift/note), or {mapping, ask}")

    p_reref = sub.add_parser("reref", help="make the card for another verse on a card's sentence")
    p_reref.add_argument("--project", required=True, help="project id or path")
    p_reref.add_argument("--card", required=True, help="the card naming the wrong verse")
    p_reref.add_argument("--ref", required=True, help="the verse to use instead, in English ('Jeremiah 12:3')")
    p_reref.add_argument("--why", default=None, help="why, in the user's words; kept in rerefs.json")

    p_apply = sub.add_parser("apply", help="apply the approved board (judge-apply path)")
    p_apply.add_argument("--project", required=True, help="project id or path")
    p_apply.add_argument("--no-realign", action="store_true",
                         help="skip recombine/realign; a later apply finishes it")

    p_verify = sub.add_parser("verify", help="check every applied quotation is still in its chunk")
    p_verify.add_argument("--project", required=True, help="project id or path")

    p_record = sub.add_parser("record", help="rewrite quotations.json from the ledger")
    p_record.add_argument("--project", required=True, help="project id or path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
