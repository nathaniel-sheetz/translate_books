"""
Where a book stands in the translate harness, phase by phase.

The book page (``/read/<project_id>``) answers "have I done X for this book?"
with a strip of nine segments and a read-only Proceso tab. Each phase is one
of the harness steps the skills run — translation, the coded checks, triage,
the three tailored judges, the book review, the image pass and the EPUB — and
reports:

* ``state``: ``done`` / ``partial`` / ``none`` / ``na`` (and ``unknown`` for
  triage before it has been computed, see below)
* ``n`` / ``N``: chapters that have it, of the chapters there are
* ``date``: when it last ran (ISO ``YYYY-MM-DD``), or ``None``
* ``owed``: the chapter ids that still lack it

Everything here is read off files the harness already writes; nothing is
recorded for the page's sake.

**Triage is the expensive one.** "Nothing left to triage" is derived by
:func:`src.triage.findings.collect_chapter`, which anchors every dictionary
and grammar finding to its sentence — about 8 s for fabre2's 80 chapters when
nothing is cached. So :func:`book_phases` reports triage as ``unknown`` unless
a previous :func:`book_triage_phase` call cached it for the same files, and
the page fetches it when the Proceso tab is first opened.

Both results are cached the way ``project_cards`` caches a card: by a
stat-only fingerprint of the directories and files they read.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from web_ui.evaluations import (
    REVIEW_JUDGE_TYPES,
    chapter_id_from_chunk_id,
    load_all_triage_by_chunk,
    load_chapter_phase_coverage,
)
from web_ui.project_cards import _IMAGE_SUFFIXES, _dir_fingerprint, _file_fingerprint

# Display order, which is harness order. The strip draws one segment per key.
PHASE_KEYS = (
    "translation", "checks", "triage",
    "dialogue", "address", "editorial",
    "book_review", "images", "epub",
)

# The Proceso tab's groups: each key opens the group it names.
PHASE_GROUPS = {
    "translation": "translation",
    "checks": "checks",
    "dialogue": "judges",
    "book_review": "book",
}

_WATCHED_DIRS = (
    "chunks",
    "evaluations",
    "alignments",
    "images",
    "reports",
    ".harness/book_review",
    ".harness/images",
)
# ignored_terms.json changes what is left to triage without touching evaluations/.
_WATCHED_FILES = ("ignored_terms.json",)

# `book_review_2026-10-24.md`, `book_review_signals_2026-10-20.md`: the reports
# a review wrote before .harness/book_review/state.json existed.
_BOOK_REVIEW_REPORT_RE = re.compile(r"^book_review_(?:.*_)?(\d{4}-\d{2}-\d{2})\.md$")

# Resolved project path -> (fingerprint, result).
_PHASE_CACHE: dict[str, tuple[tuple, dict]] = {}
_TRIAGE_CACHE: dict[str, tuple[tuple, dict]] = {}


def clear_phase_cache() -> None:
    """Drop every cached result. For tests."""
    _PHASE_CACHE.clear()
    _TRIAGE_CACHE.clear()


def _cache_key(project_dir: Path) -> str:
    try:
        return str(project_dir.resolve())
    except OSError:
        return str(project_dir)


def _epubs(project_dir: Path) -> list[Path]:
    try:
        return [p for p in project_dir.glob("*.epub") if p.is_file()]
    except OSError:
        return []


def _fingerprint(project_dir: Path) -> tuple:
    return (
        tuple(_dir_fingerprint(project_dir / name) for name in _WATCHED_DIRS),
        tuple(_file_fingerprint(project_dir / name) for name in _WATCHED_FILES),
        tuple(sorted((p.name, *_file_fingerprint(p)) for p in _epubs(project_dir))),
    )


def _day(stamp: Any) -> Optional[str]:
    """``'2026-10-24T23:01:06'`` -> ``'2026-10-24'``; anything else -> ``None``."""
    if isinstance(stamp, str) and len(stamp) >= 10 and stamp[4] == "-":
        return stamp[:10]
    return None


def _newest(stamps) -> Optional[str]:
    days = [d for d in (_day(s) for s in stamps) if d]
    return max(days) if days else None


def _state(n: int, total: int) -> str:
    if total and n >= total:
        return "done"
    return "partial" if n else "none"


def _coverage(key: str, chapters: list[str], has: set[str], date: Optional[str]) -> dict:
    n = sum(1 for ch in chapters if ch in has)
    return {
        "key": key,
        "state": _state(n, len(chapters)),
        "n": n,
        "N": len(chapters),
        "date": date,
        "owed": [ch for ch in chapters if ch not in has],
        "per_chapter": True,
    }


def _book_level(key: str, state: str, date: Optional[str], **extra) -> dict:
    return {"key": key, "state": state, "n": 0, "N": 0, "date": date,
            "owed": [], "per_chapter": False, **extra}


def _read_chunks(project_dir: Path) -> tuple[dict[str, list[str]], dict[str, set[str]], list[str]]:
    """One read of ``chunks/*_chunk_*.json``.

    Returns ``({chapter: [chunk ids]}, {chapter: {translated chunk ids}},
    [translated_at stamps])``. The stamps are what
    ``src.book_review.progress.newest_translation`` maxes over; collecting them
    here saves the page a second pass over the same files.
    """
    chunks: dict[str, list[str]] = {}
    translated: dict[str, set[str]] = {}
    stamps: list[str] = []
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return chunks, translated, stamps
    for path in sorted(chunks_dir.glob("*_chunk_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        chunk_id = data.get("id") or path.stem
        chapter = chapter_id_from_chunk_id(path.stem)
        chunks.setdefault(chapter, []).append(chunk_id)
        if (data.get("translated_text") or "").strip():
            translated.setdefault(chapter, set()).add(chunk_id)
            if isinstance(data.get("translated_at"), str):
                stamps.append(data["translated_at"])
    return chunks, translated, stamps


def _book_review(project_dir: Path) -> dict:
    from src.book_review.progress import load_state

    date = _day(load_state(project_dir).get("reviewed_at"))
    if date is None:
        reports = project_dir / "reports"
        try:
            names = [p.name for p in reports.iterdir()] if reports.is_dir() else []
        except OSError:
            names = []
        days = [m.group(1) for m in map(_BOOK_REVIEW_REPORT_RE.match, names) if m]
        date = max(days) if days else None
    return _book_level("book_review", "done" if date else "none", date)


def _images(project_dir: Path) -> dict:
    from src.image_pass.ledger import read_rows

    images = project_dir / "images"
    try:
        count = sum(
            1 for p in images.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
        ) if images.is_dir() else 0
    except OSError:
        count = 0
    if not count:
        return _book_level("images", "na", None, count=0)
    # Any ledger row means the pass has been run on this book, including a
    # book whose only image is its cover; with no row it is owed even then.
    try:
        rows = read_rows(project_dir)
    except (OSError, UnicodeDecodeError):
        rows = []
    date = _newest(r.get("ts") for r in rows)
    return _book_level("images", "done" if rows else "none", date, count=count)


def _epub(project_dir: Path) -> dict:
    epubs = _epubs(project_dir)
    date = None
    if epubs:
        from datetime import datetime

        newest = max(os.path.getmtime(p) for p in epubs)
        date = datetime.fromtimestamp(newest).date().isoformat()
    return _book_level("epub", "done" if epubs else "none", date)


def _unknown_triage(chapters: list[str]) -> dict:
    return {"key": "triage", "state": "unknown", "n": 0, "N": len(chapters),
            "date": None, "owed": [], "per_chapter": True}


def _summarize(phases: list[dict]) -> dict:
    counted = [p for p in phases if p["state"] not in ("na", "unknown")]
    return {
        "phases": phases,
        "done": sum(1 for p in counted if p["state"] == "done"),
        # A phase that does not apply (no images) is not one of the book's
        # phases; one not yet computed still is.
        "total": sum(1 for p in phases if p["state"] != "na"),
        "missing": [p["key"] for p in counted if p["state"] != "done"],
    }


def _compute(project_dir: Path) -> tuple[list[str], set[str], dict]:
    """Every phase but triage. Returns ``(chapters, coded chapters, phases by key)``."""
    chunks, translated, stamps = _read_chunks(project_dir)
    chapters = sorted(chunks)
    coverage = load_chapter_phase_coverage(project_dir)

    fully_translated = {ch for ch in chapters if len(translated.get(ch, ())) == len(chunks[ch])}

    def judged(ch: str, pred) -> bool:
        """Every translated chunk in ``ch`` satisfies ``pred`` (and there is one)."""
        ids = translated.get(ch)
        if not ids:
            return False
        recs = coverage.get(ch, {})
        return all(cid in recs and pred(recs[cid]) for cid in ids)

    all_recs = [rec for recs in coverage.values() for rec in recs.values()]

    coded = {ch for ch in chapters if judged(ch, lambda r: r["coded"])}
    phases = {
        "translation": _coverage("translation", chapters, fully_translated, _newest(stamps)),
        "checks": _coverage("checks", chapters, coded,
                            _newest(r["evaluated_at"] for r in all_recs)),
    }
    for name in REVIEW_JUDGE_TYPES:
        has = {ch for ch in chapters if judged(ch, lambda r, n=name: n in r["judges"])}
        date = _newest(r["judges"].get(name) for r in all_recs)
        phases[name] = _coverage(name, chapters, has, date)
    phases["book_review"] = _book_review(project_dir)
    phases["images"] = _images(project_dir)
    phases["epub"] = _epub(project_dir)
    return chapters, coded, phases


def _phases_entry(project_dir: Path, fingerprint: tuple) -> dict:
    """The cached non-triage result for ``fingerprint``, computing it on a miss."""
    key = _cache_key(project_dir)
    cached = _PHASE_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    chapters, coded, phases = _compute(project_dir)
    entry = {"chapters": chapters, "coded": coded, "phases": phases}
    _PHASE_CACHE[key] = (fingerprint, entry)
    return entry


def _assemble(entry: dict, triage: dict) -> dict:
    phases = [triage if k == "triage" else entry["phases"][k] for k in PHASE_KEYS]
    return {"chapters": entry["chapters"], **_summarize(phases)}


def book_phases(project_dir: Path) -> dict:
    """Every phase for one book, triage only when it is already cached.

    Returns ``{"chapters", "phases", "done", "total", "missing"}``: the
    chapter ids (from ``chunks/``), the nine phase dicts in :data:`PHASE_KEYS`
    order, how many are done of how many apply, and the keys still owed. A
    triage phase not computed yet is ``unknown`` and counts toward ``total``
    but not ``missing``.

    The dict is the cached instance's data — callers must treat it as read-only.
    """
    project_dir = Path(project_dir)
    fingerprint = _fingerprint(project_dir)
    entry = _phases_entry(project_dir, fingerprint)
    cached = _TRIAGE_CACHE.get(_cache_key(project_dir))
    triage = cached[1] if cached is not None and cached[0] == fingerprint else None
    return _assemble(entry, triage or _unknown_triage(entry["chapters"]))


def book_triage_phase(project_dir: Path) -> dict:
    """:func:`book_phases` with triage computed, which can take seconds.

    A chapter has been triaged when it has coded results **and** nothing is
    left to triage in it: every dictionary and grammar finding is dismissed,
    ignored, already ruled on, or cannot be anchored. That is derived rather
    than read off ``_triage.jsonl``, which only has rows for chapters whose
    findings needed a verdict — a complete triage of a book with clean chapters
    would otherwise read as partial.
    """
    from src.triage.findings import collect_chapter

    project_dir = Path(project_dir)
    fingerprint = _fingerprint(project_dir)
    entry = _phases_entry(project_dir, fingerprint)
    key = _cache_key(project_dir)
    cached = _TRIAGE_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return _assemble(entry, cached[1])

    chapters = entry["chapters"]
    aligned = project_dir / "alignments"
    done = {
        ch for ch in chapters
        if ch in entry["coded"]
        and (aligned / f"{ch}.json").exists()
        and not collect_chapter(project_dir, ch)
    }
    date = _newest(
        row.get("ts")
        for rows in load_all_triage_by_chunk(project_dir).values()
        for row in rows
    )
    triage = _coverage("triage", chapters, done, date)
    _TRIAGE_CACHE[key] = (fingerprint, triage)
    return _assemble(entry, triage)
