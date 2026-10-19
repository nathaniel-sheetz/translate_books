"""
Book-level metadata for an EPUB build, resolved the same way on every path.

Before this module each builder resolved metadata on its own: the web UI's
Build EPUB read ``project.json``, ``harness.py epub`` read only
``.harness/config.json`` (the *English* source title), and the
``translate_book.py`` / ``corrections_apply`` rebuilds fell back to the slug
and ``"Unknown"``. Only the web UI passed translator, description, rights or
publisher, so the same book came out with different metadata depending on
which button built it.

``load_book_metadata`` is the one resolver. ``project.json`` is the source of
truth (the web UI's Export tab and ``kdp_publish.py apply`` both write it);
the harness config and the ingest report are fallbacks for the title and the
author only. Every field beyond those is opt-in: a book whose ``project.json``
lacks a key builds without it, exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

PROJECT_FILE = "project.json"
TRANSLATOR_NOTE_FILE = "translator_note.json"
EDITION_NOTICE_FILE = "edition_notice.json"


def _read_json(path: Path) -> dict[str, Any]:
    """A JSON object from ``path``, or ``{}`` when missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _note(path: Path) -> tuple[str, str]:
    """``(heading, body)`` from a ``{heading, body}`` sidecar; blanks when absent."""
    data = _read_json(path)
    return _text(data.get("heading")), str(data.get("body") or "")


def load_book_metadata(project_dir: Path) -> dict[str, Any]:
    """Resolve every metadata field a build needs for ``project_dir``.

    ``title`` is ``project.json`` ``spanish_title`` -> ``title`` -> the harness
    config ``title`` -> the ingest report's title, and may be ``""``: callers
    keep their own last resort (the slug, the CLI's project name). ``author``
    walks the same three files. ``language`` is ``target_lang_code`` -> the
    harness ``language_code`` -> ``"es"``.
    """
    project_dir = Path(project_dir)
    project = _read_json(project_dir / PROJECT_FILE)
    harness = _read_json(project_dir / ".harness" / "config.json")
    ingest = _read_json(project_dir / "ingest_report.json").get("metadata") or {}
    if not isinstance(ingest, dict):
        ingest = {}

    subjects = project.get("subjects") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    front_matter = project.get("front_matter")
    note_heading, note_body = _note(project_dir / TRANSLATOR_NOTE_FILE)
    notice_heading, notice_body = _note(project_dir / EDITION_NOTICE_FILE)

    return {
        "title": _first(project.get("spanish_title"), project.get("title"),
                        harness.get("title"), ingest.get("title")),
        "subtitle": _text(project.get("subtitle")),
        "author": _first(project.get("author"), harness.get("author"), ingest.get("creator")),
        "language": _first(project.get("target_lang_code"), harness.get("language_code")) or "es",
        "translator": _text(project.get("translator")),
        "description": _text(project.get("description")),
        "rights": _text(project.get("rights")),
        "source_title": _text(project.get("source_title")),
        "publisher": _text(project.get("publisher")),
        "subjects": [_text(s) for s in subjects if _text(s)],
        "date": _text(project.get("publication_date")),
        "front_matter": front_matter if isinstance(front_matter, dict) else None,
        "translator_note_heading": note_heading,
        "translator_note_body": note_body,
        "edition_notice_heading": notice_heading,
        "edition_notice_body": notice_body,
    }


def epub_kwargs(project_dir: Path, **overrides: Optional[Any]) -> dict[str, Any]:
    """Keyword arguments for ``build_epub`` / ``build_epub_from_chunks``.

    An override wins when it is not ``None``/blank, so a CLI ``--title`` still
    beats ``project.json``. Blank strings come back as ``None`` so the builder
    omits the element rather than writing an empty one.
    """
    meta = load_book_metadata(project_dir)
    for key, value in overrides.items():
        if value not in (None, ""):
            meta[key] = value
    return {
        key: (None if value in ("", []) else value)
        for key, value in meta.items()
    }
