"""
Build one reader-home-page project card.

``/read/`` used to inline all of this in ``reader_projects()``: count chunks,
stat the style guide, load the glossary. The card now also reports the work
that remains once translation lands — open annotations, blank footnote marks,
and evaluator/judge findings — which is three more directory walks per project,
so the logic moved here and grew a cache.

Status is *derived* from those same numbers (see :func:`derive_status`) rather
than hand-set: the only thing a reader chooses is whether a book is archived.

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
import logging
import os
from pathlib import Path
from typing import Any, Optional

from src.annotations.summary import project_annotation_summary
from web_ui.evaluations import empty_type_counts, load_project_type_counts

logger = logging.getLogger(__name__)

# Every status a project can be in, in display order. The single source of
# truth: app.py validates the filter cookie against this and the template loops
# over it to build the Status picker.
PROJECT_STATUSES = ("pending", "in_progress", "complete", "archived")

# Directories whose contents feed a card, fingerprinted by (count, max mtime, size).
_WATCHED_DIRS = ("chunks", "evaluations", "alignments")
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


def _count_chunks(chunks_dir: Path) -> tuple[int, int]:
    """``(total_chunks, translated_chunks)`` over ``*_chunk_*.json``."""
    if not chunks_dir.exists():
        return (0, 0)
    total = 0
    translated = 0
    for cf in chunks_dir.glob("*_chunk_*.json"):
        total += 1
        data = _load_json(cf)
        if isinstance(data, dict) and data.get("translated_text"):
            translated += 1
    return (total, translated)


def derive_status(
    *,
    archived: bool,
    chapter_count: int,
    read_chapters: int,
    fully_translated: bool,
    awaiting_review: int,
    empty_footnotes: int,
) -> str:
    """Work out a project's status from what is actually on disk.

    Archived is the one manual choice and wins over everything else. Otherwise
    the rule reads off the same numbers the card's work chips show:

    * ``complete`` — nothing left to do: every chapter read, no annotation
      awaiting review, no blank footnote mark, and translation finished.
      Evaluator/judge flags deliberately don't count; you can finish a book
      while disagreeing with the judges.
    * ``in_progress`` — at least one chapter read.
    * ``pending`` — nothing read yet, which is also where a brand-new project
      with no chapters lands (``chapter_count == 0`` can't be "complete", or
      importing a book would mark it finished).
    """
    if archived:
        return "archived"
    if (
        chapter_count > 0
        and read_chapters >= chapter_count
        and fully_translated
        and awaiting_review == 0
        and empty_footnotes == 0
    ):
        return "complete"
    return "in_progress" if read_chapters > 0 else "pending"


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
        ``flag_counts`` — all six review categories, zero-filled), and the two
        status keys: ``archived`` (the reader's choice) and ``status`` (derived
        from all of the above by :func:`derive_status`).

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
        glossary_count = len(gdata.get("terms", []))

    total_chunks, translated_chunks = _count_chunks(project_dir / "chunks")

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

    card = {
        "id": project_id,
        "title": config.get("title") or project_id,
        "spanish_title": config.get("spanish_title", ""),
        "archived": archived,
        "status": derive_status(
            archived=archived,
            chapter_count=len(chapter_ids),
            read_chapters=read_chapters,
            fully_translated=fully_translated,
            awaiting_review=ann["awaiting_review"],
            empty_footnotes=ann["empty_footnotes"],
        ),
        "chapter_count": len(chapter_ids),
        "has_style_guide": (project_dir / "style.json").exists(),
        "glossary_count": glossary_count,
        "total_chunks": total_chunks,
        "translated_chunks": translated_chunks,
        "fully_translated": fully_translated,
        "has_alignments": bool(chapter_ids),
        "unread_chapters": unread_chapters,
        "awaiting_review": ann["awaiting_review"],
        "empty_footnotes": ann["empty_footnotes"],
        "flag_counts": flag_counts,
    }

    _CARD_CACHE[cache_key] = (fingerprint, card)
    return card
