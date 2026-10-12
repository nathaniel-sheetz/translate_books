"""
Every cover ``cover-compose`` has drawn for a book: ``<project>/cover/drafts/``
and the ``drafts.json`` beside it.

``cover/proof.png`` is one file, written over by each compose, and the swatch
sheet is another. That is right for the next step, which wants the latest, and
wrong for choosing: the draft before this one, and the band in the third colour,
are gone the moment the next is made. So each cover drawn is also kept here as a
**draft** — a preview small enough to keep twenty of, and the settings it was
drawn with, which is what makes it possible to go back to it.

There are three kinds, and they differ only in why the cover was drawn:

- ``proof`` — the book's cover as its spec stands. One per compose.
- ``swatch`` — the same cover in one of the band colours taken from the art.
- ``variant`` — the same cover with one thing changed, to compare (``--try``).

Swatches and variants come in **sets**: the covers of one invocation, to be
looked at side by side.

A draft is never rewritten, and the same cover is not kept twice: what a draft
*is* is the settings, the art and the design it was drawn from, and a second
compose of all three adds nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from src.image_pass import cover_dir, ledger

KIND_PROOF = "proof"
KIND_SWATCH = "swatch"
KIND_VARIANT = "variant"
KINDS = (KIND_PROOF, KIND_SWATCH, KIND_VARIANT)

# The long side of a kept preview. A proof is a 5 MB PNG; this is a tenth of it
# and still shows whether a title sits right.
PREVIEW_SIDE = 1400
_PREVIEW_QUALITY = 88


def drafts_dir(project_dir: Path) -> Path:
    return cover_dir(project_dir) / "drafts"


def drafts_path(project_dir: Path) -> Path:
    return cover_dir(project_dir) / "drafts.json"


def load(project_dir: Path) -> list[dict[str, Any]]:
    """Every draft on record, oldest first; empty when there is no readable file."""
    try:
        doc = json.loads(drafts_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = doc.get("drafts") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    kept = [row for row in rows if isinstance(row, dict) and isinstance(row.get("n"), int)]
    return sorted(kept, key=lambda row: row["n"])


def find(project_dir: Path, number: Any) -> Optional[dict[str, Any]]:
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return next((row for row in load(project_dir) if row["n"] == number), None)


def preview_path(project_dir: Path, row: dict[str, Any]) -> Path:
    return cover_dir(project_dir) / str(row.get("file") or "")


def drawn_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The part of a spec that says what the cover looks like: all of it but
    ``state``, which is the command's own bookkeeping."""
    return {key: value for key, value in spec.items() if key != "state"}


def identity(spec: dict[str, Any], art_sha: Optional[str], template_sha: Optional[str]) -> str:
    """One hash for "this cover": the settings, the art and the design."""
    text = json.dumps(
        {"spec": drawn_spec(spec), "art": art_sha, "template": template_sha},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """``{"vars": {"band_right": 0.2}}`` as ``{"band_right": 0.2}``: one line per
    setting, named the way ``--set`` names it."""
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not prefix and key in ("vars", "layers"):
            out.update(_flat(item, ""))
        elif isinstance(item, dict):
            out.update(_flat(item, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = item
    return out


def spec_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What differs between two specs, as ``{key, from, to}``; a setting one of
    them does not have is ``None`` on that side."""
    old, new = _flat(drawn_spec(before)), _flat(drawn_spec(after))
    return [
        {"key": key, "from": old.get(key), "to": new.get(key)}
        for key in sorted(set(old) | set(new))
        if old.get(key) != new.get(key)
    ]


def _write(project_dir: Path, rows: list[dict[str, Any]]) -> None:
    path = drafts_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "updated": ledger.now_stamp(), "drafts": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _save_preview(image: Any, path: Path) -> list[int]:
    from PIL import Image

    picture = image if image.mode == "RGB" else image.convert("RGB")
    longest = max(picture.size)
    if longest > PREVIEW_SIDE:
        scale = PREVIEW_SIDE / longest
        picture = picture.resize(
            (max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
            Image.LANCZOS,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path, format="JPEG", quality=_PREVIEW_QUALITY)
    return [picture.width, picture.height]


def record(
    project_dir: Path,
    image: Any,
    *,
    kind: str,
    spec: dict[str, Any],
    art_sha: Optional[str],
    template: Optional[str] = None,
    template_sha: Optional[str] = None,
    base: Optional[dict[str, Any]] = None,
    set_id: Optional[str] = None,
    label: Optional[str] = None,
    color: Optional[str] = None,
    now: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """Keep ``image`` as a draft and return ``(row, created)``.

    ``spec`` is what it was drawn with. ``base`` is the spec it is a change
    from: the book's own for a swatch or a variant, and for a proof the proof
    before it, which is looked up when not given. A cover already on record
    under the same kind comes back as it is, with ``created`` false.
    """
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} is not one of {list(KINDS)}")
    project_dir = Path(project_dir)
    rows = load(project_dir)
    ident = identity(spec, art_sha, template_sha)
    for row in reversed(rows):
        if row.get("kind") == kind and row.get("id") == ident and preview_path(
            project_dir, row
        ).is_file():
            return row, False

    if base is None and kind == KIND_PROOF:
        base = next((row.get("spec") for row in reversed(rows)
                     if row.get("kind") == KIND_PROOF and isinstance(row.get("spec"), dict)), None)
    number = (rows[-1]["n"] + 1) if rows else 1
    name = f"drafts/{number:03d}.jpg"
    size = _save_preview(image, cover_dir(project_dir) / name)
    row = {
        "n": number,
        "kind": kind,
        "set": set_id,
        "ts": now or ledger.now_stamp(),
        "file": name,
        "size": size,
        "id": ident,
        "label": label,
        "color": color,
        # None is "nothing to compare it with": the first proof of a book.
        "changes": spec_changes(base, spec) if base is not None else None,
        "spec": drawn_spec(spec),
        "art_sha": art_sha,
        "template": template,
        "template_sha": template_sha,
    }
    _write(project_dir, rows + [row])
    return row, True
