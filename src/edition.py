"""The edition: what a book says about itself in both EPUBs.

The ``book-metadata`` skill drafts an ``edition`` block in ``texts.json``::

    "edition": {
      "title": "Bambi", "subtitle": "Una vida en el bosque",
      "author": "Felix Salten", "translator": "Ana Pérez", "publisher": "Editorial Ejemplo",
      "source_title": "Bambi: Eine Lebensgeschichte aus dem Walde", "original_year": 1923,
      "subjects": ["JUVENILE FICTION / Animals / Deer", "Deer -- Fiction"],
      "version": "1.0", "status": "revisada",
      "notice": {"heading": "Créditos", "body": "…the copyright page, paragraphs…"}
    }

Every key is optional: a key left out keeps what ``project.json`` has.
``check`` says what the block may not carry and ``planned`` says what applying
it writes: the ``project.json`` keys ``src.book_metadata`` reads on every build
path, and ``edition_notice.json``. The book's ``description`` (the top-level
key of ``texts.json``) is written to ``project.json`` too, as plain text, so
the site page, ``dc:description`` and the store description start from one text.

These fields used to be written by ``kdp_publish.py apply``. They are here
because a book posted on the site without a Kindle listing needs the same title
page and copyright page; the KDP listing now reads them and writes only
``kdp_listing.json``. The house wording (translator, imprint, rights line,
notice template) still comes from ``prompts/kdp_defaults.json``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from src import epub_readback
from src.kdp.gutenberg import names_trademark

#: ``texts.json`` ``edition`` key -> the ``project.json`` key it is stored under.
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "spanish_title"),
    ("subtitle", "subtitle"),
    ("author", "author"),
    ("translator", "translator"),
    ("publisher", "publisher"),
    ("source_title", "source_title"),
    ("rights", "rights"),
    ("publication_date", "publication_date"),
)
#: A blank one of these is an error: the book cannot go without it.
REQUIRED = ("title", "author", "translator")
KNOWN_KEYS = tuple(k for k, _ in TEXT_FIELDS) + (
    "original_year", "subjects", "version", "status", "title_page", "notice")

DEFAULT_NOTICE_HEADING = "Créditos"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# What a store description may not carry; the KDP description is built from this text.
_STORE_BARS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE), "a URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "an email address"),
    (re.compile(r"(?:US\$|\$|€|£)\s?\d|\b\d+(?:[.,]\d+)?\s?(?:USD|MXN|EUR)\b"), "a price"),
)
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\([^)\n]*\)")


def markdown_plain(text: str) -> str:
    """A Markdown description as plain text, one paragraph per line (``dc:description``)."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block = _MD_LINK_RE.sub(r"\1", block)
        block = re.sub(r"^\s*(?:[-*+]|#{1,6}|>)\s+", "", block, flags=re.MULTILINE)
        block = re.sub(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", block)
        block = " ".join(block.split())
        if block:
            out.append(block)
    return "\n".join(out)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def check(
    texts: dict[str, Any],
    *,
    project: Optional[dict[str, Any]] = None,
    endnotes: Optional[int] = None,
    scripture_notice: str = "",
    today: Optional[date] = None,
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the ``edition`` block and the ``description`` of ``texts``.

    ``project`` is ``project.json`` as it stands, so a field the block leaves
    out is judged by what is already there. ``scripture_notice`` is the notice
    of the Bible version the book quotes (``quotations.json``), which the
    copyright page must then carry word for word.
    """
    errors: list[str] = []
    warnings: list[str] = []
    project = project or {}
    today = today or date.today()

    description = texts.get("description")
    if isinstance(description, str) and description.strip():
        plain = markdown_plain(description)
        if names_trademark(plain):
            errors.append("description: names Project Gutenberg, whose trademark license bars "
                          "its name on a commercial derivative")
        for pattern, what in _STORE_BARS:
            match = pattern.search(plain)
            if match:
                warnings.append(f"description: contains {what} ({match.group(0)!r}); the store "
                                "description is built from this text and may not carry one")

    if "edition" not in texts:
        return errors, warnings
    block = texts["edition"]
    if not isinstance(block, dict):
        return errors + ["edition: must be an object"], warnings

    for key in block:
        if key not in KNOWN_KEYS:
            warnings.append(f"edition.{key}: not a known key, ignored (known: {', '.join(KNOWN_KEYS)})")

    for key, stored in TEXT_FIELDS:
        if key not in block:
            continue
        value = block[key]
        if value is not None and not isinstance(value, str):
            errors.append(f"edition.{key}: must be a string")
            continue
        if _blank(value):
            if key in REQUIRED:
                errors.append(f"edition.{key}: blank; leave the key out to keep what project.json has")
            continue
        if key != "publication_date" and names_trademark(value):
            errors.append(f"edition.{key}: names Project Gutenberg")
    for key, stored in TEXT_FIELDS:
        if key in REQUIRED and _blank(block.get(key)) and _blank(project.get(stored)) and not (
                key == "title" and not _blank(project.get("title"))):
            errors.append(f"edition.{key}: missing, and project.json has none")
    when = block.get("publication_date")
    if isinstance(when, str) and when.strip() and not _DATE_RE.match(when.strip()):
        errors.append(f"edition.publication_date: {when!r} is not YYYY-MM-DD")

    if "original_year" in block:
        year = block["original_year"]
        if isinstance(year, bool) or not isinstance(year, int):
            errors.append("edition.original_year: must be a whole number")
        elif not 1000 <= year <= today.year:
            errors.append(f"edition.original_year: {year} is not a plausible year")
    elif not project.get("original_year"):
        warnings.append("edition.original_year: missing, and project.json has none; the copyright "
                        "page then gives no year for the original")

    if "subjects" in block:
        subjects = block["subjects"]
        if not isinstance(subjects, list) or not all(isinstance(s, str) for s in subjects):
            errors.append("edition.subjects: must be a list of headings")

    for key in ("version", "status"):
        if key in block and block[key] is not None and not isinstance(block[key], str):
            errors.append(f"edition.{key}: must be a string, such as \"1.0\"")
    on_disk = project.get("edition") if isinstance(project.get("edition"), dict) else {}
    if _blank(block.get("version")) and _blank(on_disk.get("version")):
        warnings.append("edition.version: missing, and project.json has none; the title page "
                        "then carries no edition line, and a libre build refuses to run")

    if "notice" in block:
        notice = block["notice"]
        if not isinstance(notice, dict):
            errors.append("edition.notice: must be {heading, body}")
        else:
            body = notice.get("body")
            if not isinstance(body, str) or not body.strip():
                errors.append("edition.notice.body: blank; leave notice out to keep the current "
                              "copyright page")
            else:
                if names_trademark(body):
                    errors.append("edition.notice.body: names Project Gutenberg")
                if scripture_notice.strip() and " ".join(scripture_notice.split()) not in " ".join(
                        body.split()):
                    errors.append("edition.notice.body: the book quotes a Bible version whose "
                                  f"notice must be on this page, word for word: «{scripture_notice.strip()}»")
                for found_errors, found_warnings in (
                        epub_readback.page_markup_issues("edition.notice.body", body),
                        epub_readback.note_count_issues(
                            "edition.notice.body", body, endnotes, expect_count=False)):
                    errors.extend(found_errors)
                    warnings.extend(found_warnings)
            heading = notice.get("heading")
            if heading is not None and not isinstance(heading, str):
                errors.append("edition.notice.heading: must be a string")
    return errors, warnings


def planned(
    texts: dict[str, Any],
    project: dict[str, Any],
    defaults: Optional[dict[str, Any]] = None,
    *,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """What applying ``texts`` writes for the edition.

    ``{"project_json": {key: value}, "remove": [key], "edition_notice": {heading,
    body} | None}``. Only keys that are given are planned, with three house
    fall-backs when a book has an ``edition`` block and no value of its own yet:
    the translator and the imprint from the defaults, the rights line from the
    defaults' template, and today as the publication date. A value already in
    ``project.json`` is never replaced by a fall-back.
    """
    defaults = defaults or {}
    today = today or date.today()
    fields: dict[str, Any] = {}
    remove: list[str] = []
    notice = None

    description = texts.get("description")
    if isinstance(description, str) and description.strip():
        fields["description"] = markdown_plain(description)

    block = texts.get("edition")
    if not isinstance(block, dict):
        return {"project_json": fields, "remove": remove, "edition_notice": notice}

    for key, stored in TEXT_FIELDS:
        if key not in block:
            continue
        if _blank(block[key]):
            if key not in REQUIRED:
                remove.append(stored)
        else:
            fields[stored] = str(block[key]).strip()

    def settled(stored: str) -> str:
        return str(fields.get(stored) or project.get(stored) or "").strip()

    for stored in ("translator", "publisher"):
        if not settled(stored) and stored not in remove and str(defaults.get(stored) or "").strip():
            fields[stored] = str(defaults[stored]).strip()
    if not settled("rights") and "rights" not in remove and str(defaults.get("rights") or "").strip():
        try:
            fields["rights"] = str(defaults["rights"]).format(
                year=today.year, translator=settled("translator"))
        except (KeyError, IndexError, ValueError):
            fields["rights"] = str(defaults["rights"])
    if not settled("publication_date") and "publication_date" not in remove:
        fields["publication_date"] = today.isoformat()

    if isinstance(block.get("original_year"), int) and not isinstance(block["original_year"], bool):
        fields["original_year"] = block["original_year"]
    if isinstance(block.get("subjects"), list):
        subjects = [str(s).strip() for s in block["subjects"] if str(s).strip()]
        if subjects:
            fields["subjects"] = subjects
        else:
            remove.append("subjects")

    before = project.get("edition") if isinstance(project.get("edition"), dict) else {}
    after = dict(before)
    for key in ("version", "status"):
        if key in block:
            if _blank(block[key]):
                after.pop(key, None)
            else:
                after[key] = str(block[key]).strip()
    if after != before:
        if after:
            fields["edition"] = after
        else:
            remove.append("edition")

    front = project.get("front_matter") if isinstance(project.get("front_matter"), dict) else {}
    wanted = {**front, "title_page": bool(block.get("title_page", front.get("title_page", True)))}
    if wanted != front:
        fields["front_matter"] = wanted

    if isinstance(block.get("notice"), dict) and str(block["notice"].get("body") or "").strip():
        heading = str(block["notice"].get("heading") or "").strip()
        notice = {
            "heading": heading or str((defaults.get("edition_notice") or {}).get("heading") or "")
                       or DEFAULT_NOTICE_HEADING,
            "body": str(block["notice"]["body"]).strip(),
        }
    return {"project_json": fields, "remove": remove, "edition_notice": notice}


def current(project: dict[str, Any], notice: dict[str, Any]) -> dict[str, Any]:
    """The edition as ``project.json`` and ``edition_notice.json`` hold it, in the block's shape."""
    edition = project.get("edition") if isinstance(project.get("edition"), dict) else {}
    out: dict[str, Any] = {key: project.get(stored) or "" for key, stored in TEXT_FIELDS}
    out["title"] = out["title"] or project.get("title") or ""
    out.update({
        "original_year": project.get("original_year"),
        "subjects": project.get("subjects") or [],
        "version": edition.get("version") or "",
        "status": edition.get("status") or "",
        "notice": {"heading": notice.get("heading") or "", "body": notice.get("body") or ""},
    })
    return out
