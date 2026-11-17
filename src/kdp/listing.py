"""
The approved listing, and what applying it writes.

A listing is the JSON the drafting step writes (``.harness/kdp/listing.json``
by convention): the KDP form's fields in the agent's words. ``apply`` writes
one file:

- ``kdp_listing.json``: the listing as approved plus ``kdp_form``, the form's
  fields in the order KDP's pages ask for them — the copy-paste source. Once
  the book is live, ``mark_published`` adds its ASIN and date.

The book itself is not written here. Its title page, copyright page and
``dc:description`` (the edition) belong to ``scripts/book_metadata.py``, which
runs first and also serves books that go to the site with no Kindle listing.
``book_state`` reads what that wrote, and ``validate.check`` holds the listing
to it: ``apply`` refuses while the two disagree.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src.kdp import LISTING_FILE
from src.kdp.validate import OWN_COPYRIGHT, check, rights_mode_of

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def load(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a listing is a JSON object")
    return data


def book_state(project_dir: Path) -> dict[str, Any]:
    """The edition as ``project.json`` holds it: what the listing has to agree with.

    ``edition_applied`` is false for a book ``book_metadata.py apply`` has not
    reached (no translator on record, or no copyright page). ``author`` is
    ``None`` for a collection, whose volume author may be several names joined.
    ``endnotes`` is the built EPUB's note count, ``None`` without a build.
    """
    from src import collection as collection_mod
    from src import epub_readback
    from src.book_metadata import load_book_metadata, load_site_metadata

    project_dir = Path(project_dir)
    meta = load_book_metadata(project_dir)
    epub = epub_readback.newest_epub(project_dir)
    return {
        "title": meta["title"],
        "subtitle": meta["subtitle"],
        "author": None if collection_mod.is_collection_dir(project_dir) else meta["author"],
        "translator": meta["translator"],
        "description": meta["description"],
        "forms": load_site_metadata(project_dir)["forms"],
        "edition": meta["edition"],
        "edition_applied": bool(meta["translator"] and meta["edition_notice_body"].strip()),
        "endnotes": epub_readback.count_endnotes(epub) if epub else None,
        "epub": str(epub) if epub else None,
    }


def select_answer(listing: dict[str, Any], facts: Optional[dict[str, Any]] = None) -> str:
    """The KDP Select answer for the form, with the reason when the site decides it."""
    if ((facts or {}).get("site") or {}).get("on_site"):
        return ("No. The book is on the site, and KDP Select requires the ebook to be "
                "exclusive to Amazon.")
    return "Yes" if listing.get("kdp_select") else "No"


def kdp_form(
    listing: dict[str, Any], defaults: Optional[dict[str, Any]] = None,
    *, facts: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """The KDP eBook Details / Content / Rights fields, in page order."""
    owned = rights_mode_of(listing, defaults) == OWN_COPYRIGHT
    title = str(listing.get("title") or "").strip()
    descriptor = str(listing.get("descriptor") or "").strip()
    audience = listing.get("primary_audience") or {}
    series = listing.get("series") or {}
    series_value = str(series.get("name") or "")
    if series_value and series.get("number"):
        series_value += f" #{series['number']}"
    reading_age = ""
    if audience.get("reading_age_min") is not None or audience.get("reading_age_max") is not None:
        low, high = audience.get("reading_age_min"), audience.get("reading_age_max")
        reading_age = f"{'' if low is None else low}–{'' if high is None else high}"
    territories = listing.get("territories") or {}
    rows = [
        ("Details", "Language", listing.get("language") or "Spanish"),
        ("Details", "Book Title", f"{title} {descriptor}".strip()),
        ("Details", "Subtitle", listing.get("subtitle") or ""),
        ("Details", "Series", series_value),
        ("Details", "Edition Number", listing.get("edition_number") or ""),
        ("Details", "Author", listing.get("author") or ""),
        ("Details", "Contributors", "; ".join(
            f"{c.get('name')} ({c.get('role')})" for c in listing.get("contributors") or [])),
        ("Details", "Description", listing.get("description_html") or ""),
        ("Details", "Publishing Rights",
         "I own the copyright and I hold the necessary publishing rights" if owned
         else "This is a public domain work"),
        ("Details", "Sexually explicit images or title language",
         "Yes" if audience.get("sexually_explicit") else "No"),
        ("Details", "Reading age", reading_age),
        ("Details", "Primary marketplace", listing.get("marketplace") or "Amazon.com"),
        ("Details", "Categories", [
            " › ".join(c.get("path") or [c.get("name") or ""]) for c in listing.get("categories") or []
        ]),
        ("Details", "Keywords", [
            (k.get("phrase") if isinstance(k, dict) else k) for k in listing.get("keywords") or []
        ]),
        ("Content", "AI-generated content", listing.get("ai_disclosure") or {}),
        ("Pricing", "KDP Select", select_answer(listing, facts)),
        ("Pricing", "Territories",
         territories.get("mode", "") + (
             f" (exclude: {', '.join(territories.get('exclude') or [])})"
             if territories.get("exclude") else "")),
    ]
    return [{"page": page, "field": field, "value": value} for page, field, value in rows]


def _read(path: Path) -> Optional[dict[str, Any]]:
    """The JSON object at ``path``: ``{}`` when the file is missing, None when it
    exists but is not a readable JSON object."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def apply(
    project_dir: Path,
    listing: dict[str, Any],
    defaults: dict[str, Any],
    *,
    facts: Optional[dict[str, Any]] = None,
    book: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Validate, then write ``kdp_listing.json``. Returns the field-level diff.

    ``book`` is :func:`book_state`; left out, it is read here. An ASIN and a
    publication date already on record are kept.
    """
    project_dir = Path(project_dir)
    if book is None:
        book = book_state(project_dir)
    verdict = check(listing, defaults, facts=facts, book=book)
    if not verdict["ok"]:
        return {"status": "refused", "reason": "the listing has errors", "check": verdict}

    listing_path = project_dir / LISTING_FILE
    before = _read(listing_path)
    if before is None:
        return {"status": "refused", "reason": f"{LISTING_FILE} exists but is not a readable "
                "JSON object; fix it by hand first"}

    record = {k: v for k, v in listing.items() if k != "epub"}
    record["publishing_rights"] = verdict["rights_mode"]
    record["kdp_select"] = bool(listing.get("kdp_select"))
    record["approved_at"] = (today or date.today()).isoformat()
    record["kdp_form"] = kdp_form(listing, defaults, facts=facts)
    for key in ("asin", "published_at"):
        if before.get(key):
            record[key] = before[key]

    changes = []
    for key in sorted(set(record) | set(before)):
        if key in ("approved_at", "kdp_form", "epub"):
            continue
        if before.get(key) != record.get(key):
            changes.append({"file": LISTING_FILE, "field": key,
                            "before": before.get(key), "after": record.get(key)})

    written = []
    if not dry_run:
        listing_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(listing_path))

    return {
        "status": "dry_run" if dry_run else "applied",
        "changes": changes,
        "written": written,
        "warnings": verdict["warnings"],
        "kdp_select": select_answer(listing, facts),
    }


def mark_published(
    project_dir: Path, asin: str, *, when: Optional[str] = None, today: Optional[date] = None,
) -> dict[str, Any]:
    """Record a live listing's ASIN and date in ``kdp_listing.json``.

    The next run then knows the book is for sale: it refreshes that listing
    and keeps its title and reading age, where a first run would draft a new one.
    """
    path = Path(project_dir) / LISTING_FILE
    record = _read(path)
    if not record:
        return {"status": "refused", "reason": f"no readable {LISTING_FILE}: apply a listing first"}
    asin = asin.strip().upper()
    if not _ASIN_RE.match(asin):
        return {"status": "refused", "reason": f"{asin!r} is not an ASIN (B0 and eight more "
                "letters or digits)"}
    when = when or (today or date.today()).isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", when):
        return {"status": "refused", "reason": f"{when!r} is not YYYY-MM-DD"}
    before = {"asin": record.get("asin"), "published_at": record.get("published_at")}
    record["asin"], record["published_at"] = asin, when
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok", "listing": str(path), "before": before,
            "after": {"asin": asin, "published_at": when}}
