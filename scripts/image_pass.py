#!/usr/bin/env python3
"""
Redo a book's images headlessly, on a ChatGPT subscription (Codex CLI).

Translating the lettering on a map, cleaning a scan, making a cover, replacing an
illustration — the work that used to be the ChatGPT web UI plus copying files
into ``projects/<slug>/images/`` by hand. The orchestrating agent looks at each
image and writes the job; Codex only makes pixels; a human picks.

Subcommands, each printing exactly one JSON object:

    inventory   every image the book references, plus the cover   (no spend)
    backfill    bring in the larger scans the source page links   (no spend)
    triage      record what you made of each image you looked at  (no spend)
    prepare     validate jobs and render one prompt per image     (no spend)
    generate    run Codex per candidate and harvest the image     (subscription)
    check       record what is wrong with each candidate          (no spend)
    composite   one candidate's patch over another picture's pixels (no spend)
    board       where the page is, and what the user said on it   (no spend)
    apply       back up, convert to the original's name, replace  (the writer)
    revert      restore originals from images_original/           (no spend)
    verify      tokens resolve, backups intact, ledger consistent (no spend)

Filenames never change, so no ``[IMAGE:…]`` token in any text artefact is
touched, and the first replacement of a file backs the original up to
``images_original/`` where nothing overwrites it.

The user's side of all this is one page in the web UI, ``/image-pass/<project>``:
every image at whatever stage it is in, with the triage verdicts, the candidates
beside their originals, and inputs that are saved where ``board`` and
``apply --from-board`` read them.

``generate`` is subscription-only and fails closed: it refuses to start unless
``codex login status`` reports a ChatGPT login, and every metered credential is
scrubbed from the child environment. There is no override flag.

Typical flow (the skill drives it, with a STOP gate before each spend or write):

    python scripts/image_pass.py inventory --project home-geography
    python scripts/image_pass.py backfill  --project home-geography --dry-run
    python scripts/image_pass.py backfill  --project home-geography
    python scripts/image_pass.py triage    --project home-geography \
        --json-file projects/home-geography/.harness/images/triage_rows.json
    python scripts/image_pass.py board     --project home-geography
    python scripts/image_pass.py prepare   --project home-geography \
        --json-file projects/home-geography/.harness/images/jobs.json
    python scripts/image_pass.py generate  --project home-geography --estimate
    python scripts/image_pass.py generate  --project home-geography
    python scripts/image_pass.py check     --project home-geography \
        --json-file projects/home-geography/.harness/images/check_rows.json
    python scripts/image_pass.py board     --project home-geography
    python scripts/image_pass.py apply     --project home-geography --from-board --dry-run
    python scripts/image_pass.py apply     --project home-geography --from-board
    python scripts/image_pass.py verify    --project home-geography
    python scripts/harness.py epub --project home-geography
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every accent in a Spanish label map. The hasattr guard keeps this safe under
# pytest's captured streams, which lack ``reconfigure``.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from src.image_pass import apply as ip_apply  # noqa: E402
from src.image_pass import backfill as ip_backfill  # noqa: E402
from src.image_pass import board as ip_board  # noqa: E402
from src.image_pass import composite as ip_composite  # noqa: E402
from src.image_pass import inventory as ip_inventory  # noqa: E402
from src.image_pass import jobs as ip_jobs  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Where web_ui/app.py listens when it is started the usual way.
DEFAULT_BASE_URL = "http://127.0.0.1:5000"

# ``.harness/images/`` for the project this invocation names, resolved once in
# main(). None when --project doesn't resolve (the command is about to fail on it
# anyway).
_OUTPUT_DIR: Path | None = None


def _die(message: str) -> None:
    """Exit with one JSON error object — never a bare traceback."""
    raise SystemExit(
        json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2)
    )


def _resolve_project(arg: str) -> Path:
    """Accept a project id or a path; exit with JSON on failure."""
    # Resolved, because `prepare` writes these paths into the manifest and a
    # later command run from another cwd must read the same files.
    candidate = Path(arg)
    if candidate.is_dir():
        return candidate.resolve()
    resolved = _REPO_ROOT / "projects" / arg
    if resolved.is_dir():
        return resolved.resolve()
    _die(f"project not found: {arg!r} (looked for a directory and projects/{arg})")
    raise AssertionError("unreachable")  # pragma: no cover


def _set_output_dir(args: argparse.Namespace) -> None:
    """Point the ``last_output.json`` sidecar at this invocation's project."""
    global _OUTPUT_DIR
    _OUTPUT_DIR = None
    project = getattr(args, "project", None)
    if not project:
        return
    candidate = Path(project)
    found = candidate if candidate.is_dir() else _REPO_ROOT / "projects" / project
    if found.is_dir():
        _OUTPUT_DIR = found / ".harness" / "images"


def _write_output_artifact(payload: dict) -> None:
    """Mirror a command's JSON result to ``.harness/images/last_output.json``.

    Same contract as ``footnote_pass.py``: a file on disk is ``Read``-able
    without a second interpreter in the loop. Best-effort by design — the
    artifact must never break a command.
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


def _load_json_list(path_arg: str, wrappers: tuple[str, ...], what: str) -> list[Any]:
    """Read ``--json-file`` as a list, or one of the wrappers an agent may write."""
    path = Path(path_arg)
    if not path.exists():
        _die(f"--json-file not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        _die(f"unreadable --json-file {path}: {exc}")
        raise AssertionError("unreachable")  # pragma: no cover
    if isinstance(doc, dict):
        for key in wrappers:
            if isinstance(doc.get(key), list):
                doc = doc[key]
                break
    if not isinstance(doc, list) or not doc:
        _die(
            f"--json-file {path} must hold a non-empty list of {what} "
            f'(or a {{"{wrappers[0]}": [...]}} wrapper)'
        )
    return doc


def _split_ids(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()] or None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_inventory(args: argparse.Namespace) -> int:
    out = ip_inventory.inventory(_resolve_project(args.project))
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _recorded_source(project_dir: Path) -> str | None:
    """The page this book was ingested from, where an ingest wrote it down."""
    for name, key in (("project.json", "gutenberg_url"), ("pipeline_state.json", "url")):
        try:
            doc = json.loads((project_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get(key), str) and doc[key].strip():
            return doc[key].strip()
    return None


def _cmd_backfill(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    source = args.source or _recorded_source(project_dir)
    if not source:
        _die(
            "no source page on record for this project: pass --source with the "
            "Gutenberg HTML URL (or a saved copy of the page)"
        )
    # Imported here: only this command reads a source page, and the ingest
    # module needs requests and beautifulsoup4, which the others do not.
    from bs4 import BeautifulSoup

    from scripts import ingest_gutenberg as gutenberg

    try:
        html, base_url = gutenberg.fetch_html(source)
    except Exception as exc:  # noqa: BLE001 - one JSON error, never a traceback
        _die(f"could not read the source page {source!r}: {exc}")
    links = gutenberg.linked_images(BeautifulSoup(html, "html.parser"), base_url)
    out = ip_backfill.backfill(
        project_dir,
        links,
        fetch=gutenberg.fetch_bytes,
        only=_split_ids(args.images),
        accept=_split_ids(args.accept),
        dry_run=args.dry_run,
    )
    out["source"] = source
    _emit(out, _BACKFILL_SCHEMA)
    return 0 if out.get("status") in ("ok", "partial") else 1


def _cmd_prepare(args: argparse.Namespace) -> int:
    jobs = _load_json_list(args.json_file, ("jobs",), "jobs")
    out = ip_jobs.prepare(_resolve_project(args.project), jobs, replace=args.replace)
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_generate(args: argparse.Namespace) -> int:
    out = ip_jobs.generate(
        _resolve_project(args.project),
        estimate=args.estimate,
        concurrency=args.concurrency,
        target_ids=_split_ids(args.target_ids),
        cli_bin=args.cli_bin,
        model=args.model,
        timeout_s=args.timeout_minutes * 60 if args.timeout_minutes else None,
    )
    _emit(out, _GENERATE_SCHEMA)
    # Exit 1 when nothing was produced, so the flow cannot walk on to `board`
    # over candidates that were never written.
    return 0 if out.get("status") in ("ok", "partial") else 1


def _cmd_triage(args: argparse.Namespace) -> int:
    rows = _load_json_list(args.json_file, ("triage", "images"), "triage rows")
    out = ip_board.save_triage(_resolve_project(args.project), rows, replace=args.replace)
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_check(args: argparse.Namespace) -> int:
    rows = _load_json_list(args.json_file, ("checks",), "check rows")
    out = ip_board.save_checks(_resolve_project(args.project), rows)
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_composite(args: argparse.Namespace) -> int:
    rows = _load_json_list(args.json_file, ("composites",), "composite rows")
    out = ip_composite.composite(_resolve_project(args.project), rows, dry_run=args.dry_run)
    _emit(out, _COMPOSITE_SCHEMA)
    return 0 if out.get("status") == "ok" else 1


def _board_is_served(url: str) -> bool:
    """Whether the web UI answers for this project's board right now."""
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _cmd_board(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    out = ip_board.summary(project_dir)
    base = args.base_url.rstrip("/")
    out["url"] = f"{base}/image-pass/{project_dir.name}"
    out["server_running"] = _board_is_served(f"{base}/api/project/{project_dir.name}/image-pass")
    out["instructions"] = (
        "Give the user url. Read every candidate path under jobs yourself, beside "
        "its original, and record what you find with `check` before asking for "
        "picks. proposed is every image that wants a job and has none. requests is "
        "what the user asked for on the page that no prepared job reflects yet: "
        "fold each into its job (labels verbatim) and re-run `prepare`. picks is "
        "what `apply --from-board` would apply."
    )
    if not out["server_running"]:
        out["instructions"] = (
            "The web UI is not answering at that address, so the page will not "
            "open: ask the user to start it (`python web_ui/app.py`), or pass "
            "--base-url. " + out["instructions"]
        )
    _emit(out, _BOARD_SCHEMA)
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    stale: list[dict] = []
    if args.from_board:
        decisions, stale = ip_board.decisions_from_picks(ip_board.build(project_dir))
        if not decisions and not stale:
            _die(
                "no picks are waiting on the board: the user accepts, sends back "
                "or skips each image on the page first"
            )
    else:
        decisions = _load_json_list(args.json_file, ("decisions", "picks"), "decisions")
    out = ip_apply.apply(
        project_dir,
        decisions,
        dry_run=args.dry_run,
        max_side=args.max_side,
    )
    if stale:
        # A pick made on a candidate that has since been regenerated is not a
        # decision about the picture now in that slot.
        out["refused"] = out["refused"] + stale
        out["counts"]["refused"] = len(out["refused"])
        landed = out["applied"] or out["planned"] or out["noted"]
        out["status"] = "partial" if landed else "error"
    _emit(out, _APPLY_SCHEMA)
    return 0 if out.get("status") == "ok" else 1


def _cmd_revert(args: argparse.Namespace) -> int:
    images = _split_ids(args.images)
    if not images:
        _die("--images needs at least one image (comma-separated), or `all`")
    out = ip_apply.revert(_resolve_project(args.project), images or [])
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    out = ip_apply.verify(_resolve_project(args.project))
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


_GENERATE_SCHEMA = {
    "status": "'ok' | 'partial' (some candidates failed or were not run) | 'error' "
    "(nothing produced, or the batch never started)",
    "error": "top-level reason the batch refused or stopped — relay it verbatim. "
    "With empty wrote/failed nothing ran and nothing was spent",
    "estimate": "true for --estimate: nothing ran",
    "plan": "{cli, model, images, candidates, already_have, concurrency, "
    "minutes_per_image, minutes_measured, estimated_minutes, jobs}",
    "limit_warning": "--estimate only: the plan-limit burn, to quote at the consent gate",
    "wrote": "candidates harvested: {id, candidate, path, width, height, wall_s, "
    "harvested_from, images_generated?}. images_generated appears only when the "
    "model called the image tool more than once — that candidate cost that many "
    "images of plan usage",
    "failed": "candidates that ran and produced nothing, each with its error",
    "counts": "{wrote, failed, not_run, todo}",
    "instructions": "what to run next",
}

_COMPOSITE_SCHEMA = {
    "status": "'ok' | 'error' (a row was invalid: nothing was made)",
    "dry_run": "true when no candidate was written",
    "made": "composites written: {image, candidate, path, from, base, regions, "
    "size, changed_share, padded_source, search}. candidate is the new number, "
    "to check and to pick. changed_share is how much of the picture differs "
    "from the base (0.036 = 3.6%). padded_source means the candidate the patch "
    "came from has bands the base does not, and was fitted centred",
    "planned": "--dry-run only: what would be made",
    "regions": "per region, inside made/planned: {polygon, bbox, offset, "
    "at_search_edge, surround_difference, surround_difference_unmoved}. offset "
    "is how far the patch was slid, in base pixels, to where the drawing around "
    "the outline matched best; surround_difference is the mean grey-level "
    "difference left in that band (0-255)",
    "warnings": "surroundings_differ (the band around an outline does not match "
    "the base: look at the join) | alignment_at_edge (the best position was the "
    "furthest one tried) | nothing_changed",
    "invalid": "rows refused, each with its problems",
    "counts": "{requested, made, planned, invalid, warnings}",
    "instructions": "what to run next",
}

_BACKFILL_SCHEMA = {
    "status": "'ok' | 'partial' (at least one image failed) | 'error'",
    "source": "the page the link map was read from",
    "dry_run": "true when nothing was written to images/, images_original/ or the ledger",
    "upgraded": "images now at the larger scan: {image, url, from_size, to_size, "
    "score, writes, bytes, relinked_from?, accepted?, left_alone?, has_job?}. score "
    "is how alike the scan and the old file look (1.0 = same picture). "
    "relinked_from means the page linked a different picture and the scan that "
    "matched was used instead — say so. left_alone means images/<file> is a "
    "replacement and only its original was upgraded",
    "planned": "--dry-run only: what would be upgraded",
    "skipped": "the linked scan is no larger than what the book has (already done)",
    "unlike": "the linked scan does not look like this image and no other scan on "
    "the page does either: {image, url, score, have, linked}. Left alone. A person "
    "has to look; --accept takes the ones that are the same picture re-cropped",
    "split": "placeholders that are halves of one linked scan — reported, never joined",
    "unmatched": "referenced images the source page links no larger scan for",
    "failed": "could not be fetched, read or written, each with its error",
    "stale_jobs": "upgraded images with a prepared job drawn from the smaller file",
    "counts": "{referenced, upgraded, planned, relinked, skipped, unlike, split, "
    "unmatched, failed, stale_jobs}",
    "instructions": "what to run next",
}

_BOARD_SCHEMA = {
    "url": "the page to give the user",
    "server_running": "false when the web UI did not answer at --base-url",
    "counts": "{images, review, proposed, queued, replaced, missing, untriaged, "
    "leave, lettering, with_input, flagged, requests, picks, stale_picks, "
    "candidates, unchecked_candidates}",
    "untriaged": "images nobody has recorded a verdict for",
    "proposed": "images that want a job and have none: {image, verdict, by}. by is "
    "'user' when the verdict is one they set on the page, over the triage or in "
    "place of it. An image they set to leave alone is simply not here",
    "jobs": "every prepared job: {id, image, mode, stage, original, original_size, "
    "reference, labels, candidates: [{candidate, path, size, flags, checked, "
    "composite}], missing}. flags names a candidate whose proportions are off "
    "the original's; composite is {from, base, regions, changed_share} for a "
    "candidate `composite` made, else null",
    "requests": "what the user asked for on the page that the prepared job does "
    "not say: {image, reasons, verdict, note, labels, candidates, triage_verdict, "
    "job_mode}. reasons: needs_job | mode_differs | labels_differ | "
    "candidates_differ | note_newer_than_job | job_unwanted | note_unaddressed "
    "(a note on an image with no job: look again and re-run `triage`). A null "
    "field means the user did not change it",
    "picks": "the user's picks still to be applied, as `apply` decisions",
    "stale_picks": "picks made on a candidate that has since been regenerated: "
    "the user has to pick again",
    "instructions": "what to do next",
}

_APPLY_SCHEMA = {
    "status": "'ok' (everything landed) | 'partial' (at least one refused) | 'error'",
    "dry_run": "true when nothing was written",
    "applied": "images replaced, each with its backup_action and sizes",
    "planned": "--dry-run only: what would be replaced",
    "refused": "accepts NOT written, each with the problems that blocked it "
    "(with --from-board, also picks made on a candidate since regenerated)",
    "invalid": "rows that are not a usable decision",
    "noted": "skip / redo rows — recorded in the ledger, nothing written",
    "warnings": "accepts that landed (or would) despite e.g. a changed aspect ratio",
    "counts": "{requested, applied, planned, refused, invalid, skipped, redo, warnings}",
    "ledger_path": ".harness/images/images.jsonl — null on --dry-run",
    "instructions": "what to run next",
}


_DISPATCH = {
    "inventory": _cmd_inventory,
    "backfill": _cmd_backfill,
    "prepare": _cmd_prepare,
    "generate": _cmd_generate,
    "triage": _cmd_triage,
    "check": _cmd_check,
    "composite": _cmd_composite,
    "board": _cmd_board,
    "apply": _cmd_apply,
    "revert": _cmd_revert,
    "verify": _cmd_verify,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image_pass.py",
        description="Redo a book's images headlessly on a ChatGPT subscription.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inventory = sub.add_parser(
        "inventory", help="every image the book references, plus the cover (no spend)"
    )
    p_inventory.add_argument("--project", required=True, help="project id or path")

    p_backfill = sub.add_parser(
        "backfill",
        help="swap the images a book was ingested with for the larger scans its "
        "source page links to, under the same filenames (no spend)",
    )
    p_backfill.add_argument("--project", required=True, help="project id or path")
    p_backfill.add_argument(
        "--source",
        default=None,
        help="the book's Gutenberg HTML URL, or a saved copy of the page "
        "(default: the URL the ingest recorded in project.json)",
    )
    p_backfill.add_argument(
        "--images", default=None, help="comma-separated image names (default: all)"
    )
    p_backfill.add_argument(
        "--accept",
        default=None,
        help="comma-separated image names whose linked scan to take although it "
        "does not measure as the same picture (after looking at both)",
    )
    p_backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch the scans and report what would change; write nothing into "
        "images/, images_original/ or the ledger",
    )

    p_prepare = sub.add_parser(
        "prepare", help="validate jobs and render one prompt per image (no spend)"
    )
    p_prepare.add_argument("--project", required=True, help="project id or path")
    p_prepare.add_argument(
        "--json-file",
        required=True,
        help="a list of {image, mode, instruction, labels?, candidates?, input?, "
        "reference?}. "
        "mode is translate | restore | cover | replace; labels is the approved "
        'source → target map ({"NORTH": "NORTE"}), required for translate; '
        f"candidates is 1-{ip_jobs.MAX_CANDIDATES} (default {ip_jobs.DEFAULT_CANDIDATES}); "
        "input is 'original' (default) or 'current'; reference (cover only) is "
        "another of the book's images to draw the cover from",
    )
    p_prepare.add_argument(
        "--replace",
        action="store_true",
        help="make the manifest exactly this batch (default: merge by image, so "
        "re-preparing one image leaves the other jobs alone)",
    )

    p_generate = sub.add_parser(
        "generate", help="run Codex per candidate and harvest the image (subscription)"
    )
    p_generate.add_argument("--project", required=True, help="project id or path")
    p_generate.add_argument(
        "--estimate",
        action="store_true",
        help="report image count x candidates x minutes and the plan-limit "
        "warning; run nothing. Still checks the Codex login",
    )
    p_generate.add_argument(
        "--concurrency", type=int, default=1, help="parallel Codex processes (default: 1)"
    )
    p_generate.add_argument(
        "--target-ids",
        help="comma-separated job ids or image names to run (default: every job "
        "with a candidate still missing)",
    )
    p_generate.add_argument("--cli-bin", default=None, help="path to codex if not on PATH")
    p_generate.add_argument(
        "--model", default=None, help="Codex model override (default: ~/.codex/config.toml)"
    )
    p_generate.add_argument(
        "--timeout-minutes",
        type=float,
        default=None,
        help="per-candidate ceiling (default: 20)",
    )

    p_triage = sub.add_parser(
        "triage", help="record what you made of each image you looked at (no spend)"
    )
    p_triage.add_argument("--project", required=True, help="project id or path")
    p_triage.add_argument(
        "--json-file",
        required=True,
        help="a list of {image, verdict, finding, labels?, lettering?}. verdict is "
        "translate | restore | cover | replace | leave; finding is what you saw; "
        "labels is every piece of lettering you could read and its replacement",
    )
    p_triage.add_argument(
        "--replace",
        action="store_true",
        help="make the triage exactly this batch (default: merge by image)",
    )

    p_check = sub.add_parser(
        "check", help="record what is wrong, or not, with each candidate (no spend)"
    )
    p_check.add_argument("--project", required=True, help="project id or path")
    p_check.add_argument(
        "--json-file",
        required=True,
        help="a list of {image, candidate, ok, finding}. finding is required when "
        "ok is false; it is shown under that candidate on the board",
    )

    p_composite = sub.add_parser(
        "composite",
        help="make a candidate from two pictures: a patch of one candidate inside "
        "an outline, another picture's pixels everywhere else (no spend)",
    )
    p_composite.add_argument("--project", required=True, help="project id or path")
    p_composite.add_argument(
        "--json-file",
        required=True,
        help="a list of {image, from, base?, regions, feather?, scale?, note?, "
        "candidate?}. from "
        "is the candidate number the patch comes from (or a path under the job's "
        "previous/ folder); base is what everything else comes from: 'original' "
        "(default), 'current', a candidate number or a previous/ path; regions "
        "is a list of [x0, y0, x1, y1] or [[x, y], ...] outlines in the base "
        f"picture's pixels; feather softens the join (default "
        f"{ip_composite.DEFAULT_FEATHER:g} px); scale (1-{ip_composite.MAX_SCALE}) "
        "enlarges the base first so small new lettering keeps its detail; "
        "candidate names an existing composite to make again in place",
    )
    p_composite.add_argument(
        "--dry-run",
        action="store_true",
        help="measure and report; write no candidate",
    )

    p_board = sub.add_parser(
        "board",
        help="where the image board is, the candidates to look at, and what the "
        "user asked for and picked on it",
    )
    p_board.add_argument("--project", required=True, help="project id or path")
    p_board.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"where the web UI is listening (default: {DEFAULT_BASE_URL})",
    )

    p_apply = sub.add_parser(
        "apply", help="back up, convert to the original's name and format, replace"
    )
    p_apply.add_argument("--project", required=True, help="project id or path")
    p_apply_from = p_apply.add_mutually_exclusive_group(required=True)
    p_apply_from.add_argument(
        "--from-board",
        action="store_true",
        help="apply the picks the user made on the image board",
    )
    p_apply_from.add_argument(
        "--json-file",
        help="a list of {image, candidate: N} to accept, or {image, verdict: "
        "'skip'|'redo', note}",
    )
    p_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report what would be replaced; write nothing",
    )
    p_apply.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="longest side, in pixels, to downscale a candidate to (default: twice "
        "the original's, at least 1024; 2560 for a cover)",
    )

    p_revert = sub.add_parser("revert", help="restore originals from images_original/")
    p_revert.add_argument("--project", required=True, help="project id or path")
    p_revert.add_argument(
        "--images", required=True, help="comma-separated image names, or `all`"
    )

    p_verify = sub.add_parser(
        "verify", help="tokens resolve, backups intact, ledger consistent (no spend)"
    )
    p_verify.add_argument("--project", required=True, help="project id or path")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _set_output_dir(args)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
