"""
The image inventory: every image a book references, plus its cover.

``source.txt`` is the authority for *which* images belong to the book — its
``[IMAGE:images/<file>:<alt>]`` tokens are what the splitter, the translator and
the EPUB builder all carry forward. The files in ``images/`` are only the pixels
behind them. Reading the tokens rather than listing the directory is what
surfaces the two failures nobody goes looking for: a token whose file is missing,
and a file nothing references.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional, Union

from src.image_pass import (
    COVER_NAMES,
    IMAGE_SUFFIXES,
    image_key,
    images_dir,
    originals_dir,
    work_dir,
)
from src.image_pass import ledger
from src.utils.text_utils import image_placeholders


def probe_image(path: Path) -> dict[str, Any]:
    """``{width, height, format}`` for an image file, or ``{error}``.

    Pillow is imported here rather than at module top so every command that
    does not need pixels (``prepare`` of a cover from nothing, the ledger) still
    runs on a checkout that has not installed it yet.
    """
    try:
        from PIL import Image
    except ImportError:
        return {"error": "Pillow is not installed — pip install -r requirements.txt"}
    try:
        with Image.open(path) as image:
            width, height = image.size
            return {"width": width, "height": height, "format": image.format}
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one answer
        return {"error": f"unreadable image: {exc}"}


# Two files of the same picture score at or above this; in the one book measured
# (home-geography, 80 thumbnail/scan pairs) every true pair but two re-cropped
# ones reached 0.99 and no two different pictures passed 0.82.
SAME_PICTURE = 0.95

_SIGNATURE_SIDE = 16


def _signature(source: Union[Path, bytes]) -> Optional[list[int]]:
    """A picture boiled down to a 16x16 grid of grey levels, or None if unreadable."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            small = image.convert("L").resize(
                (_SIGNATURE_SIDE, _SIGNATURE_SIDE), Image.Resampling.BOX
            )
            return list(small.tobytes())
    except Exception:  # noqa: BLE001 - no Pillow, or not an image: cannot compare
        return None


def picture_similarity(a: Union[Path, bytes], b: Union[Path, bytes]) -> Optional[float]:
    """How alike two image files look, whatever their size or format.

    1.0 is the same picture; two unrelated pictures land well below
    :data:`SAME_PICTURE`. ``None`` means it cannot be told — a file that will
    not open, or a blank one, which correlates with nothing.

    A publisher's "larger version" link is usually right and sometimes points
    at a different plate altogether; this is how a caller tells before it puts
    one picture under another's caption.
    """
    first, second = _signature(a), _signature(b)
    if first is None or second is None:
        return None
    mean_a, mean_b = sum(first) / len(first), sum(second) / len(second)
    spread_a = sum((x - mean_a) ** 2 for x in first) ** 0.5
    spread_b = sum((y - mean_b) ** 2 for y in second) ** 0.5
    if not spread_a or not spread_b:
        return None
    return sum((x - mean_a) * (y - mean_b) for x, y in zip(first, second)) / (spread_a * spread_b)


def _chapter_index(project_dir: Path) -> dict[str, str]:
    """``{image key: first chapter id that carries its token}``."""
    index: dict[str, str] = {}
    chapters = Path(project_dir) / "chapters"
    if not chapters.is_dir():
        return index
    for path in sorted(chapters.glob("chapter_*.txt")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for filename, _alt in image_placeholders(text):
            index.setdefault(image_key(filename), path.stem)
    return index


def referenced_images(project_dir: Path) -> list[dict[str, Any]]:
    """One row per distinct image token in ``source.txt``, in book order."""
    source = Path(project_dir) / "source.txt"
    if not source.exists():
        return []
    rows: dict[str, dict[str, Any]] = {}
    for filename, alt in image_placeholders(source.read_text(encoding="utf-8")):
        key = image_key(filename)
        row = rows.get(key)
        if row is None:
            rows[key] = {"image": key, "token": filename, "alt": alt, "references": 1}
        else:
            row["references"] += 1
            if not row["alt"] and alt:
                row["alt"] = alt
    return list(rows.values())


def find_cover(project_dir: Path) -> Optional[str]:
    """The cover the EPUB builder would auto-detect, as an image key."""
    for name in COVER_NAMES:
        if (images_dir(project_dir) / name).exists():
            return name
    return None


def _describe(project_dir: Path, key: str, state: dict[str, dict]) -> dict[str, Any]:
    path = images_dir(project_dir) / key
    out: dict[str, Any] = {"missing": not path.is_file()}
    if not out["missing"]:
        out["bytes"] = path.stat().st_size
        out.update(probe_image(path))
    out["status"] = ledger.status_of(state.get(key))
    out["has_backup"] = (originals_dir(project_dir) / key).is_file()
    return out


def build_rows(project_dir: Path) -> tuple[list[dict[str, Any]], list[str], Optional[str]]:
    """``(rows, unreferenced, cover)`` as they stand on disk now. Writes nothing.

    The image board calls this on every page load, which is why it is apart
    from :func:`inventory`: a page that rewrote ``inventory.json`` each time it
    was looked at would change the file under an agent that is reading it.
    """
    project_dir = Path(project_dir)
    state = ledger.current_state(project_dir)
    chapters = _chapter_index(project_dir)

    rows: list[dict[str, Any]] = []
    for ref in referenced_images(project_dir):
        key = ref["image"]
        rows.append({
            **ref,
            "role": "illustration",
            "chapter": chapters.get(key),
            **_describe(project_dir, key, state),
        })

    cover = find_cover(project_dir)
    referenced = {row["image"] for row in rows}
    cover_key = cover or COVER_NAMES[0]
    if cover_key not in referenced:
        rows.append({
            "image": cover_key,
            "token": None,
            "alt": None,
            "references": 0,
            "role": "cover",
            "chapter": None,
            **_describe(project_dir, cover_key, state),
        })
    else:
        for row in rows:
            if row["image"] == cover_key:
                row["role"] = "cover"

    known = {row["image"] for row in rows}
    unreferenced: list[str] = []
    root = images_dir(project_dir)
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                key = path.relative_to(root).as_posix()
                if key not in known:
                    unreferenced.append(key)
    return rows, unreferenced, cover


def inventory(project_dir: Path) -> dict[str, Any]:
    """Build the inventory and write it to ``.harness/images/inventory.json``.

    Stdout carries counts and the path; the rows stay on disk, because an
    87-image book is 87 rows nobody needs in their context twice.
    """
    project_dir = Path(project_dir)
    rows, unreferenced, cover = build_rows(project_dir)
    root = images_dir(project_dir)
    referenced = {row["image"] for row in rows if row["references"]}

    missing = [row["image"] for row in rows if row["missing"] and row["role"] != "cover"]
    unreadable = [row["image"] for row in rows if row.get("error")]
    counts = {
        "images": len(rows),
        "referenced": len(referenced),
        "missing": len(missing),
        "unreadable": len(unreadable),
        "unreferenced": len(unreferenced),
        "replaced": sum(1 for row in rows if row["status"] == ledger.STATUS_REPLACED),
        "has_cover": cover is not None,
    }

    out_path = work_dir(project_dir) / "inventory.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"project": project_dir.name, "images": rows, "unreferenced": unreferenced},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    notes: list[str] = []
    if not (project_dir / "source.txt").exists():
        notes.append("no source.txt — only the cover and files on disk are listed")
    if missing:
        notes.append(
            f"{len(missing)} token(s) name a file that is not in images/: the "
            "reader and the EPUB show nothing there today"
        )
    if cover is None:
        notes.append(
            "no cover on disk — a `cover` job targeting cover.jpg creates one, and "
            "the EPUB builder picks it up automatically"
        )

    return {
        "status": "ok",
        "project": project_dir.name,
        "counts": counts,
        "missing": missing,
        "unreadable": unreadable,
        "unreferenced": unreferenced,
        "cover": cover,
        "inventory_path": str(out_path),
        "images_dir": str(root),
        "instructions": (
            "Read inventory_path for the per-image rows, then Read the image "
            "files themselves (absolute paths under images_dir) before proposing "
            "any job."
        ),
        "notes": notes,
    }
