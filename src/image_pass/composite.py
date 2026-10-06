"""
``composite`` — a candidate made of two pictures: one inside an outline, the
other everywhere else.

The image tool does not edit a picture, it redraws it. Asked to change two lines
of lettering on a van it also re-rendered the clouds and gave a man a sharper
face; asked to fix one label on a map of thirty-one it got that label right and
dropped another (stormy-misty-s-foal, 2026-10-06). No instruction and no model
changed that. What does is taking from a candidate only the patch that was meant
to change and keeping the pixels of a picture already trusted — the publisher's
original, or an earlier candidate — for the rest.

A composite is an ordinary candidate once it exists: ``cand_NN.png`` in the
job's folder, shown on the board beside the original, checked, picked and
applied like any other. Nothing here writes into ``images/``. Beside it sits
``cand_NN.composite.json``, which says what it was made from and where the
outline runs; the board reads it to draw that outline over the picture.

Like ``prepare``, a batch is all-or-nothing, and nothing here spends usage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from src.image_pass import image_key, images_dir, originals_dir
from src.image_pass import ledger
from src.image_pass.jobs import (
    MAX_CANDIDATES,
    candidate_path,
    existing_candidates,
    load_manifest,
)

BASE_ORIGINAL = "original"
BASE_CURRENT = "current"

# Composites are numbered after the last candidate `generate` can fill, so one
# never sits in a slot a later "two candidates, please" would count as done.
FIRST_NUMBER = MAX_CANDIDATES + 1

DEFAULT_FEATHER = 2.0
MAX_FEATHER = 20.0
MAX_SCALE = 4

# How far either side of the fitted position, as a share of the base's longest
# side, the patch is slid to find where its surroundings match best. Two
# renderings of one drawing sit a few pixels apart, never tens.
_SEARCH_SHARE = 0.008
_MIN_SEARCH = 3
# The band around an outline whose pixels are compared to place the patch.
_RING = 12
# Mean grey-level difference in that band past which the two pictures are not
# showing the same thing there, whatever the best position was.
_RING_TOLERANCE = 30.0
# A pixel counts as changed past this many grey levels: under it is JPEG noise.
_CHANGED_LEVELS = 8

_ROW_KEYS = frozenset(
    {"image", "from", "base", "regions", "feather", "scale", "note", "candidate"}
)


def sidecar_path(candidate: Path) -> Path:
    """Where a composite candidate's description lives: beside the picture."""
    return candidate.with_name(candidate.stem + ".composite.json")


def load_sidecar(candidate: Path) -> Optional[dict[str, Any]]:
    """The description of ``candidate`` if it is a composite and still the file
    the description was written for; otherwise ``None``."""
    try:
        doc = json.loads(sidecar_path(candidate).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("sha256") != ledger.sha256_file(candidate):
        return None
    return doc


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _polygon(region: Any) -> Optional[list[tuple[float, float]]]:
    """A region as corner points: ``[x0, y0, x1, y1]`` is a rectangle, a list of
    three or more ``[x, y]`` pairs a polygon. ``None`` for anything else."""
    if not isinstance(region, list):
        return None
    if len(region) == 4 and all(_is_number(v) for v in region):
        x0, y0, x1, y1 = (float(v) for v in region)
        if x1 <= x0 or y1 <= y0:
            return None
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if len(region) >= 3 and all(
        isinstance(p, list) and len(p) == 2 and all(_is_number(v) for v in p) for p in region
    ):
        return [(float(x), float(y)) for x, y in region]
    return None


def _resolve(
    project_dir: Path, job: dict[str, Any], spec: Any, *, allow_book_files: bool
) -> tuple[Optional[Path], Optional[dict[str, Any]], Optional[str]]:
    """``(path, description, problem)`` for one side of a composite.

    ``spec`` is a candidate number, a path under the job's ``previous/`` folder
    (an earlier prompt's candidate, as ``prepare`` archived it) or, for the
    base, ``"original"`` / ``"current"``.
    """
    job_dir = Path(job["job_dir"])
    key = job["image"]
    if isinstance(spec, int) and not isinstance(spec, bool):
        if spec < 1:
            return None, None, "a candidate is named by its 1-based number"
        path = candidate_path(job_dir, spec)
        if not path.is_file():
            have = existing_candidates(job_dir)
            return None, None, f"candidate {spec} does not exist (have: {have or 'none'})"
        return path, {"kind": "candidate", "candidate": spec}, None
    if not isinstance(spec, str) or not spec.strip():
        return None, None, "name a candidate number or a file under the job's previous/ folder"
    name = spec.strip()
    if allow_book_files and name == BASE_ORIGINAL:
        backup = originals_dir(project_dir) / key
        path = backup if backup.is_file() else images_dir(project_dir) / key
        if not path.is_file():
            return None, None, f"{key} is not on disk: there is no original to keep"
        return path, {"kind": BASE_ORIGINAL}, None
    if allow_book_files and name == BASE_CURRENT:
        path = images_dir(project_dir) / key
        if not path.is_file():
            return None, None, f"{key} is not on disk"
        return path, {"kind": BASE_CURRENT}, None
    relative = name.replace("\\", "/")
    parts = relative.split("/")
    if parts[0] != "previous" or any(part in ("", ".", "..") for part in parts) or ":" in relative:
        return None, None, (
            f"{spec!r} is not a candidate number"
            + (", 'original', 'current'" if allow_book_files else "")
            + " or a path under the job's previous/ folder"
        )
    path = job_dir.joinpath(*parts)
    if not path.is_file():
        return None, None, f"{relative} does not exist in the job's folder"
    return path, {"kind": "previous", "path": relative}, None


def _open_rgb(path: Path):
    """The picture flattened onto white, the colour of the page."""
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            return flat
        return image.convert("RGB")


def _validate_row(
    project_dir: Path, raw: Any, jobs: dict[str, dict[str, Any]]
) -> tuple[Optional[dict[str, Any]], list[str]]:
    if not isinstance(raw, dict):
        return None, ["row is not an object"]
    problems: list[str] = []
    unknown = sorted(set(raw) - _ROW_KEYS)
    if unknown:
        problems.append(f"unknown field(s) {unknown}; a row is {sorted(_ROW_KEYS)}")
    key = image_key(raw.get("image") or "")
    job = jobs.get(key)
    if job is None:
        return None, problems + [f"{raw.get('image')!r}: no prepared job for this image"]

    source, source_from, problem = _resolve(project_dir, job, raw.get("from"), allow_book_files=False)
    if problem:
        problems.append(f"from: {problem}")
    base, base_from, problem = _resolve(
        project_dir, job, raw.get("base", BASE_ORIGINAL), allow_book_files=True
    )
    if problem:
        problems.append(f"base: {problem}")
    if source and base and source.resolve() == base.resolve():
        problems.append("from and base are the same file: there is nothing to combine")

    feather = raw.get("feather", DEFAULT_FEATHER)
    if not _is_number(feather) or not 0 <= feather <= MAX_FEATHER:
        problems.append(f"feather must be a number from 0 to {MAX_FEATHER:g} (base pixels)")
    scale = raw.get("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, int) or not 1 <= scale <= MAX_SCALE:
        problems.append(f"scale must be a whole number from 1 to {MAX_SCALE}")
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        problems.append("note must be text")
    # An outline is rarely right the first time. Naming a composite's number
    # makes it again in place; anything Codex drew is never overwritten.
    number = raw.get("candidate")
    if number is not None:
        if isinstance(number, bool) or not isinstance(number, int) or number < FIRST_NUMBER:
            problems.append(
                f"candidate must be a composite's number ({FIRST_NUMBER} or more): "
                "leave it out to make a new one"
            )
        else:
            existing = candidate_path(Path(job["job_dir"]), number)
            if existing.is_file() and not sidecar_path(existing).is_file():
                problems.append(f"candidate {number} is not a composite: it will not be overwritten")
            elif source and existing.resolve() == source.resolve():
                problems.append(f"candidate {number} cannot be remade from itself")
            elif base and existing.resolve() == base.resolve():
                problems.append(f"candidate {number} cannot be remade over itself")

    regions = raw.get("regions")
    polygons: list[list[tuple[float, float]]] = []
    if not isinstance(regions, list) or not regions:
        problems.append(
            "regions must be a non-empty list: each one [x0, y0, x1, y1] or a list "
            "of [x, y] corners, in the base picture's pixels"
        )
    else:
        for position, region in enumerate(regions):
            polygon = _polygon(region)
            if polygon is None:
                problems.append(
                    f"regions[{position}] is not [x0, y0, x1, y1] (with x1 > x0 and "
                    "y1 > y0) or a list of three or more [x, y] corners"
                )
            else:
                polygons.append(polygon)
    if problems:
        return None, problems
    assert source is not None and base is not None

    try:
        base_image, source_image = _open_rgb(base), _open_rgb(source)
    except Exception as exc:  # noqa: BLE001 - an unreadable picture is one outcome
        return None, [f"could not read a picture: {exc}"]
    width, height = base_image.size
    for position, polygon in enumerate(polygons):
        if any(not (0 <= x <= width and 0 <= y <= height) for x, y in polygon):
            problems.append(
                f"regions[{position}] runs outside the base picture, which is "
                f"{width} wide by {height} high"
            )
    if problems:
        return None, problems
    return {
        "image": key,
        "job": job,
        "source": source,
        "source_from": source_from,
        "source_image": source_image,
        "base": base,
        "base_from": base_from,
        "base_image": base_image,
        "polygons": polygons,
        "feather": float(feather),
        "scale": scale,
        "note": note.strip() if isinstance(note, str) and note.strip() else None,
        "number": number,
    }, []


# ---------------------------------------------------------------------------
# the picture
# ---------------------------------------------------------------------------

def _ring_difference(base_grey, patch_grey, ring) -> float:
    from PIL import ImageChops, ImageStat

    return ImageStat.Stat(ImageChops.difference(base_grey, patch_grey), mask=ring).mean[0]


def compose(
    base, source, polygons: list[list[tuple[float, float]]], *, feather: float, scale: int = 1
) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
    """Paste ``source`` into ``base`` inside each polygon. Returns the picture,
    one report per region and what was measured of the whole.

    ``source`` is fitted over ``base`` the way the image tool hands a picture
    back: one scale for both directions, centred, so a strip it padded with
    white bands lines up without them. Each patch is then slid a few pixels to
    wherever the band of drawing around its outline differs least from the
    base, because two renderings of one drawing never sit exactly together.
    """
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    if scale > 1:
        base = base.resize((base.width * scale, base.height * scale), Image.Resampling.LANCZOS)
        polygons = [[(x * scale, y * scale) for x, y in polygon] for polygon in polygons]
    feather *= scale
    width, height = base.size
    fit = min(source.width / width, source.height / height)
    left = (source.width - width * fit) / 2
    top = (source.height - height * fit) / 2
    aligned = source.resize(
        (width, height),
        Image.Resampling.LANCZOS,
        box=(left, top, left + width * fit, top + height * fit),
    )

    search = max(_MIN_SEARCH, round(max(width, height) * _SEARCH_SHARE))
    ring_width = _RING * scale
    grow = int(round(feather * 2)) + 2
    out = base.copy()
    base_grey, aligned_grey = base.convert("L"), aligned.convert("L")
    # Every outline at once: what lies inside another region differs on purpose
    # too, and must not be what this one is placed by.
    everything = Image.new("L", (width, height), 0)
    for polygon in polygons:
        ImageDraw.Draw(everything).polygon(polygon, fill=255)
    if grow:
        everything = everything.filter(ImageFilter.MaxFilter(2 * grow + 1))
    reports: list[dict[str, Any]] = []
    for polygon in polygons:
        xs, ys = [x for x, _ in polygon], [y for _, y in polygon]
        box = (
            max(0, int(min(xs)) - ring_width - grow),
            max(0, int(min(ys)) - ring_width - grow),
            min(width, int(max(xs)) + 1 + ring_width + grow),
            min(height, int(max(ys)) + 1 + ring_width + grow),
        )
        local = [(x - box[0], y - box[1]) for x, y in polygon]
        size = (box[2] - box[0], box[3] - box[1])
        shape = Image.new("L", size, 0)
        ImageDraw.Draw(shape).polygon(local, fill=255)
        # What is compared is the band outside the outlines and clear of their
        # feathered edges: inside them the two pictures differ on purpose.
        ring = ImageChops.invert(everything.crop(box))

        offset, residual, at_zero = (0, 0), None, None
        if ring.getbbox() is not None:
            base_crop = base_grey.crop(box)
            scores: dict[tuple[int, int], float] = {}
            for dy in range(-search, search + 1):
                for dx in range(-search, search + 1):
                    moved = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                    if moved[0] < 0 or moved[1] < 0 or moved[2] > width or moved[3] > height:
                        continue
                    scores[(dx, dy)] = _ring_difference(base_crop, aligned_grey.crop(moved), ring)
            # Nearest the fitted position among equals: a flat surround scores
            # the same everywhere and must not send the patch to a corner.
            offset = min(scores, key=lambda o: (round(scores[o], 3), abs(o[0]) + abs(o[1])))
            residual, at_zero = scores[offset], scores[(0, 0)]

        moved = (box[0] + offset[0], box[1] + offset[1], box[2] + offset[0], box[3] + offset[1])
        mask = shape.filter(ImageFilter.GaussianBlur(feather)) if feather else shape
        out.paste(aligned.crop(moved), box[:2], mask)
        reports.append({
            "polygon": [[round(x, 1), round(y, 1)] for x, y in polygon],
            "bbox": [int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1],
            "offset": list(offset),
            "at_search_edge": search in (abs(offset[0]), abs(offset[1])),
            "surround_difference": None if residual is None else round(residual, 1),
            "surround_difference_unmoved": None if at_zero is None else round(at_zero, 1),
        })

    # The largest difference in any channel: two colours of one brightness are
    # still two colours.
    red, green, blue = ImageChops.difference(out, base).split()
    histogram = ImageChops.lighter(ImageChops.lighter(red, green), blue).histogram()
    changed = sum(histogram[_CHANGED_LEVELS + 1:]) / float(width * height)
    measured = {
        "size": [width, height],
        "changed_share": round(changed, 4),
        "padded_source": abs(source.width / source.height - width / height)
        / (width / height) > 0.05,
        "search": search,
    }
    return out, reports, measured


def _next_number(job: dict[str, Any]) -> int:
    have = existing_candidates(Path(job["job_dir"]))
    return max([FIRST_NUMBER - 1, job.get("candidates") or 0, *have]) + 1


def composite(project_dir: Path, rows: list[Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Make one composite candidate per row. Spends nothing; writes only into
    the job's folder.

    A row is ``{image, from, base?, regions, feather?, scale?, note?,
    candidate?}``: take what lies inside ``regions`` from candidate ``from`` and
    everything else from ``base`` (default: the publisher's original).
    ``regions`` are in the base picture's pixels. ``candidate`` names an
    existing composite to make again in place, after moving an outline.
    """
    project_dir = Path(project_dir)
    if not rows:
        return {"status": "error", "error": "no composite rows given"}
    jobs = {job["image"]: job for job in load_manifest(project_dir)["jobs"]}

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for position, raw in enumerate(rows):
        row, problems = _validate_row(project_dir, raw, jobs)
        if row is None:
            invalid.append({
                "index": position,
                "image": raw.get("image") if isinstance(raw, dict) else None,
                "problems": problems,
            })
        else:
            valid.append(row)
    if invalid:
        return {
            "status": "error",
            "error": f"{len(invalid)} of {len(rows)} composite row(s) are invalid; nothing was made",
            "invalid": invalid,
            "counts": {"requested": len(rows), "invalid": len(invalid), "made": 0},
            "instructions": "Fix the named problems and re-run with the whole batch.",
        }

    made: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    # Two rows for one image in a batch take consecutive numbers.
    taken: dict[str, int] = {}
    for row in valid:
        job = row["job"]
        picture, regions, measured = compose(
            row["base_image"], row["source_image"], row["polygons"],
            feather=row["feather"], scale=row["scale"],
        )
        number = row["number"] or max(_next_number(job), taken.get(row["image"], 0) + 1)
        taken[row["image"]] = max(number, taken.get(row["image"], 0))
        target = candidate_path(Path(job["job_dir"]), number)
        summary = {
            "image": row["image"],
            "candidate": number,
            "path": str(target),
            "from": row["source_from"],
            "base": row["base_from"],
            "regions": regions,
            **measured,
        }
        for position, region in enumerate(regions):
            difference = region["surround_difference"]
            if difference is not None and difference > _RING_TOLERANCE:
                warnings.append({
                    "image": row["image"], "candidate": number, "code": "surroundings_differ",
                    "detail": (
                        f"regions[{position}]: around the outline the two pictures "
                        f"differ by {difference:g} grey levels of 255 on average. "
                        "The patch may not sit where it should, or the outline cuts "
                        "through something that was redrawn"
                    ),
                })
            if region["at_search_edge"]:
                warnings.append({
                    "image": row["image"], "candidate": number, "code": "alignment_at_edge",
                    "detail": (
                        f"regions[{position}]: the best position found was the "
                        f"furthest one looked at ({region['offset']}): the two "
                        "pictures may be further apart than that"
                    ),
                })
        if measured["changed_share"] == 0:
            warnings.append({
                "image": row["image"], "candidate": number, "code": "nothing_changed",
                "detail": "the composite is the base, pixel for pixel",
            })

        if not dry_run:
            tmp = target.with_suffix(".tmp.png")
            picture.save(tmp, format="PNG")
            os.replace(tmp, target)
            sidecar = {
                "version": 1,
                "created": ledger.now_stamp(),
                "sha256": ledger.sha256_file(target),
                "from": {**row["source_from"], "sha256": ledger.sha256_file(row["source"])},
                "base": {**row["base_from"], "sha256": ledger.sha256_file(row["base"])},
                "regions": [region["polygon"] for region in regions],
                "offsets": [region["offset"] for region in regions],
                "feather": row["feather"],
                "scale": row["scale"],
                "size": measured["size"],
                "changed_share": measured["changed_share"],
                "note": row["note"],
            }
            side = sidecar_path(target)
            tmp = side.with_name(f".{side.name}.tmp")
            tmp.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, side)
        made.append(summary)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "made": [] if dry_run else made,
        "planned": made if dry_run else [],
        "warnings": warnings,
        "counts": {
            "requested": len(rows),
            "made": 0 if dry_run else len(made),
            "planned": len(made) if dry_run else 0,
            "invalid": 0,
            "warnings": len(warnings),
        },
        "instructions": (
            "Nothing was written. Relay warnings, then re-run without --dry-run."
            if dry_run
            else "Each composite is a new candidate. Read it beside its base at "
            "the outline's edge, record what you find with `check`, then give the "
            "user the board url: the outline is drawn over the candidate there."
        ),
    }
