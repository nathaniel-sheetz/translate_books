"""
``projects/<slug>/quotations.json``: what the book quotes from the NBLA, exactly.

Written after every apply, at the project root beside ``edition_notice.json`` and
``kdp_listing.json``, and read by :func:`src.kdp.facts.gather`. This pass records;
kdp-publish decides what the numbers mean for the copyright page.

- **Every accepted NBLA-worded card counts**, whether or not it needed an edit.
  A book already corrected by hand (short-sermons) must not count close to zero.
  Accepted means the card's latest ledger row is ``applied`` with the NBLA
  wording ticked.
- Quotations the author adapted on purpose count too (the conservative
  reading); their rows say ``adapted``. So do cards the user fixed in the book
  by hand (rows say ``hand``), counted from the mapping when there is one, else
  from the whole verse.
- Lockman does not say whether a verse quoted twice counts once or twice, so
  both are kept: ``verses_unique`` and ``verse_occurrences``. ``within_limits``
  uses the larger.
- The rows are the accounting: reference, the translation before, the NBLA after,
  and the note. There is no separate report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.quotations.versions import NBLA

FILENAME = "quotations.json"


def record_path(project_dir: Path) -> Path:
    return Path(project_dir) / FILENAME


def book_words(project_dir: Path) -> int:
    """Words in the translated chapters (``chapters/*.txt``)."""
    total = 0
    for path in sorted((Path(project_dir) / "chapters").glob("*.txt")):
        try:
            total += len(path.read_text(encoding="utf-8").split())
        except OSError:
            continue
    return total


def whole_books_quoted(verse_keys: set[str]) -> Optional[list[int]]:
    """Bible books every verse of which is quoted, or ``None`` with no index to tell."""
    from src.quotations.index import IndexError_, load_index

    if not verse_keys:
        return []
    try:
        index = load_index("nbla", check_epub=False)
    except IndexError_:
        return None
    by_book: dict[str, set[str]] = {}
    for key in verse_keys:
        by_book.setdefault(key[:2], set()).add(key)
    whole = []
    for book, keys in by_book.items():
        total = sum(1 for k in index.verses if k.startswith(book + ":"))
        if total and len(keys) >= total:
            whole.append(int(book))
    return sorted(whole)


def _spanish_ref(ref: Optional[str], sep: Optional[str]) -> Optional[str]:
    """``Jn 21:15–17`` from the card's canonical English reference."""
    from src.quotations.refs import parse_ref, spanish_abbreviation

    parsed = parse_ref(ref or "")
    if parsed is None:
        return ref
    if not parsed.verses:
        return parsed.spanish(sep)
    nums = parsed.nums_canonical().replace("-", sep or "-")
    return f"{spanish_abbreviation(parsed.book)} {nums}"


def build_record(project_dir: Path) -> dict[str, Any]:
    from src.quotations.apply import latest_decisions
    from src.quotations.board import load_mapped

    project_dir = Path(project_dir)
    mapped = load_mapped(project_dir) or {}
    cards = {c["key"]: c for c in mapped.get("cards") or []}
    sep = mapped.get("range_separator")

    rows: list[dict[str, Any]] = []
    unique: set[str] = set()
    occurrences = 0
    words = 0
    for key, decision in latest_decisions(project_dir).items():
        if decision.get("action") != "applied" or not decision.get("nbla"):
            continue
        card = cards.get(key) or {}
        quote_edits = card.get("quote_edits") or []
        keys = decision.get("verse_keys") or sorted(
            {k for e in quote_edits for k in e.get("verse_keys") or []}
        )
        n_words = decision.get("nbla_words") or sum(e.get("nbla_words") or 0 for e in quote_edits)
        unique.update(keys)
        occurrences += len(keys)
        words += n_words
        note = decision.get("drift") or card.get("drift") or ""
        if decision.get("adapted") or card.get("adapted"):
            note = "; ".join(x for x in ("adapted: " + (card.get("why") or "on purpose"), note) if x)
        hand = bool(decision.get("hand"))
        rows.append({
            "key": key,
            "chapter_id": decision.get("chapter_id"),
            "reference": _spanish_ref(decision.get("ref") or card.get("ref"), sep),
            "kind": decision.get("kind"),
            "adapted": bool(decision.get("adapted") or card.get("adapted")),
            "hand": hand,
            "verses": keys,
            "words": n_words,
            # A card fixed by hand has no edit of the pass's to show.
            "before": [] if hand else [e["old"] for e in quote_edits],
            "after": [] if hand else [e["new"] for e in quote_edits],
            "edited": hand or any(e["old"] != e["new"] for e in quote_edits),
            "note": note,
            "applied_at": decision.get("ts"),
        })
    rows.sort(key=lambda r: (r["chapter_id"] or "", r["key"]))

    total_words = book_words(project_dir)
    share = round(words / total_words, 4) if total_words else None
    whole = whole_books_quoted(unique)
    limits = dict(NBLA.limits)
    larger = max(len(unique), occurrences)
    within = (
        larger <= limits["verses"]
        and (share is None or share < limits["share"])
        and not whole
    )
    return {
        "version": NBLA.id,
        "version_name": NBLA.name,
        "notice": NBLA.notice,
        "verses_unique": len(unique),
        "verse_occurrences": occurrences,
        "quoted_words": words,
        "book_words": total_words,
        "share": share,
        "limits": limits,
        "within_limits": within,
        "whole_book_quoted": (bool(whole) if whole is not None else None),
        "whole_books": whole,
        "adapted": sum(1 for r in rows if r["adapted"]),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }


def write_record(project_dir: Path) -> dict[str, Any]:
    doc = build_record(project_dir)
    record_path(project_dir).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def read_record(project_dir: Path) -> Optional[dict[str, Any]]:
    path = record_path(project_dir)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def has_written_bible_refs(project_dir: Path) -> bool:
    """True when the English source cites a Bible verse anywhere (for kdp-publish's prompt)."""
    from src.quotations.refs import find_refs

    project_dir = Path(project_dir)
    source = project_dir / "source.txt"
    texts: list[str] = []
    if source.exists():
        texts.append(source.read_text(encoding="utf-8", errors="replace"))
    else:
        for path in sorted((project_dir / "chunks").glob("*.json")):
            try:
                texts.append(json.loads(path.read_text(encoding="utf-8")).get("source_text") or "")
            except (OSError, json.JSONDecodeError):
                continue
    return any(find_refs(t) for t in texts if re.search(r"\d+\s*[:.]\s*\d+", t))
