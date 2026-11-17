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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src import edition as edition_mod
from src import epub_readback
from src.book_metadata import (
    ABOUT_BOOK_FILE,
    EDITION_NOTICE_FILE,
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

KNOWN_KEYS = ("edition", "forms", "adaptations", "parent_notes", "about_book", "description",
              "resumen")
CURRENT_FILE = "current.json"

_REVIEW_SHA_RE = re.compile(r"Source SHA-256[^`\n]*`([0-9a-fA-F]{64})`")
_REVIEWED_SOURCE_RE = re.compile(r"\]\(([^)\n]+?\.txt):\d+\)")
_SITE_MARKDOWN_KEYS = ("adaptations", "parent_notes", "description")

_ANY_TARGET_TAG_RE = re.compile(r"\[(LIBRE|KINDLE)\]", re.IGNORECASE)
_PAGE_VAR_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_RESUMEN_LINE_RE = re.compile(
    r'^(?P<key>resumen[ \t]*=[ \t]*)"(?:[^"\\\r\n]|\\.)*"(?P<tail>[ \t]*(?:#[^\r\n]*)?)(?P<eol>\r?)$',
    re.MULTILINE,
)
# «cap. 31», «caps. 12 y 19», «sermón XIII»: the chapter word, then arabic or roman numerals.
_NUMERAL = r"(?:\d+|[IVXLCDM]+\b)"
_CHAPTER_REF_RE = re.compile(
    rf"\b(?i:cap(?:ítulos?|s)?\.?|sermón|sermones|lección|lecciones)\s+"
    rf"({_NUMERAL}(?:\s*(?:,|y|e|–|-|a)\s*{_NUMERAL})*)")
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
#: The texts that cite chapters, checked against the last chapter of the book.
_CHAPTER_CITING_KEYS = ("adaptations", "parent_notes")

# A text in metadata_draft.md: a marker line, then a fenced block holding it whole.
_DRAFT_BLOCK_RE = re.compile(
    r"(?P<head><!--[ \t]*text:[ \t]*(?P<key>\w+)[ \t]*-->[ \t]*\r?\n(?P<fence>`{3,})[^\n]*\n)"
    r"(?P<body>.*?)(?P<tail>\r?\n(?P=fence)[ \t]*$)",
    re.DOTALL | re.MULTILINE)
#: ``(key, heading, fence language)`` for each text, in the order the draft shows them.
DRAFT_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("edition", "The edition (title page and copyright page)", "json"),
    ("description", "Description (`descripcion.md`, `dc:description`)", "markdown"),
    ("resumen", "Card summary (`resumen`)", "text"),
    ("forms", "`forms`", "text"),
    ("adaptations", "«Sobre esta edición» (`adaptations`)", "markdown"),
    ("parent_notes", "«Lo que los padres deben saber» (`parent_notes`)", "markdown"),
    ("about_book", "«Sobre este libro» (`about_book`, both EPUBs)", "text"),
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


newest_epub = epub_readback.newest_epub


def read_epub(epub_path: Path) -> dict[str, Any]:
    """``{endnotes, has_about_book}`` read back from a built EPUB."""
    pages = epub_readback.page_texts(epub_path)
    return {
        "endnotes": epub_readback.count_endnotes_in(pages.get(epub_readback.ENDNOTES_PAGE, "")),
        "has_about_book": epub_readback.ABOUT_BOOK_PAGE in pages,
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
    for name in ("corrections_applied.jsonl", "annotations.jsonl", ABOUT_BOOK_FILE,
                 "edition_notice.json"):
        path = project_dir / name
        if path.exists() and path.stat().st_mtime > built:
            out.append(name)
    chunks = project_dir / "chunks"
    if chunks.is_dir():
        latest = max((p.stat().st_mtime for p in chunks.glob("*.json")), default=0)
        if latest > built:
            out.append("chunks/")
    return out


def site_status(project_dir: Path) -> dict[str, Any]:
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


def _review(project_dir: Path, name: str) -> dict[str, Any]:
    """One content review, and whether it was written on this project's ``source.txt``.

    A review records the SHA-256 of the text it read. When that is not the
    project's source, the source was edited before translation, and those edits
    are changes the edition made too.
    """
    path = project_dir / name
    out: dict[str, Any] = {"file": name, "modified": _stamp(path)}
    text = path.read_text(encoding="utf-8", errors="replace")
    recorded = _REVIEW_SHA_RE.search(text)
    source = project_dir / "source.txt"
    if recorded and source.exists():
        import hashlib

        out["source_sha256"] = recorded.group(1).lower()
        out["matches_source"] = (
            hashlib.sha256(source.read_bytes()).hexdigest() == out["source_sha256"])
        if not out["matches_source"]:
            linked = _REVIEWED_SOURCE_RE.search(text)
            out["reviewed_source"] = linked.group(1) if linked else None
    return out


def chapter_map(project: dict[str, Any]) -> dict[str, Any]:
    """``{files: {chapter_NN: what the EPUB heading shows}, last_number}`` from the manifest.

    A review cites the source's own numerals, and ``chapters/chapter_NN.txt`` is
    offset from them by the front matter; the texts cite the number shown here.
    """
    files: dict[str, str] = {}
    last = 0
    for entry in project.get("chapter_manifest") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        number = entry.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            files[str(entry["id"])] = str(number)
            last = max(last, number)
        else:
            files[str(entry["id"])] = f"{entry.get('kind') or 'other'}: {entry.get('label') or ''}".strip()
    return {"files": files, "last_number": last or None}


def gather_context(project_dir: Path, epub: Optional[Path] = None) -> dict[str, Any]:
    """What ``check`` and ``apply`` need to know about the book besides the texts.

    The built EPUB's note count, ``project.json`` as it stands, the notice of
    the Bible version the book quotes, the last chapter number, and the house
    defaults.
    """
    from src.kdp import load_defaults
    from src.quotations.record import read_record

    project_dir = Path(project_dir)
    epub = Path(epub) if epub else newest_epub(project_dir)
    endnotes = read_epub(epub)["endnotes"] if epub and epub.exists() else None
    project = _read_object(project_dir / PROJECT_FILE)
    record = read_record(project_dir) or {}
    return {
        "epub": str(epub) if epub and epub.exists() else None,
        "endnotes": endnotes,
        "project": project,
        "scripture_notice": str(record.get("notice") or "") if record.get("rows") else "",
        "last_chapter": chapter_map(project)["last_number"],
        "defaults": load_defaults(),
        "draft": read_draft(project_dir / DRAFT_FILE),
    }


# ── metadata_draft.md ───────────────────────────────────────────────────────

def draft_value(key: str, texts: dict[str, Any]) -> Optional[str]:
    """How ``texts[key]`` is shown in its fenced block; ``None`` when there is nothing to show."""
    value = texts.get(key)
    if key == "forms":
        return ", ".join(str(v) for v in value) if isinstance(value, list) else None
    if key == "edition":
        return json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else None
    if key == "about_book":
        value = value.get("body") if isinstance(value, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _from_draft(key: str, block: str, texts: dict[str, Any]) -> Any:
    """A fenced block read back into the value ``texts.json`` holds. ``ValueError`` if unreadable."""
    block = block.strip()
    if key == "forms":
        return [part.strip() for part in re.split(r"[,\n]", block) if part.strip()]
    if key == "edition":
        value = json.loads(block)
        if not isinstance(value, dict):
            raise ValueError("the edition block must hold a JSON object")
        return value
    if key == "about_book":
        heading = (texts.get("about_book") or {}).get("heading") if isinstance(
            texts.get("about_book"), dict) else None
        return {"heading": heading or DEFAULT_ABOUT_HEADING, "body": block}
    return block


def read_draft(path: Path) -> dict[str, str]:
    """``{key: block text}`` for every marked fenced block of ``metadata_draft.md``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group("key"): m.group("body").replace("\r\n", "\n")
            for m in _DRAFT_BLOCK_RE.finditer(text) if m.group("key") in KNOWN_KEYS}


def _fenced(key: str, language: str, value: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"<!-- text: {key} -->\n{fence}{language}\n{value}\n{fence}"


def render_draft(project_dir: Path, texts: dict[str, Any], *, title: str = "",
                 today: Optional[str] = None, dry_run: bool = False) -> dict[str, Any]:
    """Put the texts of ``texts.json`` into the fenced blocks of ``metadata_draft.md``.

    A draft that does not exist is started with one marked block per text and
    empty evidence sections for the agent to fill. In an existing draft only
    the marked blocks are rewritten; everything around them is left alone. A
    text with no marked block is reported, not appended.
    """
    path = Path(project_dir) / DRAFT_FILE
    values = {key: draft_value(key, texts) for key, _, _ in DRAFT_SECTIONS}
    if not path.exists():
        when = today or datetime.now().date().isoformat()
        lines = [f"# Metadata draft: {title or Path(project_dir).name}", "",
                 f"{when}. Nothing has been written to the book or the site yet.", "",
                 "Made from: (the content review; the built EPUB and its date).", ""]
        for number, (key, heading, language) in enumerate(DRAFT_SECTIONS, 1):
            lines += [f"## {number}. {heading}", ""]
            if values[key] is not None:
                lines += [_fenced(key, language, values[key]), ""]
            else:
                lines += ["(not drafted)", ""]
        lines += ["## What the record shows", "",
                  "| Finding | Where | Evidence |", "|---|---|---|", "",
                  "## Decisions", "",
                  "Gaps, each with the draft line it would change, then what was left out on "
                  "purpose.", ""]
        if not dry_run:
            path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return {"path": str(path), "action": "created", "dry_run": dry_run,
                "blocks": {k: "written" for k, v in values.items() if v is not None},
                "missing_markers": []}

    text = path.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}

    def _swap(match: re.Match) -> str:
        key = match.group("key")
        value = values.get(key)
        if value is None:
            blocks[key] = "kept (not in texts.json)"
            return match.group(0)
        same = match.group("body").replace("\r\n", "\n").strip() == value.strip()
        blocks[key] = "unchanged" if same else "updated"
        return match.group("head") + value + match.group("tail")

    new = _DRAFT_BLOCK_RE.sub(_swap, text)
    missing = [key for key, value in values.items() if value is not None and key not in blocks]
    if new != text and not dry_run:
        path.write_text(new, encoding="utf-8", newline="\n")
    return {"path": str(path), "action": "updated" if new != text else "unchanged",
            "dry_run": dry_run, "blocks": blocks, "missing_markers": missing,
            "note": ("add `<!-- text: KEY -->` on the line above a text's fenced block so it "
                     "can be kept in step with texts.json") if missing else None}


def pull_draft(project_dir: Path, texts: dict[str, Any]) -> dict[str, Any]:
    """Read the marked blocks of ``metadata_draft.md`` back into ``texts``, word for word.

    Returns ``{"texts": the merged texts, "changed": [keys], "errors": [...]}``.
    This is the path for a user who edits the draft file: their wording is final.
    """
    merged = dict(texts)
    changed, errors = [], []
    for key, block in read_draft(Path(project_dir) / DRAFT_FILE).items():
        try:
            value = _from_draft(key, block, texts)
        except ValueError as exc:
            errors.append(f"{key}: the draft's block cannot be read ({exc})")
            continue
        if value != texts.get(key):
            merged[key] = value
            changed.append(key)
    return {"texts": merged, "changed": changed, "errors": errors}


def _roman(numeral: str) -> int:
    total = 0
    for char, following in zip(numeral, numeral[1:] + " "):
        value = _ROMAN[char]
        total += -value if _ROMAN.get(following, 0) > value else value
    return total


def chapters_cited(text: str) -> list[int]:
    """The chapter numbers ``text`` cites («cap. 31», «caps. 12 y 19», «sermón XIII»)."""
    out = []
    for match in _CHAPTER_REF_RE.finditer(text):
        for numeral in re.findall(_NUMERAL, match.group(1)):
            out.append(int(numeral) if numeral.isdigit() else _roman(numeral))
    return out


def works(project_dir: Path) -> list[tuple[str, Path]]:
    """``[(slug, dir)]`` for the projects whose records make up this book.

    A book is its own one work, with a blank slug. A collection volume is its
    members: each was translated, reviewed and corrected in its own folder, and
    the volume only binds them.
    """
    from src import collection as collection_mod

    project_dir = Path(project_dir)
    if not collection_mod.is_collection_dir(project_dir):
        return [("", project_dir)]
    try:
        coll = collection_mod.load_collection(project_dir)
    except ValueError:
        return [("", project_dir)]
    return [(member.slug, member.path) for member in coll.members if member.path is not None]


def status(project_dir: Path) -> dict[str, Any]:
    """What a draft can be built from, and what is already written."""
    project_dir = Path(project_dir)
    meta = load_book_metadata(project_dir)
    current = load_site_metadata(project_dir)
    about = _read_object(project_dir / ABOUT_BOOK_FILE)
    project = _read_object(project_dir / PROJECT_FILE)
    guide, guide_kind = guide_file()
    book_works = works(project_dir)
    is_collection = bool(book_works) and book_works[0][0] != ""

    reviews = []
    unreviewed = []
    for slug, work_dir_ in book_works:
        found = [_review(work_dir_, name) for name in REVIEW_FILES if (work_dir_ / name).exists()]
        for review in found:
            if slug:
                review["work"] = slug
                review["file"] = f"{slug}/{review['file']}"
        reviews.extend(found)
        if slug and not found:
            unreviewed.append(slug)

    epub = newest_epub(project_dir)
    epub_info: Optional[dict[str, Any]] = None
    if epub:
        epub_info = {"path": str(epub), "modified": _stamp(epub), **read_epub(epub)}
        epub_info["changed_since_build"] = _newer_than(epub, project_dir)

    corrections = 0
    for _slug, work_dir_ in book_works:
        log = work_dir_ / "corrections_applied.jsonl"
        if log.exists():
            corrections += sum(1 for line in log.read_text(encoding="utf-8").splitlines()
                               if line.strip())
    inputs = {
        "edition_notice.json": (project_dir / "edition_notice.json").exists(),
        "kdp_listing.json": (project_dir / "kdp_listing.json").exists(),
        "corrections_applied.jsonl": corrections,
        "images_original/": any((d / "images_original").is_dir() for _s, d in book_works),
        "cover/": (project_dir / "cover").is_dir(),
        "source.txt": any((d / "source.txt").exists() for _s, d in book_works),
    }

    draft = project_dir / DRAFT_FILE
    texts = work_dir(project_dir) / TEXTS_FILE
    needs = []
    if not reviews:
        needs.append("no content review in the project folder (family-review.md or "
                     "content-review.md): run family-review or theology-review, or ask where it is")
    for slug in unreviewed if reviews else []:
        needs.append(f"no content review for {slug}, one of this volume's works: run "
                     "family-review or theology-review on it, or ask where it is")
    if not epub:
        needs.append("no built EPUB: build one (harness.py epub) so the note count can be read")
    elif epub_info and epub_info["changed_since_build"]:
        needs.append("the EPUB is older than " + ", ".join(epub_info["changed_since_build"])
                     + ": rebuild before counting notes")
    for review in reviews:
        if review.get("matches_source") is False:
            where = review.get("reviewed_source") or "the text the review links to"
            work = f" --project {review['work']}" if review.get("work") else ""
            needs.append(
                f"{review['file']} was written on another text than this project's source.txt: "
                "the edits made before translation are changes this edition made too. Compare "
                f"them with: book_metadata.py source-diff{work} --against \"{where}\"")

    edition_now = edition_mod.current(project, _read_object(project_dir / EDITION_NOTICE_FILE))
    ask = []
    if not edition_now["version"]:
        ask.append("edition version and status (project.json edition): the title page line, and "
                   "a libre build refuses without it")
    if not is_collection and not project.get("original_year"):
        ask.append("original_year: first publication of the original work, for the copyright page")

    return {
        "project": project_dir.name,
        "collection": [slug for slug, _dir in book_works] if is_collection else None,
        "title": meta["title"],
        "author": meta["author"],
        "guide": {"path": str(guide), "kind": guide_kind},
        "reviews": reviews,
        "epub": epub_info,
        "inputs": inputs,
        "edition": edition_now,
        "chapters": chapter_map(project),
        "ask": ask,
        "current": {
            "forms": current["forms"],
            "adaptations_chars": len(current["adaptations"]),
            "parent_notes_chars": len(current["parent_notes"]),
            "about_book_chars": len(str(about.get("body") or "")),
        },
        "site": site_status(project_dir),
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
    found_errors, found_warnings = epub_readback.note_count_issues(field, text, endnotes)
    errors.extend(found_errors)
    warnings.extend(found_warnings)


def _check_about_body(body: str, errors: list[str], warnings: list[str]) -> None:
    field = "about_book.body"
    if re.search(r"gutenberg", body, re.IGNORECASE):
        errors.append(f"{field}: names Gutenberg, which the Kindle build's verify-epub refuses")
    found_errors, found_warnings = epub_readback.page_markup_issues(field, body)
    errors.extend(found_errors)
    warnings.extend(found_warnings)


def current_texts(project_dir: Path) -> dict[str, Any]:
    """Every text as it is written today, in the shape of ``texts.json``.

    A book with texts in place is a revision: this is what the new draft is
    compared with. The site's ``descripcion.md`` and ``resumen`` are read from
    the site repo when the book has a folder there.
    """
    project_dir = Path(project_dir)
    project = _read_object(project_dir / PROJECT_FILE)
    about = _read_object(project_dir / ABOUT_BOOK_FILE)
    site_now = load_site_metadata(project_dir)
    out: dict[str, Any] = {
        "edition": edition_mod.current(project, _read_object(project_dir / EDITION_NOTICE_FILE)),
        "forms": site_now["forms"],
        "adaptations": site_now["adaptations"],
        "parent_notes": site_now["parent_notes"],
        "about_book": {"heading": about.get("heading") or "", "body": about.get("body") or ""},
        "description": "",
        "resumen": "",
        "project_description": project.get("description") or "",
    }
    site_info = site_status(project_dir)
    if site_info.get("book_dir_exists"):
        book_dir = Path(site_info["book_dir"])
        description = book_dir / "descripcion.md"
        if description.exists():
            out["description"] = description.read_text(encoding="utf-8").strip()
        try:
            import tomllib

            out["resumen"] = str(tomllib.loads(
                (book_dir / "libro.toml").read_text(encoding="utf-8")).get("resumen") or "")
        except (ModuleNotFoundError, OSError, ValueError):
            pass
    return out


def check(texts: dict[str, Any], *, endnotes: Optional[int] = None,
          context: Optional[dict[str, Any]] = None) -> dict[str, list[str]]:
    """``{errors, warnings}`` for a drafted ``texts.json``.

    ``endnotes`` is the note count of the built EPUB, or ``None`` when there is
    no EPUB. ``context`` (:func:`gather_context`) adds what the edition rules need:
    ``project.json`` as it stands and the Bible version's notice. Errors block
    ``apply``; warnings are for the agent to weigh.
    """
    errors: list[str] = []
    warnings: list[str] = []
    context = context or {}
    if endnotes is None:
        endnotes = context.get("endnotes")

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

    last_chapter = context.get("last_chapter")
    if last_chapter:
        citing = {key: texts.get(key) for key in _CHAPTER_CITING_KEYS}
        if isinstance(texts.get("about_book"), dict):
            citing["about_book.body"] = texts["about_book"].get("body")
        for field, value in citing.items():
            if not isinstance(value, str):
                continue
            beyond = sorted({n for n in chapters_cited(value) if n > last_chapter})
            if beyond:
                errors.append(
                    f"{field}: cites chapter {', '.join(str(n) for n in beyond)}, and the book's "
                    f"last chapter is {last_chapter}; cite the number the EPUB heading shows")

    for key, block in (context.get("draft") or {}).items():
        shown = draft_value(key, texts)
        if shown is not None and shown.strip() != block.strip():
            warnings.append(
                f"draft_out_of_sync: {key} in metadata_draft.md differs from texts.json "
                "(`draft` pushes texts.json into the draft, `draft --pull` reads the draft back)")

    edition_errors, edition_warnings = edition_mod.check(
        texts, project=context.get("project"), endnotes=endnotes,
        scripture_notice=context.get("scripture_notice") or "")
    errors.extend(edition_errors)
    warnings.extend(edition_warnings)

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
          dry_run: bool = False, context: Optional[dict[str, Any]] = None,
          today: Optional[Any] = None) -> dict[str, Any]:
    """Write the edition and the site fields to ``project.json``, the copyright page
    to ``edition_notice.json`` and ``about_book`` to ``about_book.json``.

    Refuses while :func:`check` has errors. A key left out of ``texts`` is left
    alone; a blank text or an empty ``forms`` removes its key, as the Export
    tab's Website box does. ``description`` is written to ``project.json`` as
    plain text (``dc:description``); its Markdown and ``resumen`` are the
    site's, and :func:`site` writes them. ``context`` is :func:`gather_context`; left
    out, it is gathered here.
    """
    project_dir = Path(project_dir)
    context = dict(context) if context is not None else gather_context(project_dir)
    # The edition rules judge a missing field by what project.json has right now.
    context["project"] = _read_object(project_dir / PROJECT_FILE)
    report = check(texts, endnotes=endnotes, context=context)
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

    plan = edition_mod.planned(texts, project, context.get("defaults"), today=today)
    for key, after in plan["project_json"].items():
        before = project.get(key)
        changes.append(_change(PROJECT_FILE, key, "" if before is None else before, after))
        updated[key] = after
    for key in plan["remove"]:
        if key in project:
            changes.append(_change(PROJECT_FILE, key, project[key], ""))
            updated.pop(key, None)
    notice_path = project_dir / EDITION_NOTICE_FILE
    notice_before = _read_object(notice_path)
    notice_after = plan["edition_notice"]
    if notice_after is not None:
        for field in ("heading", "body"):
            changes.append(_change(EDITION_NOTICE_FILE, field, notice_before.get(field) or "",
                                   notice_after[field]))

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
        if notice_after is not None and notice_after != {
                k: notice_before.get(k) for k in ("heading", "body")}:
            with open(notice_path, "w", encoding="utf-8") as f:
                json.dump(notice_after, f, indent=2, ensure_ascii=False)

    left = [key for key in ("description", "resumen") if key in texts]
    in_epub = any(c["action"] != "unchanged" and c["file"] != PROJECT_FILE for c in changes) or any(
        c["action"] != "unchanged" and c["field"] not in ("forms", "adaptations", "parent_notes")
        for c in changes)
    return {
        "applied": not dry_run,
        "dry_run": dry_run,
        **report,
        "changes": changes,
        "site_only": left,
        "next": ("the EPUBs carry these changes only after a rebuild (both targets)"
                 if in_epub else None),
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
