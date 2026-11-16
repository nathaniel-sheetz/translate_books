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
truth (the web UI's Export tab and ``book_metadata.py apply`` both write it);
the harness config and the ingest report are fallbacks for the title and the
author (and the harness config for the language). Every field beyond those is opt-in: a book whose ``project.json``
lacks a key builds without it, exactly as before.

``publish`` carries the house settings the free-library (``libre``) build needs
-- the license, the site links, the edition-label format -- from
``prompts/publish_defaults.json`` (gitignored; the tracked ``.example`` twin is
the fallback), with a book's own ``project.json`` ``license`` over the house one.

``load_site_metadata`` reads the fields only the mislibrosvivos.com book page
uses -- ``forms``, ``adaptations``, ``parent_notes`` -- and is kept apart from
``load_book_metadata`` because they never go into either EPUB.
``export_site_texts`` copies the two texts into the site repo's book folder.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

PROJECT_FILE = "project.json"
TRANSLATOR_NOTE_FILE = "translator_note.json"
EDITION_NOTICE_FILE = "edition_notice.json"
ABOUT_BOOK_FILE = "about_book.json"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PUBLISH_DEFAULTS_FILE = _PROMPTS_DIR / "publish_defaults.json"
PUBLISH_DEFAULTS_EXAMPLE_FILE = _PROMPTS_DIR / "publish_defaults.example.json"

_DEFAULT_EDITION_LABEL = "Versión {version} ({status})"

#: The recommended forms a book page can name, in order: ``(code, label)``.
#: ``project.json`` ``forms`` stores the codes.
FORMS: tuple[tuple[str, str], ...] = (
    ("I", "Form I (years 1–3)"),
    ("II", "Form II (years 4–6)"),
    ("III/IV", "Form III/IV (years 7–9)"),
    ("V/VI", "Form V/VI (years 10–12)"),
)
_FORM_CODES = tuple(code for code, _ in FORMS)

logger = logging.getLogger(__name__)


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


def slugify(text: str) -> str:
    """``"Sermones cortos para niños"`` -> ``"sermones-cortos-para-ninos"``."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _site_slug(raw: str) -> str:
    """``project.json`` ``site_slug`` as one safe path component.

    It names ``libros/<slug>/`` in the site repo and ``libre/<slug>.epub``, so
    a hand-edited ``"../x"`` or ``"Mi Libro"`` is slugified rather than used.
    """
    if not raw or _SLUG_RE.fullmatch(raw):
        return raw
    safe = slugify(raw)
    logger.warning("project.json site_slug %r is not a slug; using %r", raw, safe)
    return safe


def load_publish_defaults() -> dict[str, Any]:
    """The house publish settings: the user's file, else the tracked example, else ``{}``."""
    for path in (PUBLISH_DEFAULTS_FILE, PUBLISH_DEFAULTS_EXAMPLE_FILE):
        if path.exists():
            return _read_json(path)
    return {}


def _publish_settings(project: dict[str, Any]) -> dict[str, Any]:
    """``{license, site, edition_label}`` for one book.

    A book's ``license`` key replaces the house license field by field; set to
    ``null`` or ``{}`` it opts the book out. ``license`` is ``None`` when there
    is no license name to state.
    """
    house = load_publish_defaults()
    license_ = house.get("license") if isinstance(house.get("license"), dict) else {}
    if "license" in project:
        own = project.get("license")
        license_ = {**license_, **own} if isinstance(own, dict) and own else {}
    if not _text(license_.get("name")):
        license_ = None
    site = house.get("site") if isinstance(house.get("site"), dict) else {}
    return {
        "license": license_,
        "site": {"url": _text(site.get("url")), "feedback_url": _text(site.get("feedback_url"))},
        "edition_label": _text(house.get("edition_label")) or _DEFAULT_EDITION_LABEL,
    }


def _edition(value: Any) -> Optional[dict[str, str]]:
    """``{"version", "status"}`` from ``project.json`` ``edition``; ``None`` without a version."""
    if not isinstance(value, dict) or not _text(value.get("version")):
        return None
    return {"version": _text(value.get("version")), "status": _text(value.get("status"))}


def _joined(names: list[str]) -> str:
    """One name -> itself; several -> "A, B y C" (the volume is Spanish)."""
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} y {names[-1]}"


def collection_defaults(project_dir: Path) -> dict[str, str]:
    """For a collection dir: author, translator and language from its members.

    A blank field on the volume falls back to the members' values: one
    distinct author names the volume's author, several are joined. ``{}`` for
    a book, so a book's metadata is resolved exactly as before.
    """
    from src import collection as collection_mod

    if not collection_mod.is_collection_dir(project_dir):
        return {}
    try:
        coll = collection_mod.load_collection(project_dir)
    except ValueError:  # corrupt collection.json: resolve like a book
        return {}
    members = [load_book_metadata(m.path) for m in coll.members if m.path is not None]

    def distinct(key: str) -> list[str]:
        seen: list[str] = []
        for meta in members:
            if meta[key] and meta[key] not in seen:
                seen.append(meta[key])
        return seen

    languages = distinct("language")
    return {
        "author": _joined(distinct("author")),
        "translator": _joined(distinct("translator")),
        "language": languages[0] if languages else "",
    }


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
    elif not isinstance(subjects, list):
        subjects = []
    front_matter = project.get("front_matter")
    note_heading, note_body = _note(project_dir / TRANSLATOR_NOTE_FILE)
    notice_heading, notice_body = _note(project_dir / EDITION_NOTICE_FILE)
    about_heading, about_body = _note(project_dir / ABOUT_BOOK_FILE)

    author = _first(project.get("author"), harness.get("author"), ingest.get("creator"))
    translator = _text(project.get("translator"))
    language = _first(project.get("target_lang_code"), harness.get("language_code"))
    if not (author and translator and language):
        derived = collection_defaults(project_dir)
        author = author or derived.get("author", "")
        translator = translator or derived.get("translator", "")
        language = language or derived.get("language", "")

    title = _first(project.get("spanish_title"), project.get("title"),
                   harness.get("title"), ingest.get("title"))
    return {
        "title": title,
        "subtitle": _text(project.get("subtitle")),
        "author": author,
        "language": language or "es",
        "translator": translator,
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
        "about_book_heading": about_heading,
        "about_book_body": about_body,
        "edition": _edition(project.get("edition")),
        "site_slug": _site_slug(_text(project.get("site_slug"))) or slugify(title)
                     or project_dir.name,
        "publish": _publish_settings(project),
    }


def unknown_forms(value: Any) -> list[str]:
    """The entries of a ``forms`` list that are not a code in :data:`FORMS`."""
    items = value if isinstance(value, list) else [value]
    return [str(item) for item in items if _text(item).upper() not in _FORM_CODES]


def normalize_forms(value: Any) -> list[str]:
    """``["III/IV", "ii", "II"]`` -> ``["II", "III/IV"]``: known codes, deduplicated, in order.

    A string is read as a one-item list; anything else, and any unknown code, is dropped.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    picked = {_text(item).upper() for item in value}
    return [code for code in _FORM_CODES if code in picked]


def load_site_metadata(project_dir: Path) -> dict[str, Any]:
    """``{forms, adaptations, parent_notes}`` from ``project.json``; blanks when unset.

    ``adaptations`` says what this edition adapted or added; ``parent_notes``
    is the book page's "Lo que los padres deben saber". Both are Markdown.
    """
    project = _read_json(Path(project_dir) / PROJECT_FILE)
    raw_forms = project.get("forms")
    if raw_forms is not None and unknown_forms(raw_forms):
        logger.warning("%s: project.json forms has unknown values %s (known: %s); dropped",
                       Path(project_dir).name, unknown_forms(raw_forms), ", ".join(_FORM_CODES))
    return {
        "forms": normalize_forms(raw_forms),
        "adaptations": _text(project.get("adaptations")),
        "parent_notes": _text(project.get("parent_notes")),
    }


#: ``project.json`` key -> the file the site's ``build.py`` reads from ``libros/<site_slug>/``.
SITE_TEXT_FILES: tuple[tuple[str, str], ...] = (
    ("adaptations", "adaptaciones.md"),
    ("parent_notes", "padres.md"),
)


def site_repo_dir() -> str:
    """The local checkout of the site repo, from the house ``site.repo_dir``; ``""`` when unset."""
    site = load_publish_defaults().get("site")
    return _text(site.get("repo_dir")) if isinstance(site, dict) else ""


def export_site_texts(project_dir: Path, site_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Write ``adaptations`` and ``parent_notes`` to the site's book folder.

    The folder is ``<site_dir>/libros/<site_slug>/`` and must already exist with
    its ``libro.toml``: creating a book page (cover, release, ages) stays a
    deliberate step in the site repo. A blank field leaves its file alone rather
    than deleting it, so a book whose notes were written by hand on the site is
    never wiped. Each file reports ``created``, ``updated``, ``unchanged`` or
    ``skipped``; ``dry_run`` reports without writing.
    """
    project_dir, site_dir = Path(project_dir), Path(site_dir)
    slug = load_book_metadata(project_dir)["site_slug"]
    book_dir = site_dir / "libros" / slug
    if not (book_dir / "libro.toml").exists():
        raise FileNotFoundError(
            f"{book_dir / 'libro.toml'} not found: create the book's folder in the site repo "
            f"first, or set project.json site_slug to the folder's name")
    texts = load_site_metadata(project_dir)
    files = []
    for key, name in SITE_TEXT_FILES:
        path = book_dir / name
        text = texts[key]
        if not text:
            files.append({"file": name, "action": "skipped",
                          "reason": f"project.json {key} is blank"})
            continue
        new = text + "\n"
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old is not None and old.replace("\r\n", "\n") == new:
            action = "unchanged"
        else:
            action = "created" if old is None else "updated"
            if not dry_run:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new)
        files.append({"file": name, "action": action, "chars": len(text)})
    return {"site_slug": slug, "book_dir": str(book_dir), "dry_run": dry_run, "files": files}


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
