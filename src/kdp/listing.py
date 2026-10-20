"""
The approved listing, and what applying it writes.

A listing is the JSON the drafting step writes (``.harness/kdp/listing.json``
by convention): the KDP form's fields in the agent's words, plus an ``epub``
block for what the book itself should carry. ``apply`` is the only writer, and
only of three files:

- ``project.json``: the metadata keys ``src.book_metadata`` reads, so the next
  build — from any path — carries them;
- ``edition_notice.json``: the copyright page, ``{heading, body}``;
- ``kdp_listing.json``: the listing as approved plus ``kdp_form``, the form's
  fields in the order KDP's pages ask for them — the copy-paste source.

It refuses while ``validate.check`` reports an error.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src.book_metadata import EDITION_NOTICE_FILE, PROJECT_FILE
from src.kdp import LISTING_FILE
from src.kdp.validate import OWN_COPYRIGHT, check, description_plain, rights_mode_of


def load(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a listing is a JSON object")
    return data


def translator_of(listing: dict[str, Any]) -> str:
    for contributor in listing.get("contributors") or []:
        if str(contributor.get("role") or "").lower() in ("translator", "traductor"):
            return str(contributor.get("name") or "").strip()
    return ""


def _fill(template: str, values: dict[str, Any]) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def kdp_form(
    listing: dict[str, Any], defaults: Optional[dict[str, Any]] = None,
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
        ("Pricing", "Territories",
         territories.get("mode", "") + (
             f" (exclude: {', '.join(territories.get('exclude') or [])})"
             if territories.get("exclude") else "")),
    ]
    return [{"page": page, "field": field, "value": value} for page, field, value in rows]


def planned_writes(
    project_dir: Path, listing: dict[str, Any], defaults: dict[str, Any],
    *, today: Optional[date] = None,
) -> dict[str, Any]:
    """What ``apply`` would write: ``{project_json: {...}, edition_notice: {...}}``."""
    today = today or date.today()
    epub = listing.get("epub") or {}
    translator = translator_of(listing) or str(defaults.get("translator") or "")
    publisher = epub.get("publisher") or defaults.get("publisher") or ""
    rights = epub.get("rights") or _fill(
        str(defaults.get("rights") or ""), {"year": today.year, "translator": translator},
    )
    project_fields = {
        "spanish_title": str(listing.get("title") or "").strip(),
        "subtitle": str(listing.get("subtitle") or "").strip(),
        "author": str(listing.get("author") or "").strip(),
        "translator": translator,
        "publisher": publisher,
        "description": epub.get("description") or description_plain(
            str(listing.get("description_html") or "")),
        "rights": rights,
        "subjects": [str(s).strip() for s in epub.get("subjects") or [] if str(s).strip()],
        "original_year": epub.get("original_year"),
        "publication_date": epub.get("publication_date") or today.isoformat(),
        "front_matter": {"title_page": bool(epub.get("title_page", True))},
    }
    if epub.get("source_title"):
        project_fields["source_title"] = epub["source_title"]
    # Never blank out a value already on disk with an empty one.
    project_fields = {k: v for k, v in project_fields.items() if v not in (None, "", [])}
    notice = epub.get("edition_notice") or {}
    return {
        "project_json": project_fields,
        "edition_notice": {
            "heading": str(notice.get("heading") or "").strip(),
            "body": str(notice.get("body") or "").strip(),
        } if str(notice.get("body") or "").strip() else None,
    }


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
    dry_run: bool = False,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Validate, then write the three files. Returns the field-level diff."""
    project_dir = Path(project_dir)
    verdict = check(listing, defaults, facts=facts)
    if not verdict["ok"]:
        return {"status": "refused", "reason": "the listing has errors", "check": verdict}

    plan = planned_writes(project_dir, listing, defaults, today=today)
    project_path = project_dir / PROJECT_FILE
    project = _read(project_path)
    if project is None:
        # Writing over it would drop every key apply does not own.
        return {"status": "refused", "reason": f"{PROJECT_FILE} exists but is not a readable "
                "JSON object; fix it by hand first"}
    changes = []
    for key, after in plan["project_json"].items():
        before = project.get(key)
        if key == "front_matter" and isinstance(before, dict):
            after = {**before, **after}
        if before != after:
            changes.append({"file": PROJECT_FILE, "field": key, "before": before, "after": after})
            project[key] = after

    notice_path = project_dir / EDITION_NOTICE_FILE
    if plan["edition_notice"] is not None:
        before = _read(notice_path) or {}
        for field in ("heading", "body"):
            if before.get(field) != plan["edition_notice"][field]:
                changes.append({"file": EDITION_NOTICE_FILE, "field": field,
                                "before": before.get(field),
                                "after": plan["edition_notice"][field]})

    record = dict(listing)
    record["publishing_rights"] = verdict["rights_mode"]
    record["approved_at"] = (today or date.today()).isoformat()
    record["kdp_form"] = kdp_form(listing, defaults)

    written = []
    if not dry_run:
        project_path.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(project_path))
        if plan["edition_notice"] is not None:
            notice_path.write_text(
                json.dumps(plan["edition_notice"], indent=2, ensure_ascii=False), encoding="utf-8")
            written.append(str(notice_path))
        listing_path = project_dir / LISTING_FILE
        listing_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(listing_path))

    return {
        "status": "dry_run" if dry_run else "applied",
        "changes": changes,
        "written": written,
        "warnings": verdict["warnings"],
    }
