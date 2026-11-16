"""The deterministic half of the ``book-metadata`` skill.

The skill drafts a book's site and «Sobre este libro» texts into
``projects/<slug>/.harness/metadata/texts.json``::

    {
      "forms": ["II"],
      "adaptations": "…Markdown: the site's «Sobre esta edición»…",
      "parent_notes": "…Markdown: the site's «Lo que los padres deben saber»…",
      "about_book": {"heading": "Sobre este libro", "body": "…plain paragraphs…"},
      "description": "…Markdown: the site's descripcion.md…",
      "resumen": "…one sentence: the site card's summary…"
    }

Every key is optional: a key left out is left alone wherever it would land.

``status`` says what a draft can be built from (the content review, the built
EPUB and its note count, what is already written). ``check`` refuses what the
EPUB page would render as literal characters and any note count that does not
match the built EPUB. ``apply`` writes ``project.json`` and ``about_book.json``;
``site`` writes the site repo's book folder. Nothing here drafts a text.

The note-count check reads Spanish («71 notas», «no lleva notas»): the site
these texts are written for is Spanish.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.book_metadata import (
    ABOUT_BOOK_FILE,
    PROJECT_FILE,
    export_site_texts,
    load_book_metadata,
    load_site_metadata,
    normalize_forms,
    site_repo_dir,
    unknown_forms,
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
GUIDE_FILE = _PROMPTS_DIR / "book_metadata_guide.md"
GUIDE_EXAMPLE_FILE = _PROMPTS_DIR / "book_metadata_guide.example.md"

TEXTS_FILE = "texts.json"
DRAFT_FILE = "metadata_draft.md"
DEFAULT_ABOUT_HEADING = "Sobre este libro"

#: The content reviews a draft is built from: family-review, then theology-review.
REVIEW_FILES = ("family-review.md", "content-review.md", "theology-review.md")

KNOWN_KEYS = ("forms", "adaptations", "parent_notes", "about_book", "description", "resumen")
_SITE_MARKDOWN_KEYS = ("adaptations", "parent_notes", "description")

_ENDNOTE_ANCHOR_RE = re.compile(r'<a id="en-\d+"')
_NOTE_COUNT_RE = re.compile(r"\b(\d+)\s+notas\b", re.IGNORECASE)
_NO_NOTES_RE = re.compile(r"\bno\s+lleva\s+notas\b", re.IGNORECASE)
_TARGET_TAG_RE = re.compile(r"^\[(LIBRE|KINDLE)\][ \t]*\n?", re.IGNORECASE)
_ANY_TARGET_TAG_RE = re.compile(r"\[(LIBRE|KINDLE)\]", re.IGNORECASE)
_PAGE_VAR_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_PAGE_VARS = ("SITIO", "COMENTARIOS")
_LINK_RE = re.compile(r"\[[^\[\]\n]+\]\([^)\n]*\)")
# Markdown the EPUB's generated pages do not render: it would show as typed.
_LITERAL_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+\S|\d+[.)]\s+\S|#{1,6}\s+\S|>\s*\S)")
_RESUMEN_LINE_RE = re.compile(
    r'^(?P<key>resumen[ \t]*=[ \t]*)"(?:[^"\\\r\n]|\\.)*"(?P<tail>[ \t]*(?:#[^\r\n]*)?)(?P<eol>\r?)$',
    re.MULTILINE,
)


def work_dir(project_dir: Path) -> Path:
    """``projects/<slug>/.harness/metadata/`` (not created here)."""
    return Path(project_dir) / ".harness" / "metadata"


def guide_file() -> tuple[Path, str]:
    """The writing guide the drafts follow: the user's own, else the tracked example."""
    if GUIDE_FILE.exists():
        return GUIDE_FILE, "user"
    return GUIDE_EXAMPLE_FILE, "example"


def _stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def newest_epub(project_dir: Path) -> Optional[Path]:
    """The most recently written ``*.epub`` in the project folder (the Kindle build)."""
    epubs = sorted(Path(project_dir).glob("*.epub"), key=lambda p: p.stat().st_mtime)
    return epubs[-1] if epubs else None


def read_epub(epub_path: Path) -> dict[str, Any]:
    """``{endnotes, has_about_book}`` read back from a built EPUB."""
    with zipfile.ZipFile(epub_path) as zf:
        names = zf.namelist()
        notes = 0
        for name in names:
            if name.endswith("endnotes.xhtml"):
                notes += len(_ENDNOTE_ANCHOR_RE.findall(zf.read(name).decode("utf-8", "replace")))
        return {
            "endnotes": notes,
            "has_about_book": any(name.endswith("about_book.xhtml") for name in names),
        }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _newer_than(epub: Path, project_dir: Path) -> list[str]:
    """What changed after ``epub`` was built, so its counts and pages may be out of date."""
    built = epub.stat().st_mtime
    out = []
    for name in ("corrections_applied.jsonl", ABOUT_BOOK_FILE, "edition_notice.json"):
        path = project_dir / name
        if path.exists() and path.stat().st_mtime > built:
            out.append(name)
    chunks = project_dir / "chunks"
    if chunks.is_dir():
        latest = max((p.stat().st_mtime for p in chunks.glob("*.json")), default=0)
        if latest > built:
            out.append("chunks/")
    return out


def _site_status(project_dir: Path) -> dict[str, Any]:
    slug = load_book_metadata(project_dir)["site_slug"]
    repo = site_repo_dir()
    site: dict[str, Any] = {"site_slug": slug, "repo_dir": repo or None}
    if not repo:
        site["note"] = "no site repo: set site.repo_dir in prompts/publish_defaults.json"
        return site
    book_dir = Path(repo) / "libros" / slug
    site["book_dir"] = str(book_dir)
    site["book_dir_exists"] = (book_dir / "libro.toml").exists()
    if site["book_dir_exists"]:
        site["files"] = sorted(p.name for p in book_dir.iterdir() if p.is_file())
        match = _RESUMEN_LINE_RE.search((book_dir / "libro.toml").read_text(encoding="utf-8"))
        site["has_resumen_line"] = bool(match)
    else:
        site["note"] = ("no libro.toml there: create the book's folder in the site repo, or "
                        "set project.json site_slug to the folder's name")
    return site


def status(project_dir: Path) -> dict[str, Any]:
    """What a draft can be built from, and what is already written."""
    project_dir = Path(project_dir)
    meta = load_book_metadata(project_dir)
    current = load_site_metadata(project_dir)
    about = _read_object(project_dir / ABOUT_BOOK_FILE)
    guide, guide_kind = guide_file()

    reviews = [{"file": name, "modified": _stamp(project_dir / name)}
               for name in REVIEW_FILES if (project_dir / name).exists()]

    epub = newest_epub(project_dir)
    epub_info: Optional[dict[str, Any]] = None
    if epub:
        epub_info = {"path": str(epub), "modified": _stamp(epub), **read_epub(epub)}
        epub_info["changed_since_build"] = _newer_than(epub, project_dir)

    corrections = project_dir / "corrections_applied.jsonl"
    inputs = {
        "edition_notice.json": (project_dir / "edition_notice.json").exists(),
        "kdp_listing.json": (project_dir / "kdp_listing.json").exists(),
        "corrections_applied.jsonl": (
            sum(1 for line in corrections.read_text(encoding="utf-8").splitlines() if line.strip())
            if corrections.exists() else 0),
        "images_original/": (project_dir / "images_original").is_dir(),
        "cover/": (project_dir / "cover").is_dir(),
        "source.txt": (project_dir / "source.txt").exists(),
    }

    draft = project_dir / DRAFT_FILE
    texts = work_dir(project_dir) / TEXTS_FILE
    needs = []
    if not reviews:
        needs.append("no content review in the project folder (family-review.md or "
                     "content-review.md): run family-review or theology-review, or ask where it is")
    if not epub:
        needs.append("no built EPUB: build one (harness.py epub) so the note count can be read")
    elif epub_info and epub_info["changed_since_build"]:
        needs.append("the EPUB is older than " + ", ".join(epub_info["changed_since_build"])
                     + ": rebuild before counting notes")

    return {
        "project": project_dir.name,
        "title": meta["title"],
        "author": meta["author"],
        "guide": {"path": str(guide), "kind": guide_kind},
        "reviews": reviews,
        "epub": epub_info,
        "inputs": inputs,
        "current": {
            "forms": current["forms"],
            "adaptations_chars": len(current["adaptations"]),
            "parent_notes_chars": len(current["parent_notes"]),
            "about_book_chars": len(str(about.get("body") or "")),
        },
        "site": _site_status(project_dir),
        "draft": {"path": str(draft), "exists": draft.exists()},
        "texts_file": {"path": str(texts), "exists": texts.exists()},
        "needs": needs,
    }


def load_texts(path: Path) -> dict[str, Any]:
    """The drafted texts. ``ValueError`` when the file is missing or not a JSON object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"no texts file at {path}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"unreadable JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return data


def _check_counts(field: str, text: str, endnotes: Optional[int],
                  errors: list[str], warnings: list[str]) -> None:
    counts = [int(n) for n in _NOTE_COUNT_RE.findall(text)]
    says_none = bool(_NO_NOTES_RE.search(text))
    if endnotes is None:
        if counts or says_none:
            warnings.append(f"{field}: states a note count, but there is no built EPUB to check it against")
        return
    for n in counts:
        if n != endnotes:
            errors.append(f"{field}: says {n} notas, the built EPUB has {endnotes}")
    if says_none and endnotes:
        errors.append(f"{field}: says the edition has no notes, the built EPUB has {endnotes}")
    if endnotes and not counts and not says_none:
        warnings.append(f"{field}: the built EPUB has {endnotes} notes and the text gives no count")


def _check_about_body(body: str, errors: list[str], warnings: list[str]) -> None:
    field = "about_book.body"
    if "**" in body:
        errors.append(f"{field}: bold (**…**) is not rendered on this page; it would show as typed")
    if re.search(r"gutenberg", body, re.IGNORECASE):
        errors.append(f"{field}: names Gutenberg, which the Kindle build's verify-epub refuses")
    for block in (b.strip() for b in re.split(r"\n\s*\n", body)):
        if not block:
            continue
        tagged = _TARGET_TAG_RE.match(block)
        rest = block[tagged.end():] if tagged else block
        head = rest[:40].replace("\n", " ")
        if _ANY_TARGET_TAG_RE.search(rest):
            errors.append(f"{field}: a [LIBRE]/[KINDLE] tag only works at the start of a paragraph "
                          f"(«{head}…»)")
        for line in rest.split("\n"):
            if _LITERAL_LINE_RE.match(line):
                errors.append(f"{field}: lists, subheadings and block quotes are not rendered on "
                              f"this page («{line.strip()[:40]}…»)")
                break
        has_link = bool(_LINK_RE.search(rest)) or any(
            v.upper() in _PAGE_VARS for v in _PAGE_VAR_RE.findall(rest))
        if has_link and not (tagged and tagged.group(1).upper() == "LIBRE"):
            warnings.append(f"{field}: a link outside a [LIBRE] paragraph shows in the Kindle build "
                            f"as bare text («{head}…»)")
    for var in _PAGE_VAR_RE.findall(body):
        if var.upper() not in _PAGE_VARS:
            errors.append(f"{field}: unknown variable {{{{{var}}}}} (known: SITIO, COMENTARIOS)")


def check(texts: dict[str, Any], *, endnotes: Optional[int] = None) -> dict[str, list[str]]:
    """``{errors, warnings}`` for a drafted ``texts.json``.

    ``endnotes`` is the note count of the built EPUB, or ``None`` when there is
    no EPUB. Errors block ``apply``; warnings are for the agent to weigh.
    """
    errors: list[str] = []
    warnings: list[str] = []

    present = [key for key in KNOWN_KEYS if key in texts]
    if not present:
        errors.append("nothing to write: none of " + ", ".join(KNOWN_KEYS) + " is present")
    for key in texts:
        if key not in KNOWN_KEYS:
            warnings.append(f"{key}: not a known key, ignored (known: {', '.join(KNOWN_KEYS)})")

    if "forms" in texts:
        forms = texts["forms"]
        if not isinstance(forms, list):
            errors.append("forms: must be a list of form codes")
        else:
            bad = unknown_forms(forms)
            if bad:
                errors.append(f"forms: unknown code(s) {', '.join(bad)}")
            elif not forms:
                warnings.append("forms: empty, which removes the key from project.json")

    for key in ("adaptations", "parent_notes", "description", "resumen"):
        if key not in texts:
            continue
        value = texts[key]
        if not isinstance(value, str):
            errors.append(f"{key}: must be a string")
            continue
        if not value.strip():
            if key in ("adaptations", "parent_notes"):
                warnings.append(f"{key}: blank, which removes the key from project.json")
            else:
                errors.append(f"{key}: blank; leave the key out to keep what the site has")
            continue
        if key in _SITE_MARKDOWN_KEYS:
            if _ANY_TARGET_TAG_RE.search(value) or _PAGE_VAR_RE.search(value):
                errors.append(f"{key}: [LIBRE]/[KINDLE] and {{{{…}}}} only work in the EPUB's pages, "
                              "not on the site")

    if isinstance(texts.get("adaptations"), str) and texts["adaptations"].strip():
        _check_counts("adaptations", texts["adaptations"], endnotes, errors, warnings)

    if isinstance(texts.get("description"), str) and texts["description"].strip():
        first = texts["description"].strip().split("\n", 1)[0].strip()
        if not (first.startswith("**") and first.endswith("**") and len(first) > 4):
            warnings.append("description: the first line is not a bold hook (**…**)")

    if isinstance(texts.get("resumen"), str) and texts["resumen"].strip():
        resumen = texts["resumen"].strip()
        if "\n" in resumen:
            errors.append("resumen: must be one line")
        elif len(resumen) > 200:
            warnings.append(f"resumen: {len(resumen)} characters; the card summary is one sentence")

    if "about_book" in texts:
        about = texts["about_book"]
        if not isinstance(about, dict):
            errors.append("about_book: must be {heading, body}")
        else:
            body = about.get("body")
            if not isinstance(body, str) or not body.strip():
                errors.append("about_book.body: blank; leave about_book out to keep the current page")
            else:
                _check_about_body(body, errors, warnings)
                _check_counts("about_book.body", body, endnotes, errors, warnings)
            heading = about.get("heading")
            if heading is not None and not isinstance(heading, str):
                errors.append("about_book.heading: must be a string")

    return {"errors": errors, "warnings": warnings}


def _change(file: str, field: str, before: Any, after: Any) -> dict[str, Any]:
    if before == after:
        action = "unchanged"
    elif after in ("", [], None):
        action = "removed"
    elif before in ("", [], None):
        action = "set"
    else:
        action = "replaced"
    return {"file": file, "field": field, "action": action, "before": before, "after": after}


def apply(project_dir: Path, texts: dict[str, Any], *, endnotes: Optional[int] = None,
          dry_run: bool = False) -> dict[str, Any]:
    """Write ``forms``, ``adaptations`` and ``parent_notes`` to ``project.json`` and
    ``about_book`` to ``about_book.json``.

    Refuses while :func:`check` has errors. A key left out of ``texts`` is left
    alone; a blank text or an empty ``forms`` removes its key, as the Export
    tab's Website box does. ``description`` and ``resumen`` are the site's:
    :func:`site` writes them.
    """
    project_dir = Path(project_dir)
    report = check(texts, endnotes=endnotes)
    if report["errors"]:
        return {"applied": False, "dry_run": dry_run, **report, "changes": []}

    project_path = project_dir / PROJECT_FILE
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"applied": False, "dry_run": dry_run, "warnings": report["warnings"],
                "errors": [f"unreadable {project_path}: {exc}"], "changes": []}

    changes = []
    updated = dict(project)
    if "forms" in texts:
        after = normalize_forms(texts["forms"])
        changes.append(_change(PROJECT_FILE, "forms", project.get("forms") or [], after))
        if after:
            updated["forms"] = after
        else:
            updated.pop("forms", None)
    for key in ("adaptations", "parent_notes"):
        if key not in texts:
            continue
        after = texts[key].strip()
        changes.append(_change(PROJECT_FILE, key, project.get(key) or "", after))
        if after:
            updated[key] = after
        else:
            updated.pop(key, None)

    about_path = project_dir / ABOUT_BOOK_FILE
    about_after: Optional[dict[str, str]] = None
    if "about_book" in texts:
        before = _read_object(about_path)
        about_after = {
            "heading": str(texts["about_book"].get("heading") or "").strip() or DEFAULT_ABOUT_HEADING,
            "body": texts["about_book"]["body"].strip(),
        }
        changes.append(_change(ABOUT_BOOK_FILE, "heading", before.get("heading") or "",
                               about_after["heading"]))
        changes.append(_change(ABOUT_BOOK_FILE, "body", before.get("body") or "",
                               about_after["body"]))

    if not dry_run:
        if updated != project:
            with open(project_path, "w", encoding="utf-8") as f:
                json.dump(updated, f, indent=2, ensure_ascii=False)
        if about_after is not None and about_after != {
                k: _read_object(about_path).get(k) for k in ("heading", "body")}:
            with open(about_path, "w", encoding="utf-8") as f:
                json.dump(about_after, f, indent=2, ensure_ascii=False)

    left = [key for key in ("description", "resumen") if key in texts]
    return {
        "applied": not dry_run,
        "dry_run": dry_run,
        **report,
        "changes": changes,
        "site_only": left,
        "next": ("the EPUB shows «Sobre este libro» only after a rebuild"
                 if about_after is not None else None),
    }


def _write_text(path: Path, new: str, dry_run: bool) -> str:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and old.replace("\r\n", "\n") == new:
        return "unchanged"
    if not dry_run:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    return "created" if old is None else "updated"


def _set_resumen(toml_path: Path, resumen: str, dry_run: bool) -> dict[str, Any]:
    """Replace the single-line ``resumen = "…"`` entry of ``libro.toml``, nothing else."""
    raw = toml_path.read_bytes().decode("utf-8")
    match = _RESUMEN_LINE_RE.search(raw)
    if not match:
        return {"file": "libro.toml", "field": "resumen", "action": "skipped",
                "reason": 'no single-line resumen = "…" entry: add it by hand'}
    value = json.dumps(resumen, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    line = f"{match.group('key')}{value}{match.group('tail')}{match.group('eol')}"
    new = raw[:match.start()] + line + raw[match.end():]
    try:
        import tomllib
        if tomllib.loads(new).get("resumen") != resumen:
            return {"file": "libro.toml", "field": "resumen", "action": "skipped",
                    "reason": "the edited libro.toml would not read back the same resumen"}
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        pass
    except ValueError as exc:
        return {"file": "libro.toml", "field": "resumen", "action": "skipped",
                "reason": f"the edited libro.toml would not parse: {exc}"}
    if new == raw:
        return {"file": "libro.toml", "field": "resumen", "action": "unchanged"}
    if not dry_run:
        toml_path.write_bytes(new.encode("utf-8"))
    return {"file": "libro.toml", "field": "resumen", "action": "updated"}


def site(project_dir: Path, texts: dict[str, Any], site_dir: Optional[Path] = None, *,
         dry_run: bool = False) -> dict[str, Any]:
    """Write the site repo's book folder, ``<site_dir>/libros/<site_slug>/``.

    ``adaptaciones.md`` and ``padres.md`` come from ``project.json`` (so run
    :func:`apply` first); ``descripcion.md`` and the ``resumen`` line of
    ``libro.toml`` come from ``texts``. The folder and its ``libro.toml`` must
    already exist: creating a book page stays a deliberate step in the site
    repo, and so do its commits and pushes.
    """
    project_dir = Path(project_dir)
    target = Path(site_dir) if site_dir else (Path(site_repo_dir()) if site_repo_dir() else None)
    if target is None:
        return {"written": False, "dry_run": dry_run, "files": [],
                "error": "no site repo: pass --site-dir, or set site.repo_dir in "
                         "prompts/publish_defaults.json"}
    if not target.is_dir():
        return {"written": False, "dry_run": dry_run, "files": [],
                "error": f"site repo not found: {target}"}
    try:
        exported = export_site_texts(project_dir, target, dry_run=dry_run)
    except FileNotFoundError as exc:
        return {"written": False, "dry_run": dry_run, "files": [], "error": str(exc)}

    book_dir = Path(exported["book_dir"])
    files = list(exported["files"])
    description = texts.get("description")
    if isinstance(description, str) and description.strip():
        action = _write_text(book_dir / "descripcion.md", description.strip() + "\n", dry_run)
        files.append({"file": "descripcion.md", "action": action, "chars": len(description.strip())})
    else:
        files.append({"file": "descripcion.md", "action": "skipped",
                      "reason": "texts.json has no description"})
    resumen = texts.get("resumen")
    if isinstance(resumen, str) and resumen.strip():
        files.append(_set_resumen(book_dir / "libro.toml", resumen.strip(), dry_run))
    else:
        files.append({"file": "libro.toml", "field": "resumen", "action": "skipped",
                      "reason": "texts.json has no resumen"})
    return {"written": not dry_run, "dry_run": dry_run, "site_slug": exported["site_slug"],
            "book_dir": str(book_dir), "files": files,
            "next": "commit and push in the site repo; nothing here does that"}
