"""
``apply``, ``revert`` and ``verify`` — the only code that writes into ``images/``.

``apply`` is where a generated picture becomes part of a published book, so it
is built around what cannot be undone by accident:

- **the backup comes first and is write-once.** The first replacement of a file
  copies it to ``images_original/<file>``; no later ``apply`` overwrites that
  copy, so replacing an image a second time cannot turn a generated picture
  into the "original". (``backfill`` is the one thing that does write there:
  it swaps the publisher's small file for the publisher's larger scan.)
- **the filename and format never change.** The candidate is re-encoded to the
  original's extension, so every ``[IMAGE:…]`` token keeps resolving and no text
  artefact is touched.
- **the swap is atomic.** The new file is written beside the target and renamed
  over it, so the reader never serves half an image.

``revert`` copies the backup back, byte for byte. ``verify`` audits the whole
arrangement: every token resolves, every replaced file still has its original,
and nothing has drifted from what the ledger says was written.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

from src.image_pass import (
    IMAGE_SUFFIXES,
    MODE_COVER,
    image_key,
    images_dir,
    is_safe_key,
    originals_dir,
)
from src.image_pass import ledger
from src.image_pass.inventory import probe_image, referenced_images
from src.image_pass.jobs import candidate_path, existing_candidates, load_manifest

VERDICT_ACCEPT = "accept"
VERDICT_SKIP = "skip"
VERDICT_REDO = "redo"

# How far a replacement's width/height ratio may sit from the original's before
# it is called out. Generated images come in a few fixed proportions, so a small
# drift is normal; past this the picture occupies a visibly different box.
ASPECT_TOLERANCE = 0.05

# A candidate is downscaled to this many times the original's longest side
# (never below the floor): generated images are ~1.5k px, the originals in a
# Gutenberg book ~300 px, and 87 full-size replacements would bloat the EPUB
# tenfold for detail no reader's screen shows.
_SCALE_OVER_ORIGINAL = 2
_MIN_LONG_SIDE = 1024
_COVER_LONG_SIDE = 2560

_JPEG_QUALITY = 90

FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
}


def aspect_drift(width: int, height: int, ref_width: int, ref_height: int) -> float:
    """Relative difference between two width/height ratios (0 = identical)."""
    if not (width and height and ref_width and ref_height):
        return 0.0
    ratio, ref = width / height, ref_width / ref_height
    return abs(ratio - ref) / ref


def _long_side_cap(mode: Optional[str], original: Optional[dict[str, Any]]) -> int:
    if mode == MODE_COVER:
        return _COVER_LONG_SIDE
    if original and original.get("width"):
        return max(
            _MIN_LONG_SIDE,
            _SCALE_OVER_ORIGINAL * max(original["width"], original["height"]),
        )
    return _MIN_LONG_SIDE


def convert_candidate(
    candidate: Path, target: Path, *, max_side: int
) -> dict[str, Any]:
    """Encode ``candidate`` in ``target``'s format, atomically. Returns its size."""
    from PIL import Image

    fmt = FORMATS[target.suffix.lower()]
    tmp = target.with_name(f".{target.name}.image-pass.tmp")
    with Image.open(candidate) as image:
        image.load()
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if fmt == "JPEG":
            # JPEG has no alpha: flatten onto white, the colour of the page.
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.save(tmp, format=fmt, quality=_JPEG_QUALITY, optimize=True)
        elif fmt == "GIF":
            image.convert("P", palette=Image.Palette.ADAPTIVE).save(tmp, format=fmt)
        else:
            image.save(tmp, format=fmt)
        width, height = image.size
    os.replace(tmp, target)
    return {"width": width, "height": height}


def copy_atomic(source: Path, target: Path) -> None:
    tmp = target.with_name(f".{target.name}.image-pass.tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)


def _split_decisions(decisions: list[Any]) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()
    for position, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            invalid.append({"index": position, "problems": ["decision is not an object"]})
            continue
        key = image_key(raw.get("image") or "")
        problems: list[str] = []
        if not is_safe_key(key):
            problems.append(f"image {raw.get('image')!r} is not a path inside images/")
        verdict = raw.get("verdict")
        candidate = raw.get("candidate")
        if verdict is None:
            verdict = VERDICT_ACCEPT if candidate is not None else None
        if verdict not in (VERDICT_ACCEPT, VERDICT_SKIP, VERDICT_REDO):
            problems.append(
                "give `candidate: N` to accept one, or verdict "
                f"{VERDICT_SKIP!r} / {VERDICT_REDO!r} with a note"
            )
        if verdict == VERDICT_ACCEPT and (
            isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1
        ):
            problems.append("an accept needs `candidate`: the 1-based number from the review page")
        if key in seen:
            problems.append(f"{key}: decided twice in this file")
        if problems:
            invalid.append({"index": position, "image": raw.get("image"), "problems": problems})
            continue
        seen.add(key)
        rows.append({
            "image": key,
            "verdict": verdict,
            "candidate": candidate if verdict == VERDICT_ACCEPT else None,
            "note": raw.get("note") if isinstance(raw.get("note"), str) else None,
        })
    return rows, invalid


def apply(
    project_dir: Path,
    decisions: list[Any],
    *,
    dry_run: bool = False,
    max_side: Optional[int] = None,
) -> dict[str, Any]:
    """Swap each accepted candidate in for its original, behind a backup.

    Per-image, not per-batch: one refused image lands the rest and is reported
    by name. ``dry_run`` validates and reports what would happen and writes
    nothing — not the image, not the backup, not the ledger.
    """
    project_dir = Path(project_dir)
    rows, invalid = _split_decisions(decisions)
    jobs = {job["image"]: job for job in load_manifest(project_dir)["jobs"]}

    applied: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    noted: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ledger_file: Optional[Path] = None

    for row in rows:
        key = row["image"]
        job = jobs.get(key)

        if row["verdict"] != VERDICT_ACCEPT:
            noted.append({"image": key, "verdict": row["verdict"], "note": row["note"]})
            if not dry_run:
                ledger_file = ledger.append_row(project_dir, {
                    "ts": ledger.now_stamp(),
                    "action": row["verdict"],
                    "image": key,
                    "note": row["note"],
                    "mode": job.get("mode") if job else None,
                    "instruction": job.get("instruction") if job else None,
                })
            continue

        problems: list[str] = []
        target = images_dir(project_dir) / key
        backup = originals_dir(project_dir) / key
        if target.suffix.lower() not in FORMATS:
            problems.append(f"cannot write {target.suffix or 'an extensionless file'}")
        candidate: Optional[Path] = None
        if job is None:
            problems.append("no prepared job for this image")
        else:
            candidate = candidate_path(Path(job["job_dir"]), row["candidate"])
            if not candidate.is_file():
                have = existing_candidates(Path(job["job_dir"]))
                problems.append(
                    f"candidate {row['candidate']} does not exist (have: {have or 'none'})"
                )
        cand_info = probe_image(candidate) if candidate and candidate.is_file() else {}
        if cand_info.get("error"):
            problems.append(f"candidate: {cand_info['error']}")
        if problems:
            refused.append({"image": key, "candidate": row["candidate"], "problems": problems})
            continue
        assert job is not None and candidate is not None

        existed = target.is_file()
        # The reference for proportions is the publisher's file, which is the
        # backup once one exists — not whatever replacement currently sits there.
        reference = backup if backup.is_file() else target if existed else None
        ref_info = probe_image(reference) if reference else {}
        cap = max_side or _long_side_cap(job.get("mode"), ref_info)
        drift = (
            aspect_drift(cand_info["width"], cand_info["height"],
                         ref_info["width"], ref_info["height"])
            if ref_info.get("width")
            else 0.0
        )
        summary = {
            "image": key,
            "candidate": row["candidate"],
            "mode": job.get("mode"),
            "target": str(target),
            "backup": str(backup) if existed else None,
            "backup_action": (
                "none (new file)" if not existed
                else "kept (already backed up)" if backup.is_file()
                else "created"
            ),
            "original_size": (
                [ref_info["width"], ref_info["height"]] if ref_info.get("width") else None
            ),
            "candidate_size": [cand_info["width"], cand_info["height"]],
            "max_side": cap,
        }
        if drift > ASPECT_TOLERANCE:
            warning = {
                "image": key,
                "code": "aspect_changed",
                "detail": (
                    f"candidate is {cand_info['width']}x{cand_info['height']}, the "
                    f"original {ref_info['width']}x{ref_info['height']} "
                    f"({drift:.0%} off): it will occupy a different box on the page"
                ),
            }
            warnings.append(warning)
            summary["warning"] = warning["code"]

        if dry_run:
            planned.append(summary)
            continue

        sha_before = ledger.sha256_file(target) if existed else None
        try:
            if existed and not backup.is_file():
                backup.parent.mkdir(parents=True, exist_ok=True)
                copy_atomic(target, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = convert_candidate(candidate, target, max_side=cap)
        except Exception as exc:  # noqa: BLE001 - report it against this image
            refused.append({
                "image": key,
                "candidate": row["candidate"],
                "problems": [f"could not write: {exc}"],
            })
            continue
        summary["written_size"] = [written["width"], written["height"]]
        summary["bytes"] = target.stat().st_size
        applied.append(summary)
        ledger_file = ledger.append_row(project_dir, {
            "ts": ledger.now_stamp(),
            "action": ledger.ACTION_APPLY,
            "image": key,
            "candidate": row["candidate"],
            "note": row["note"],
            "job_id": job["id"],
            "mode": job.get("mode"),
            "instruction": job.get("instruction"),
            "labels": job.get("labels") or {},
            "prompt_sha": job.get("prompt_sha"),
            "created": not existed,
            "backup": f"images_original/{key}" if existed else None,
            "sha256_before": sha_before,
            "sha256_original": ledger.sha256_file(backup) if backup.is_file() else None,
            "sha256_after": ledger.sha256_file(target),
            "sha256_candidate": ledger.sha256_file(candidate),
            "size_before": summary["original_size"],
            "size_after": summary["written_size"],
        })

    counts = {
        "requested": len(decisions),
        "applied": len(applied),
        "planned": len(planned),
        "refused": len(refused),
        "invalid": len(invalid),
        "skipped": sum(1 for row in noted if row["verdict"] == VERDICT_SKIP),
        "redo": sum(1 for row in noted if row["verdict"] == VERDICT_REDO),
        "warnings": len(warnings),
    }
    clean = not refused and not invalid
    return {
        "status": "ok" if clean else "partial" if (applied or planned or noted) else "error",
        "dry_run": dry_run,
        "applied": applied,
        "planned": planned,
        "refused": refused,
        "invalid": invalid,
        "noted": noted,
        "warnings": warnings,
        "counts": counts,
        "ledger_path": str(ledger_file) if ledger_file else None,
        "originals_dir": str(originals_dir(project_dir)),
        "instructions": (
            "Nothing was written. Relay planned and warnings, then re-run without "
            "--dry-run."
            if dry_run
            else "Run `verify`, then hand back to translate-harness for `epub`. "
            "Every `redo` row needs a new instruction and a re-run of prepare."
        ),
    }


def revert(project_dir: Path, images: list[str]) -> dict[str, Any]:
    """Put the original back for each named image (``all`` = every replaced one).

    The backup stays where it is: it is the original whether or not it is also
    what ``images/`` currently holds, and a later ``apply`` must find it.
    """
    project_dir = Path(project_dir)
    state = ledger.current_state(project_dir)
    if any(str(name).strip().lower() == "all" for name in images):
        keys = sorted(
            key for key, row in state.items()
            if ledger.status_of(row) == ledger.STATUS_REPLACED
        )
        if not keys:
            return {
                "status": "ok",
                "reverted": [],
                "refused": [],
                "counts": {"requested": 0, "reverted": 0, "refused": 0},
                "instructions": "Nothing is currently replaced.",
            }
    else:
        keys = [image_key(name) for name in images if str(name).strip()]

    reverted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    ledger_file: Optional[Path] = None
    for key in keys:
        if not is_safe_key(key):
            refused.append({"image": key, "problems": ["not a path inside images/"]})
            continue
        target = images_dir(project_dir) / key
        backup = originals_dir(project_dir) / key
        last = state.get(key)
        created = bool(last and last.get("action") == ledger.ACTION_APPLY and last.get("created"))

        if backup.is_file():
            sha_before = ledger.sha256_file(target) if target.is_file() else None
            sha_original = ledger.sha256_file(backup)
            if sha_before == sha_original:
                action = "already original"
                if ledger.status_of(last) != ledger.STATUS_REPLACED:
                    reverted.append({"image": key, "action": action})
                    continue
                # Put back by hand: nothing to copy, but the ledger still says
                # replaced and has to be told.
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_atomic(backup, target)
                action = "restored"
        elif created and target.is_file():
            # We made this file from nothing (a new cover); "the original" is
            # its absence. Only remove what we provably wrote.
            sha_before = ledger.sha256_file(target)
            if sha_before != last.get("sha256_after"):
                refused.append({
                    "image": key,
                    "problems": [
                        "this file was created by image-pass but has changed "
                        "since; it has no original to restore and will not be "
                        "deleted on a guess"
                    ],
                })
                continue
            target.unlink()
            sha_original = None
            action = "removed (had no original)"
        else:
            refused.append({
                "image": key,
                "problems": ["no backup in images_original/ — nothing to restore from"],
            })
            continue
        reverted.append({"image": key, "action": action})
        ledger_file = ledger.append_row(project_dir, {
            "ts": ledger.now_stamp(),
            "action": ledger.ACTION_REVERT,
            "image": key,
            "result": action,
            "sha256_before": sha_before,
            "sha256_after": sha_original,
        })

    return {
        "status": "ok" if not refused else "partial" if reverted else "error",
        "reverted": reverted,
        "refused": refused,
        "counts": {
            "requested": len(keys),
            "reverted": len(reverted),
            "refused": len(refused),
        },
        "ledger_path": str(ledger_file) if ledger_file else None,
        "instructions": "Rebuild the EPUB if one was built from the replaced images.",
    }


def verify(project_dir: Path) -> dict[str, Any]:
    """Audit the images against the tokens, the backups and the ledger.

    ``broken`` is something a reader would see or a revert could not undo;
    ``warned`` is drift worth a look that publishes fine.
    """
    project_dir = Path(project_dir)
    root = images_dir(project_dir)
    backups = originals_dir(project_dir)
    state = ledger.current_state(project_dir)
    originals = ledger.expected_originals(project_dir)
    broken: list[dict[str, Any]] = []
    warned: list[dict[str, Any]] = []
    audited = 0

    def flag(bucket: list, key: str, code: str, detail: str) -> None:
        bucket.append({"image": key, "code": code, "detail": detail})

    seen: set[str] = set()
    for ref in referenced_images(project_dir):
        key = ref["image"]
        seen.add(key)
        audited += 1
        path = root / key
        if not path.is_file():
            flag(broken, key, "missing_file",
                 f"[IMAGE:{ref['token']}] names a file that is not in images/")
            continue
        info = probe_image(path)
        if info.get("error"):
            flag(broken, key, "unreadable", info["error"])

    for key, row in sorted(state.items()):
        status = ledger.status_of(row)
        path = root / key
        backup = backups / key
        if key not in seen:
            audited += 1
        if status == ledger.STATUS_REPLACED:
            if not path.is_file():
                flag(broken, key, "missing_file", "the ledger says replaced, but the file is gone")
                continue
            if not row.get("created") and not backup.is_file():
                flag(broken, key, "no_backup",
                     "replaced, and images_original/ no longer holds the original: "
                     "revert cannot restore it")
            if ledger.sha256_file(path) != row.get("sha256_after"):
                flag(warned, key, "changed_since_apply",
                     "the file is not the one apply wrote — edited or replaced by hand")
            if backup.is_file():
                # A backfill since the apply replaced the backup on purpose.
                recorded = originals.get(key) or row.get("sha256_original")
                if recorded and ledger.sha256_file(backup) != recorded:
                    flag(broken, key, "backup_changed",
                         "images_original/ no longer matches the original the ledger recorded")
                now, ref = probe_image(path), probe_image(backup)
                if now.get("width") and ref.get("width"):
                    drift = aspect_drift(now["width"], now["height"], ref["width"], ref["height"])
                    if drift > ASPECT_TOLERANCE:
                        flag(warned, key, "aspect_changed",
                             f"{now['width']}x{now['height']} against the original "
                             f"{ref['width']}x{ref['height']} ({drift:.0%} off)")
        elif status == ledger.STATUS_REVERTED and backup.is_file() and path.is_file():
            if ledger.sha256_file(path) != ledger.sha256_file(backup):
                flag(warned, key, "reverted_but_differs",
                     "the ledger says reverted, but the file is not the original")

    if backups.is_dir():
        for path in sorted(backups.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                key = path.relative_to(backups).as_posix()
                if key not in state:
                    flag(warned, key, "backup_without_ledger",
                         "images_original/ holds this file but the ledger has no row for it")

    by_code: dict[str, int] = {}
    for item in broken + warned:
        by_code[item["code"]] = by_code.get(item["code"], 0) + 1
    replaced = sum(1 for row in state.values() if ledger.status_of(row) == ledger.STATUS_REPLACED)
    return {
        "status": "ok" if not broken else "broken",
        "counts": {
            "audited": audited,
            "referenced": len(seen),
            "replaced": replaced,
            "broken": len(broken),
            "warned": len(warned),
        },
        "by_code": by_code,
        "broken": broken,
        "warned": warned,
        "instructions": (
            "missing_file / unreadable: the reader and EPUB show nothing there. "
            "no_backup / backup_changed: the original is not recoverable from "
            "this project — re-ingest the source to get it back. aspect_changed: "
            "look at the page before publishing."
        ),
    }
