"""
Build one reader-home-page project card.

``/read/`` used to inline all of this in ``reader_projects()``: count chunks,
stat the style guide, load the glossary. The card now also reports the work
that remains once translation lands — open annotations, blank footnote marks,
and evaluator/judge findings — which is three more directory walks per project,
so the logic moved here and grew a cache.

Status is *derived* from those same numbers (see :func:`derive_status`) rather
than hand-set — it names the harness stage a book is in (setup, translation,
review, done). The only thing a reader chooses is whether a book is archived.

Two things keep a 21-project home page cheap:

* **Nothing translated ⇒ nothing to scan.** The card hides every work chip when
  ``translated_chunks == 0``, so a freshly imported book never pays for the
  annotation or evaluation walks.
* **A stat-only fingerprint.** Rebuilding a card reads ~1300 JSON files across
  all projects (~450 ms); fingerprinting the same tree with ``stat`` alone is
  ~45 ms, so repeat loads hit the cache and cost about a tenth as much.

The fingerprint is ``(file_count, max_mtime_ns, total_size)`` per watched
directory plus ``(mtime_ns, size)`` per watched file. Count and size catch adds
and removes; mtime catches in-place edits.
"""

from __future__ import annotations

import json
import os
import zlib
from pathlib import Path
from typing import Any, Optional

from src.annotations.summary import project_annotation_summary
from web_ui.evaluations import REVIEW_JUDGE_TYPES, empty_type_counts, load_project_type_counts

# Every status a project can be in, in display order — which is also harness
# order. The single source of truth: app.py validates the filter cookie against
# this and the template loops over it to build the Status picker.
PROJECT_STATUSES = ("pending", "needs_translation", "needs_review", "complete", "archived")

# How many cloth colors the generated cover cycles through (reader.css
# .cloth-0 … .cloth-N). A card carries its index, derived from the project id.
COVER_CLOTHS = 9

# Directories whose contents feed a card, fingerprinted by (count, max mtime, size).
# images/ because dropping in a cover.jpg must reach the home page.
_WATCHED_DIRS = ("chunks", "evaluations", "alignments", "images")
# Individual files whose contents feed a card.
_WATCHED_FILES = (
    "annotations.jsonl",
    "evaluations/_feedback.jsonl",
    "style.json",
    "glossary.json",
    "project.json",
    # Marking a chapter reviewed changes the unread count and nothing else on
    # disk, so without this the card would go stale until some unrelated file moved.
    "reviewed.json",
)

# Resolved project path -> (fingerprint, card). Module-level: the card content
# is a pure function of the files on disk, so it is safe to share across
# requests, and tests reset it with clear_card_cache().
_CARD_CACHE: dict[str, tuple[tuple, dict]] = {}


def clear_card_cache() -> None:
    """Drop every cached card. For tests, and for anything that rewrites a project wholesale."""
    _CARD_CACHE.clear()


def _dir_fingerprint(path: Path) -> tuple[int, int, int]:
    """``(file_count, max_mtime_ns, total_size)`` over one directory, stat only."""
    count = 0
    max_mtime = 0
    total_size = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                count += 1
                total_size += st.st_size
                if st.st_mtime_ns > max_mtime:
                    max_mtime = st.st_mtime_ns
    except OSError:
        return (0, 0, 0)
    return (count, max_mtime, total_size)


def _file_fingerprint(path: Path) -> tuple[int, int]:
    """``(mtime_ns, size)``, or ``(0, 0)`` when the file is absent."""
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _fingerprint(project_dir: Path) -> tuple:
    return (
        tuple(_dir_fingerprint(project_dir / name) for name in _WATCHED_DIRS),
        tuple(_file_fingerprint(project_dir / name) for name in _WATCHED_FILES),
    )


def _load_json(path: Path) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _count_chunks(chunks_dir: Path) -> tuple[int, int, int, int]:
    """``(total_chunks, translated_chunks, total_chapters, translated_chapters)``
    over ``*_chunk_*.json``.

    A chapter is the file stem before ``_chunk_``, and it counts as translated
    only when every one of its chunks is — the card's "4/15 translated" badge
    means chapters a reader can finish, not chunks.
    """
    if not chunks_dir.exists():
        return (0, 0, 0, 0)
    total = 0
    translated = 0
    # chapter id -> (chunks, translated chunks)
    chapters: dict[str, list[int]] = {}
    for cf in chunks_dir.glob("*_chunk_*.json"):
        total += 1
        counts = chapters.setdefault(cf.stem.split("_chunk_")[0], [0, 0])
        counts[0] += 1
        data = _load_json(cf)
        if isinstance(data, dict) and data.get("translated_text"):
            translated += 1
            counts[1] += 1
    done = sum(1 for n, t in chapters.values() if n == t)
    return (total, translated, len(chapters), done)


# A cover is the EPUB's cover (same names and order as
# src/epub_builder._resolve_cover), else the cover an importer saved from the
# source. Frontispieces are deliberately not used: they are illustrations, and a
# generated cover reads as "no cover yet" more honestly than a plate does.
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def find_cover(project_dir: Path) -> Optional[Path]:
    """The image the home page shows for a book, or ``None``."""
    images = Path(project_dir) / "images"
    for name in _COVER_NAMES:
        if (images / name).is_file():
            return images / name
    try:
        candidates = sorted(p for p in images.iterdir() if p.is_file())
    except OSError:
        return None
    for p in candidates:
        name = p.name.lower()
        if p.suffix.lower() in _IMAGE_SUFFIXES and (
            name.startswith("source_cover") or ".cover." in name
        ):
            return p
    return None


# Badge kinds in priority order per status: the facts that explain *why* a book
# has its status, most actionable first. The page shows a prefix of this list
# (posters two, cards as many as fit in two rows), so order is the design.
_BADGE_ORDER = {
    "pending": (),  # setup badges, built separately below
    "needs_translation": ("translated", "review", "unread", "footnotes"),
    "needs_review": ("review", "flags", "unread", "footnotes"),
}


def card_badges(card: dict, review_types_selected) -> list[dict]:
    """The badges one card shows, in display order.

    Each is ``{"kind", "count"}``; ``count`` is ``None`` for the setup badges
    (they have no number, so they cannot shorten to icon + number) and a
    ``"done/total"`` string for ``translated``. The flags badge is the one
    badge emitted at zero: the category picker re-sums it in the page and must
    be able to reveal it. Complete and archived books get no badges.
    """
    status = card["status"]
    if status == "pending":
        out = []
        if not card["has_style_guide"]:
            out.append({"kind": "no_style_guide", "count": None})
        if card["glossary_count"] == 0:
            out.append({"kind": "no_glossary", "count": None})
        if card["total_chunks"] == 0:
            out.append({"kind": "not_chunked", "count": None})
        return out

    if card["translated_chunks"] == 0:
        # Nothing translated means nothing to read or review yet; the build
        # skipped those walks, so the only fact worth a badge is the progress.
        return [{"kind": "translated",
                 "count": f'0/{card["chapters_total"]}'}] if status == "needs_translation" else []

    # No type selected counts nothing: the ticked types are the whole review
    # setting, so the badge sits at zero (and hidden) rather than counting all.
    values = {
        "translated": f'{card["chapters_translated"]}/{card["chapters_total"]}',
        "review": card["awaiting_review"],
        "flags": sum(card["flag_counts"].get(t, 0) for t in review_types_selected),
        "unread": card["unread_chapters"],
        "footnotes": card["empty_footnotes"],
    }
    return [
        {"kind": kind, "count": values[kind]}
        for kind in _BADGE_ORDER.get(status, ())
        if kind == "flags" or values[kind]
    ]


def derive_status(
    *,
    archived: bool,
    setup_complete: bool,
    total_chunks: int,
    fully_translated: bool,
    chapter_count: int,
    read_chapters: int,
    awaiting_review: int,
    empty_footnotes: int,
    judge_findings: int,
) -> str:
    """Work out a project's status from where it stands in the harness.

    Archived is the one manual choice and wins over everything else. Otherwise
    the first stage that still has work in it names the status:

    * ``pending`` — setup unfinished: no style guide, an empty glossary, or
      nothing chunked yet.
    * ``needs_translation`` — set up, but some chunk (or every one) is still
      untranslated. Reading ahead doesn't move a book past this.
    * ``needs_review`` — translated, but something is still open: an unread
      chapter, an annotation awaiting review, a blank footnote mark, or a live
      judge finding (dialogue, address, editorial). A translated book with no
      alignments yet (``chapter_count == 0``) lands here too — there is
      nothing to read, so it can't have been read.
    * ``complete`` — none of the above. Coded-checker flags (dictionary,
      grammar, …) deliberately don't count: they are right about one time in
      ten, so a book could otherwise never finish.
    """
    if archived:
        return "archived"
    if not setup_complete or total_chunks == 0:
        return "pending"
    if not fully_translated:
        return "needs_translation"
    if (
        chapter_count == 0
        or read_chapters < chapter_count
        or awaiting_review
        or empty_footnotes
        or judge_findings
    ):
        return "needs_review"
    return "complete"


def build_project_card(project_dir: Path, project_id: str) -> dict:
    """Assemble the dict the ``mode == "projects"`` template renders for one book.

    Args:
        project_dir: The project's directory (already resolved — flat or nested).
        project_id: Its leaf folder name, used as the id in URLs.

    Returns:
        The setup/progress keys the card has always shown (``id``, ``title``,
        ``spanish_title``, ``chapter_count``, ``has_style_guide``,
        ``glossary_count``, ``total_chunks``, ``translated_chunks``,
        ``fully_translated``, ``has_alignments``), the work-remaining keys
        (``unread_chapters``, ``awaiting_review``, ``empty_footnotes``, and
        ``flag_counts`` — all six review categories, zero-filled), the two
        status keys: ``archived`` (the reader's choice) and ``status`` (derived
        from all of the above by :func:`derive_status`), and what the library
        draws: ``chapters_total``/``chapters_translated``, ``cover`` (a file
        under ``images/``, or ``None``), ``cover_version`` and ``cloth``.

        The dict is the cached instance — callers must treat it as read-only.
    """
    project_dir = Path(project_dir)
    try:
        cache_key = str(project_dir.resolve())
    except OSError:
        cache_key = str(project_dir)

    fingerprint = _fingerprint(project_dir)
    cached = _CARD_CACHE.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    align_dir = project_dir / "alignments"
    chapter_ids = {f.stem for f in align_dir.glob("*.json")} if align_dir.exists() else set()
    # "Unread" is exactly "not marked reviewed" — the same rule the chapter list
    # badges per row. Read straight from reviewed.json rather than through
    # app._load_reviewed, because app.py imports this module.
    reviewed = _load_json(project_dir / "reviewed.json")
    reviewed_ids = set(reviewed) if isinstance(reviewed, dict) else set()
    unread_chapters = len(chapter_ids - reviewed_ids)
    # Intersect rather than len(reviewed_ids): reviewed.json can name chapters
    # that a re-chunk since removed, and those must not count as read.
    read_chapters = len(chapter_ids & reviewed_ids)

    glossary_count = 0
    gdata = _load_json(project_dir / "glossary.json")
    if isinstance(gdata, dict):
        terms = gdata.get("terms", [])
        if isinstance(terms, list):
            glossary_count = len(terms)

    total_chunks, translated_chunks, chapters_total, chapters_translated = _count_chunks(
        project_dir / "chunks")
    cover = find_cover(project_dir)

    # Nothing translated ⇒ the card hides every work chip anyway, so skip the
    # two expensive walks entirely.
    if translated_chunks > 0:
        ann = project_annotation_summary(project_dir)
        flag_counts = load_project_type_counts(project_dir)
    else:
        ann = {"awaiting_review": 0, "empty_footnotes": 0}
        flag_counts = empty_type_counts()

    config = _load_json(project_dir / "project.json")
    if not isinstance(config, dict):
        config = {}

    # `archived` replaced the old hand-set `status` field. Projects written
    # before the switch still carry status == "archived" and nothing else, so
    # fall back to it; PATCH /api/project/<id>/archived drops the stale key the
    # first time a book is archived or unarchived.
    archived = bool(config.get("archived", config.get("status") == "archived"))
    fully_translated = total_chunks > 0 and translated_chunks == total_chunks
    has_style_guide = (project_dir / "style.json").exists()
    setup_complete = has_style_guide and glossary_count > 0

    card = {
        "id": project_id,
        "title": config.get("title") or project_id,
        "spanish_title": config.get("spanish_title", ""),
        "archived": archived,
        "status": derive_status(
            archived=archived,
            setup_complete=setup_complete,
            total_chunks=total_chunks,
            fully_translated=fully_translated,
            chapter_count=len(chapter_ids),
            read_chapters=read_chapters,
            awaiting_review=ann["awaiting_review"],
            empty_footnotes=ann["empty_footnotes"],
            # All judge categories, not the card's picker selection: status
            # must not change with a display cookie.
            judge_findings=sum(flag_counts[k] for k in REVIEW_JUDGE_TYPES),
        ),
        "chapter_count": len(chapter_ids),
        "has_style_guide": has_style_guide,
        "glossary_count": glossary_count,
        "total_chunks": total_chunks,
        "translated_chunks": translated_chunks,
        "chapters_total": chapters_total,
        "chapters_translated": chapters_translated,
        "fully_translated": fully_translated,
        # Filename under images/ (served resized by /projects/<id>/cover-thumb),
        # and its mtime so the thumbnail URL changes when the cover does.
        "cover": cover.name if cover else None,
        "cover_version": _file_fingerprint(cover)[0] if cover else 0,
        # Which cloth color a generated cover gets: stable per book.
        "cloth": zlib.crc32(project_id.encode("utf-8")) % COVER_CLOTHS,
        "has_alignments": bool(chapter_ids),
        "unread_chapters": unread_chapters,
        "awaiting_review": ann["awaiting_review"],
        "empty_footnotes": ann["empty_footnotes"],
        "flag_counts": flag_counts,
    }

    _CARD_CACHE[cache_key] = (fingerprint, card)
    return card
