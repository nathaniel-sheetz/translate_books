"""
``backfill`` — bring in the publisher's larger scans for a book already ingested.

A Gutenberg page displays a thumbnail and links it to the full-size scan.
``scripts/ingest_gutenberg.py`` imports the scan for a new book; a book ingested
before it did has the thumbnails, and its translation is long since built on
their filenames. This puts the larger scan behind each of those names.

It is the same kind of write as ``apply`` — the name and format stay, the swap
is atomic, the ledger gets a row — with one deliberate difference: **the larger
scan becomes the original.** No backup of the thumbnail is made (it is the same
picture, smaller, and the publisher still serves it), and where an earlier
``apply`` left the thumbnail in ``images_original/``, the larger scan replaces
it there, so a redo or a revert starts from the better file.

**The link is a hint, the picture is the test.** Publishers mislink: in
home-geography the star chart's thumbnail links to the compass's scan and the
compass's to the star chart's. So a scan is only taken when it measures as the
same picture as the file it would replace. When the linked one does not, the
page's other scans are tried, and the one that does match is used; when none
does, the image is reported in ``unlike`` and left alone until a person has
looked and named it in ``accept``.

What it will not do:

- touch a replacement. If ``images/<file>`` is a picture ``apply`` put there,
  only its backup is upgraded; the job has to be re-run to redraw it from the
  larger scan.
- swap for something that is not larger. A re-run, or a book ingested with the
  larger scans already, changes nothing.
- join split pictures. Two placeholders that are the halves of one linked scan
  are reported, not collapsed: that rewrites text in four places.

Nothing here touches the network. The caller hands in the link map and a
``fetch`` callable; ``scripts/image_pass.py`` supplies both from the ingest
module, which is where a source page is understood.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from src.image_pass import image_key, images_dir, is_safe_key, originals_dir, work_dir
from src.image_pass import ledger
from src.image_pass.apply import FORMATS, convert_candidate, copy_atomic
from src.image_pass.inventory import (
    SAME_PICTURE,
    picture_similarity,
    probe_image,
    referenced_images,
)
from src.image_pass.jobs import load_manifest


def cache_dir(project_dir: Path) -> Path:
    """Where fetched scans are kept, so a dry run and the run after it fetch once."""
    return work_dir(project_dir) / "backfill"


def _size(info: dict[str, Any]) -> Optional[list[int]]:
    return [info["width"], info["height"]] if info.get("width") else None


class _Scans:
    """The page's larger scans, fetched into the cache on first use."""

    def __init__(self, cache: Path, fetch: Callable[[str], bytes]):
        self.cache = cache
        self.fetch = fetch
        self._seen: dict[str, tuple[Optional[Path], dict[str, Any]]] = {}

    def get(self, link: dict[str, Any]) -> tuple[Optional[Path], dict[str, Any]]:
        """``(path, {width, height, …})``, or ``(None, {error})``."""
        url = link["url"]
        if url not in self._seen:
            self._seen[url] = self._load(link)
        return self._seen[url]

    def _load(self, link: dict[str, Any]) -> tuple[Optional[Path], dict[str, Any]]:
        path = self.cache / link["name"]
        if not path.is_file():
            try:
                self.cache.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_bytes(self.fetch(link["url"]))
                os.replace(tmp, path)
            except Exception as exc:  # noqa: BLE001 - reported against the image
                return None, {"error": f"could not fetch: {exc}"}
        info = probe_image(path)
        if info.get("error"):
            path.unlink(missing_ok=True)
            return None, info
        return path, info


def _write(scan: Path, target: Path, *, max_side: int) -> None:
    """Put ``scan`` at ``target`` in ``target``'s format — as-is when it already is."""
    if FORMATS.get(scan.suffix.lower()) == FORMATS[target.suffix.lower()]:
        copy_atomic(scan, target)
    else:
        convert_candidate(scan, target, max_side=max_side)


def _score(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 3)


def backfill(
    project_dir: Path,
    links: list[dict[str, Any]],
    *,
    fetch: Callable[[str], bytes],
    only: Optional[list[str]] = None,
    accept: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Swap each referenced image for the larger scan its source page links to.

    ``links`` is ``ingest_gutenberg.linked_images`` for the book's source page.
    ``accept`` names images whose linked scan is to be taken although it does
    not measure as the same picture — a re-cropped plate, once someone has
    looked. ``dry_run`` fetches the scans (into the cache, so the sizes it
    reports are real) and writes nothing to ``images/``, ``images_original/``
    or the ledger.
    """
    project_dir = Path(project_dir)
    root, backups = images_dir(project_dir), originals_dir(project_dir)
    scans = _Scans(cache_dir(project_dir), fetch)
    accepted = {image_key(name) for name in accept or []}

    whole: dict[str, dict[str, Any]] = {}
    halves: dict[str, dict[str, Any]] = {}
    for link in links:
        names = link.get("inline_names") or []
        for name in names:
            (whole if len(names) == 1 else halves).setdefault(name, link)

    keys = [row["image"] for row in referenced_images(project_dir)]
    failed: list[dict[str, Any]] = []
    if only:
        wanted = [image_key(name) for name in only]
        failed += [
            {"image": key, "error": "not an image this book references"}
            for key in wanted if key not in keys
        ]
        keys = [key for key in keys if key in wanted]
    with_jobs = {job["image"] for job in load_manifest(project_dir)["jobs"]}

    upgraded: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unlike: list[dict[str, Any]] = []
    split: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ledger_file: Optional[Path] = None

    for key in keys:
        name = Path(key).name
        if name in halves:
            split.append({
                "image": key,
                "url": halves[name]["url"],
                "halves": halves[name]["inline_names"],
            })
            continue
        link = whole.get(name)
        if link is None:
            unmatched.append(key)
            continue

        target, backup = root / key, backups / key
        if not is_safe_key(key) or target.suffix.lower() not in FORMATS:
            failed.append({"image": key, "url": link["url"],
                           "error": f"cannot write {target.suffix or 'an extensionless file'}"})
            continue
        scan, large = scans.get(link)
        if scan is None:
            failed.append({"image": key, "url": link["url"], "error": large["error"]})
            continue

        # The original is the backup once an apply has made one; otherwise it
        # is whatever sits in images/.
        baseline = backup if backup.is_file() else target if target.is_file() else None
        base = probe_image(baseline) if baseline else {}

        # Is the linked scan this picture? With nothing on disk to compare
        # (a missing file, a blank one) the link is all there is.
        score = picture_similarity(baseline, scan) if baseline else None
        relinked_from: Optional[str] = None
        if score is not None and score < SAME_PICTURE and key not in accepted:
            best, best_score = None, SAME_PICTURE
            for other in whole.values():
                if other is link:
                    continue
                other_scan, _info = scans.get(other)
                found = picture_similarity(baseline, other_scan) if other_scan else None
                if found is not None and found >= best_score:
                    best, best_score = other, found
            if best is None:
                unlike.append({
                    "image": key, "url": link["url"], "score": _score(score),
                    "have": _size(base), "linked": _size(large),
                })
                continue
            relinked_from, link, score = link["url"], best, best_score
            scan, large = scans.get(link)
            assert scan is not None

        if base.get("width") and (
            large["width"] * large["height"] <= base["width"] * base["height"]
        ):
            skipped.append({
                "image": key, "reason": "not larger",
                "have": _size(base), "linked": _size(large),
            })
            continue

        shows_original = baseline is not backup or (
            target.is_file() and ledger.sha256_file(target) == ledger.sha256_file(backup)
        )
        writes = [f"images_original/{key}"] if baseline is backup else []
        if shows_original:
            writes.append(f"images/{key}")
        summary: dict[str, Any] = {
            "image": key,
            "url": link["url"],
            "from_size": _size(base),
            "to_size": _size(large),
            "score": _score(score),
            "writes": writes,
        }
        if relinked_from:
            summary["relinked_from"] = relinked_from
        if score is not None and score < SAME_PICTURE:
            summary["accepted"] = True
        if not shows_original:
            summary["left_alone"] = (
                f"images/{key} is a replacement: only its original was upgraded. "
                "Re-run its job to redraw it from the larger scan."
            )
        if key in with_jobs:
            summary["has_job"] = True

        if dry_run:
            planned.append(summary)
            continue

        sha_before = ledger.sha256_file(baseline) if baseline else None
        max_side = max(large["width"], large["height"])
        try:
            if baseline is backup:
                _write(scan, backup, max_side=max_side)
                if shows_original:
                    copy_atomic(backup, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write(scan, target, max_side=max_side)
        except Exception as exc:  # noqa: BLE001 - report it against this image
            failed.append({"image": key, "url": link["url"], "error": f"could not write: {exc}"})
            continue
        new = backup if baseline is backup else target
        summary["bytes"] = new.stat().st_size
        upgraded.append(summary)
        ledger_file = ledger.append_row(project_dir, {
            "ts": ledger.now_stamp(),
            "action": ledger.ACTION_BACKFILL,
            "image": key,
            "source_url": link["url"],
            "relinked_from": relinked_from,
            "score": summary["score"],
            "baseline": ledger.BASELINE_BACKUP if baseline is backup else ledger.BASELINE_IMAGES,
            "wrote": writes,
            "sha256_before": sha_before,
            "sha256_after": ledger.sha256_file(new),
            "sha256_source": ledger.sha256_file(scan),
            "size_before": summary["from_size"],
            "size_after": _size(probe_image(new)),
        })

    done = planned if dry_run else upgraded
    stale_jobs = sorted(row["image"] for row in done if row.get("has_job"))
    counts = {
        "referenced": len(keys),
        "upgraded": len(upgraded),
        "planned": len(planned),
        "relinked": sum(1 for row in done if row.get("relinked_from")),
        "skipped": len(skipped),
        "unlike": len(unlike),
        "split": len(split),
        "unmatched": len(unmatched),
        "failed": len(failed),
        "stale_jobs": len(stale_jobs),
    }
    if not links:
        instructions = "The source page links no larger scans: there is nothing to bring in."
    elif dry_run:
        instructions = (
            "Nothing was written. Relay planned (naming every relinked_from), "
            "unlike, split and failed, then re-run without --dry-run."
        )
    else:
        instructions = (
            "Run `verify`, then rebuild the EPUB. Every image in stale_jobs has a "
            "prepared job whose input was the smaller file: re-run `prepare` for it "
            "(its old candidates are archived) and generate again."
        )
    if unlike:
        instructions += (
            " Each image in unlike links to a scan that does not look like it: "
            "open both, and pass --accept <names> only for the ones that are the "
            "same picture re-cropped."
        )
    return {
        "status": "ok" if not failed else "partial" if done or skipped else "error",
        "dry_run": dry_run,
        "upgraded": upgraded,
        "planned": planned,
        "skipped": skipped,
        "unlike": unlike,
        "split": split,
        "unmatched": unmatched,
        "failed": failed,
        "stale_jobs": stale_jobs,
        "counts": counts,
        "ledger_path": str(ledger_file) if ledger_file else None,
        "cache_dir": str(scans.cache),
        "instructions": instructions,
    }
