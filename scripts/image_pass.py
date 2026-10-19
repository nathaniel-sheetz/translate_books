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
    cover-compose  the band, title and emblem over the cover's art (no spend)
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
    python scripts/image_pass.py generate  --project home-geography --limit 10
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
import functools
import json
import re
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
from src.image_pass import cover_compose as ip_cover  # noqa: E402
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
        fetch=functools.partial(gutenberg.fetch_bytes, base_url=base_url),
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
        limit=args.limit,
        cli_bin=args.cli_bin,
        model=args.model,
        timeout_s=args.timeout_minutes * 60 if args.timeout_minutes else None,
        # stdout is the one JSON object; a batch that runs for minutes says how
        # far it has got on stderr.
        progress=lambda line: print(line, file=sys.stderr, flush=True),
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


def _json_value(text: str) -> Any:
    """``0.2`` is a number and ``[0, 0.1]`` a list; anything else is the text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _pairs(values: list[str] | None, flag: str) -> list[tuple[str, str]]:
    pairs = []
    for raw in values or []:
        name, sep, value = raw.partition("=")
        if not sep or not name.strip():
            _die(f"{flag} takes name=value, not {raw!r}")
        pairs.append((name.strip(), value.strip()))
    return pairs


_COVER_TEXT_FIELDS = ("title", "subtitle", "author", "translator")


def _aspect_override(text: str, flag: str) -> Any:
    if text.lower() in ("template", ""):
        return None
    if text.lower() == ip_cover.ASPECT_ART:
        return ip_cover.ASPECT_ART
    try:
        wide, tall = (float(part) for part in text.split(":"))
    except ValueError:
        _die(f"{flag} takes W:H, `art` or `template`, not {text!r}")
    return [wide, tall]


def _set_override(overrides: dict[str, Any], name: str, value: str) -> None:
    # `band_right=0.2` is a variable; `title.at=[0.03,0.02]` a layer's field.
    layer, dot, field = name.partition(".")
    parsed = _json_value(value) if value else None
    if dot:
        overrides.setdefault("layers", {}).setdefault(layer, {})[field] = parsed
    else:
        overrides.setdefault("vars", {})[name] = parsed


def _cover_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """What this invocation changes in the book's cover spec."""
    overrides: dict[str, Any] = {}
    for field in _COVER_TEXT_FIELDS:
        value = getattr(args, field)
        if value is not None:
            # `|` is a line break: "Edmund Spenser|Jeanie Lang" is two lines.
            # An empty one forgets the field, except the author: with none in
            # the spec the project's is drawn, so "no author" is kept as "".
            # `--unset author` goes back to the project's.
            cleared = "" if field == "author" else None
            overrides[field] = value.replace("|", chr(10)) or cleared
    if args.title_lines is not None:
        lines = [part.strip() for part in args.title_lines.split("|") if part.strip()]
        overrides["title_lines"] = lines or None
    if args.aspect is not None:
        overrides["aspect"] = _aspect_override(args.aspect, "--aspect")
    if args.art_top is not None:
        overrides["art_top"] = None if args.art_top.lower() == "auto" else _json_value(args.art_top)
    for name, value in _pairs(args.color, "--color"):
        overrides.setdefault("colors", {})[name] = value or None
    for name, value in _pairs(args.set, "--set"):
        _set_override(overrides, name, value)
    for name in _split_ids(args.unset) or []:
        overrides[name] = None
    return overrides


def _cover_try(text: str) -> dict[str, Any]:
    """One ``--try``: the settings to lay over the spec for one draft.

    ``art_top=0.05,band_right=0.2`` is two settings of one cover. Each is named
    as its own flag names it: ``art_top``, ``aspect``, ``title_lines`` and the
    text fields are the spec's, a ``#rrggbb`` value is one of the template's
    colours, and anything else is what ``--set`` takes.
    """
    overrides: dict[str, Any] = {}
    # A comma starts a setting only where a `name=` follows it. One inside
    # brackets (`title.at=[0.03,0.02]`) or a title (`title=Hola, mundo`) is
    # part of the value.
    for part in re.split(r",(?=\s*[\w.]+\s*=)(?![^\[]*\])", text):
        if not part.strip():
            continue
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            _die(f"--try takes name=value[,name=value], not {part.strip()!r}")
        if name == "art_top":
            overrides[name] = None if value.lower() == "auto" else _json_value(value)
        elif name == "aspect":
            overrides[name] = _aspect_override(value, "--try aspect=")
        elif name == "title_lines":
            lines = [line.strip() for line in value.split("|") if line.strip()]
            overrides[name] = lines or None
        elif name in _COVER_TEXT_FIELDS:
            overrides[name] = value.replace("|", chr(10)) or None
        elif value.startswith("#"):
            overrides.setdefault("colors", {})[name] = value
        else:
            _set_override(overrides, name, value)
    if not overrides:
        _die("--try needs at least one name=value")
    return {"label": text.strip(), "overrides": overrides}


def _cmd_cover_compose(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    from_draft, approved_id, design = args.from_draft, None, None
    if args.from_board:
        design = ip_board.cover_design(project_dir)
        if design is None:
            _die(
                "nothing is waiting on the board for the cover: the user chooses a "
                "draft or approves one on the New images tab first"
            )
        if design["stale"]:
            _die(
                "what the user chose on the board is out of date ("
                + "; ".join(design["problems"]) + "): give them the board url and "
                "have them choose again"
            )
        if args.apply and not design["approve"]:
            _die(
                "the user has not approved a draft on the board: --from-board --apply "
                "lands only a cover they approved there. Without --from-board, --apply "
                "is on their word in the conversation, as before."
            )
        # What they chose and has been done is not chosen again: going back to
        # a draft that is already in the book would undo every proof since.
        if not design["waiting"]:
            _die(
                "nothing is waiting on the board for the cover: what the user chose "
                "there has been done. A proof composed since is one they have not "
                "chosen or approved yet"
            )
        if args.apply and "not_applied" not in design["waiting"]:
            _die(
                "the draft the user approved on the board is already the book's "
                "cover: there is nothing to apply. Show them the proof as it "
                "stands and have them approve that."
            )
        outstanding = {"draft_not_adopted", "not_applied"} & set(design["waiting"])
        if from_draft is None and outstanding:
            from_draft = design["draft"]
        if args.apply:
            approved_id = design["id"]
    overrides = _cover_overrides(args)
    tries = [_cover_try(text) for text in args.tries or []] or None
    try:
        out = ip_cover.compose(
            project_dir,
            overrides,
            art=Path(args.art) if args.art else None,
            cover_is_art=args.cover_is_art,
            swatches=args.swatches,
            tries=tries,
            from_draft=from_draft,
            approved_id=approved_id,
            apply=args.apply,
            dry_run=args.dry_run,
            template=Path(args.template) if args.template else None,
        )
    except Exception as exc:  # noqa: BLE001 - one JSON error, never a traceback
        _die(f"cover-compose failed: {type(exc).__name__}: {exc}")
    if design is not None:
        out["from_board"] = {
            field: design[field] for field in ("draft", "approve", "note", "waiting")
        }
    _emit(out, _COVER_COMPOSE_SCHEMA)
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
        "what `apply --from-board` would apply. cover is where the cover stands "
        "and what the user chose among its drafts: `cover-compose --from-board` "
        "takes it."
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
    "plan": "{cli, models, images, candidates, already_have, held_back, "
    "concurrency, workers, minutes_per_image, minutes_slowest, minutes_measured, "
    "sequential_minutes, estimated_minutes, jobs}. models is {model id: runs on "
    "it}. workers is how many run at once (never more than there are "
    "candidates); estimated_minutes is at that many, sequential_minutes one at a "
    "time. held_back is what --limit left for the next run. jobs names each "
    "missing candidate and the model it will run on",
    "limit_warning": "--estimate only: the plan-limit burn, to quote at the consent gate",
    "wrote": "candidates harvested: {id, candidate, mode, model, path, width, "
    "height, wall_s, harvested_from, images_generated?}. model is null on "
    "Codex's default. images_generated appears only when the model called the "
    "image tool more than once — that candidate cost that many images of plan "
    "usage",
    "failed": "candidates that ran and produced nothing, each with its error",
    "counts": "{wrote, failed, not_run, todo}",
    "elapsed_s": "how long the batch took",
    "wall_s_summed": "what the same runs would have taken one at a time",
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

_COVER_COMPOSE_SCHEMA = {
    "status": "'ok' | 'error' (read error; images/ was not touched)",
    "dry_run": "true when no file was written",
    "applied": "true when images/cover.* is now the composed cover",
    "proof": "cover/proof.png: the composed cover. Read it before showing it",
    "draft": "the number this proof is kept under in cover/drafts/. The same "
    "cover composed twice keeps its number",
    "swatches": "--swatches only: cover/swatches.png, the cover once per "
    "suggested_colors entry, numbered left to right",
    "swatch_drafts": "--swatches only: [{draft, color}], the same covers one by "
    "one, as the board shows them. `--from-draft N` takes that colour",
    "tried": "--try only: per attempt {label, status: 'ok' | 'does_not_fit' | "
    "'error', draft, path, reasons, notes, error}. Read each path; the spec was "
    "not changed by any of them",
    "from_board": "--from-board only: {draft, approve, note, waiting} as the user "
    "left them on the board. Act on note yourself: no flag carries it",
    "art": "{from, path, size, kept_at?}: which picture was drawn on and why "
    "that one. kept_at is where it was copied to for every later compose",
    "title_from": "'spec' | 'project (translated title)' | 'project' (no title "
    "was given or recorded; the project's own was used, which may be the English one)",
    "band_color": "the colour the band was drawn in",
    "band_color_from": "'spec' (picked for this book) | 'suggested' (the first "
    "suggestion, not saved: --apply refuses until one is picked) | 'template'",
    "suggested_colors": "band colours taken from the art that the lettering reads on",
    "size": "[width, height] of the composed cover",
    "lines": "per text layer, the lines as they were broken",
    "vars": "the template's variables as solved for this book",
    "art_top": "where the art's top edge sits, as a fraction of the cover's height",
    "side_crop": "share of the art's width cropped (0 unless it was too wide "
    "to reach the bottom edge under the deepest band)",
    "reasons": "why a variable is not the template's own value: what the art "
    "or the title needed",
    "notes": "what was done to the art's placement that was not asked for",
    "warnings": "things to tell the user before they look at the proof",
    "misfits": "on a does-not-fit error: {layer, line, problem} for each line "
    "that would not stay inside its shape",
    "spec_path": "cover/spec.json, where this book's overrides are kept",
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
    "leave, lettering, with_input, flagged, design_waiting, requests, picks, "
    "stale_picks, candidates, unchecked_candidates}",
    "cover": "the cover as the page's New images tab shows it: {image, status: "
    "'art' | 'colour' | 'layout' | 'approve' | 'done' (the step it is at), steps: "
    "{art, colour, layout, book} with each one's state, proof (cover/proof.png, "
    "or null), proof_draft, book_draft, drafts (how many are kept), design}. "
    "design is what the user chose there, or null: {draft, approve, note, "
    "waiting, spent, stale, problems}. waiting: draft_not_adopted (run "
    "`cover-compose --from-board`) | not_applied (they approved it: add --apply) "
    "| note_unread (read note and act on it; the next proof clears it). spent: "
    "the draft they approved is in the book and a different cover has been "
    "composed since, which they have not approved. stale: the draft is gone or "
    "was approved on other art, and they have to choose again",
    "untriaged": "images nobody has recorded a verdict for",
    "proposed": "images that want a job and have none: {image, verdict, by}. by is "
    "'user' when the verdict is one they set on the page, over the triage or in "
    "place of it. An image they set to leave alone is simply not here",
    "jobs": "every prepared job: {id, image, mode, stage, original, original_size, "
    "reference, labels, candidates: [{candidate, path, size, flags, checked, "
    "composite, model}], missing}. flags names a candidate whose proportions are "
    "off the original's; composite is {from, base, regions, changed_share} for a "
    "candidate `composite` made, else null; model is the Codex model that made "
    "it, null for a composite or a run on Codex's default",
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
    "cover-compose": _cmd_cover_compose,
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
        "reference?, model?}. model is the Codex model for this job, or a list "
        "to give each candidate a different one. "
        "mode is translate | restore | cover | replace; labels is the approved "
        'source → target map ({"NORTH": "NORTE"}), required for translate; '
        f"candidates is 1-{ip_jobs.MAX_CANDIDATES} (default {ip_jobs.DEFAULT_CANDIDATES}); "
        "input is 'original' (default) or 'current'; reference (cover or replace) is "
        "the picture to draw from instead of the job's own original: one of the "
        "book's images, cover/art.jpg, or a job's candidate (jobs/<id>/cand_01.png)",
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
        "--concurrency",
        type=int,
        default=ip_jobs.DEFAULT_CONCURRENCY,
        help="parallel Codex processes, at most one per candidate (default: "
        f"{ip_jobs.DEFAULT_CONCURRENCY}). The usage spent is the same at any number",
    )
    p_generate.add_argument(
        "--target-ids",
        help="comma-separated job ids or image names to run (default: every job "
        "with a candidate still missing)",
    )
    p_generate.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run only the first N missing candidates: a first wave to look at "
        "before the rest. Running `generate` again takes up where it stopped",
    )
    p_generate.add_argument("--cli-bin", default=None, help="path to codex if not on PATH")
    p_generate.add_argument(
        "--model",
        default=None,
        help="Codex model for jobs that name none, or several comma-separated to "
        "give each candidate of a job a different one (default: the book's "
        f"`{ip_jobs.MODEL_CONFIG_KEY}` config key, else ~/.codex/config.toml)",
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

    p_cover = sub.add_parser(
        "cover-compose",
        help="lay the house cover over the art: the band with the title on it and "
        "the emblem. Writes cover/proof.png; --apply puts it in images/ (no spend)",
    )
    p_cover.add_argument("--project", required=True, help="project id or path")
    p_cover.add_argument(
        "--art",
        help="the picture with no lettering on it. Kept as cover/art.<ext> and "
        "reused from then on; without it, the cover the image pass applied is taken",
    )
    p_cover.add_argument(
        "--cover-is-art",
        action="store_true",
        help="images/cover.* has no lettering on it, though the ledger does not "
        "say the image pass made it: take it as the art",
    )
    p_cover.add_argument("--title", help="the title as the cover prints it; `|` breaks a line")
    p_cover.add_argument(
        "--title-lines",
        help="the title's lines, `|` between them, instead of letting it wrap; "
        "an empty string goes back to wrapping",
    )
    p_cover.add_argument(
        "--subtitle",
        help="a line under the title in its own, smaller size (--set "
        "subtitle_size=0.04); an empty string takes it off again",
    )
    p_cover.add_argument("--author", help='the author line; `|` between two authors, "" for none')
    p_cover.add_argument("--translator", help="the translator's name")
    p_cover.add_argument(
        "--color",
        action="append",
        metavar="NAME=#RRGGBB",
        help="one of the template's colours for this book, e.g. band=#874727 (repeatable)",
    )
    p_cover.add_argument(
        "--aspect",
        help="this cover's proportions: W:H (5:8); `art` to take the picture's own "
        "shape, made taller by --art-top so that sliding the picture down crops "
        "none of it; `template` to go back to the design's",
    )
    p_cover.add_argument(
        "--art-top",
        help="where the art's top edge sits, as a fraction of the cover's height: "
        "0 at the top edge, more to start it lower under the band, less than 0 to "
        "lose its top instead of its bottom; `auto` to let the command place it",
    )
    p_cover.add_argument(
        "--set",
        action="append",
        metavar="NAME=VALUE",
        help="override one of the template's variables (band_right=0.2) or one "
        "field of a layer (title.at=[0.03,0.02]) for this book; an empty value "
        "removes the override (repeatable)",
    )
    p_cover.add_argument(
        "--unset",
        help="comma-separated spec keys to drop, e.g. title_lines,art_top",
    )
    p_cover.add_argument(
        "--swatches",
        action="store_true",
        help="also write cover/swatches.png: the cover in each band colour "
        "suggested from the art, to choose between",
    )
    p_cover.add_argument(
        "--try",
        dest="tries",
        action="append",
        metavar="NAME=VALUE[,NAME=VALUE]",
        help="also draw the cover with these settings laid over the book's, as a "
        "draft to compare on the board, and leave the spec as it was: "
        "art_top=0.05, band_right=0.2,title_size=0.06, band=#874727 (repeatable: "
        "one draft each)",
    )
    p_cover.add_argument(
        "--from-draft",
        type=int,
        metavar="N",
        help="start from the settings draft N was drawn with, instead of the spec "
        "as it stands; other flags are laid over them",
    )
    p_cover.add_argument(
        "--from-board",
        action="store_true",
        help="take the draft the user chose on the image board; with --apply, "
        "land the cover only if it is the draft they approved there",
    )
    p_cover.add_argument(
        "--apply",
        action="store_true",
        help="put the composed cover in images/, behind the backup and the ledger",
    )
    p_cover.add_argument(
        "--dry-run",
        action="store_true",
        help="solve and report the layout; write nothing",
    )
    p_cover.add_argument(
        "--template",
        help="a cover template other than assets/cover_template/template.json",
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
