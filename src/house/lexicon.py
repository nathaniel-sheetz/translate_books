"""
The house lexicon: words rendered the same way in every book that has them.

``prompts/house_lexicon.json`` (gitignored; ``house_lexicon.example.json`` is the
tracked twin) replaces ``forced_glossary_terms.json``. Each entry is
``{english, spanish, alternatives, scope, why, origin, type_guess}``:

- ``spanish: null`` means "always raise this word as a glossary candidate",
  which is all the old forced-terms file could say;
- ``spanish`` set means the book glossary is pre-filled with it whenever the
  word occurs in the source, overridable at the glossary gate;
- ``scope`` is ``every_book`` (default) or ``group:<id>``.

A lexicon entry reaches the translator only through the book glossary, so it
costs a line only in the chunks whose English contains the word.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.house import EVERY_BOOK, PROMPTS_DIR, REPO_ROOT, in_scope
from src.house.match import term_occurs

logger = logging.getLogger(__name__)

LEXICON_FILE = PROMPTS_DIR / "house_lexicon.json"
LEXICON_EXAMPLE_FILE = PROMPTS_DIR / "house_lexicon.example.json"

#: The file the lexicon replaces. Read only when the operator has no lexicon of
#: their own yet, so an install that never migrated keeps its forced terms.
LEGACY_FORCED_FILE = REPO_ROOT / "forced_glossary_terms.json"

_TYPES = ("character", "place", "concept", "technical", "other")


def _entry(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    english = str(raw.get("english") or raw.get("term") or "").strip()
    if not english:
        return None
    spanish = raw.get("spanish")
    spanish = str(spanish).strip() if spanish not in (None, "") else None
    type_guess = str(raw.get("type_guess") or "other").strip().lower()
    return {
        "english": english,
        "spanish": spanish,
        "alternatives": [str(a).strip() for a in raw.get("alternatives") or [] if str(a).strip()],
        "scope": str(raw.get("scope") or EVERY_BOOK).strip(),
        "why": str(raw.get("why") or "").strip(),
        "origin": str(raw.get("origin") or "").strip(),
        "type_guess": type_guess if type_guess in _TYPES else "other",
    }


def _read(path: Path, key: str) -> Optional[list[dict[str, Any]]]:
    """Entries under ``key`` in ``path``, or ``None`` when the file is unusable."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return None
    raw = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        logger.warning("Ignoring %s: expected a top-level %r list", path, key)
        return None
    return [e for e in (_entry(r) for r in raw if isinstance(r, dict)) if e]


def lexicon_path() -> Path:
    """The file :func:`load_lexicon` reads first."""
    return LEXICON_FILE if LEXICON_FILE.exists() else LEXICON_EXAMPLE_FILE


def load_lexicon() -> list[dict[str, Any]]:
    """Every lexicon entry, whatever its scope.

    Order of sources: the operator's lexicon; else their legacy
    ``forced_glossary_terms.json`` (read as candidate-only entries); else the
    tracked example. A broken operator file falls back to the example, as the
    house rules do, rather than to nothing.
    """
    if LEXICON_FILE.exists():
        entries = _read(LEXICON_FILE, "entries")
        if entries is not None:
            return entries
    elif LEGACY_FORCED_FILE.exists():
        entries = _read(LEGACY_FORCED_FILE, "terms")
        if entries is not None:
            return entries
    if LEXICON_EXAMPLE_FILE.exists():
        return _read(LEXICON_EXAMPLE_FILE, "entries") or []
    return []


def entries_for(
    groups: set[str],
    source_text: Optional[str] = None,
    *,
    lexicon: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """The entries in scope for a book in ``groups``, present in ``source_text`` when given."""
    lexicon = load_lexicon() if lexicon is None else lexicon
    lowered = source_text.lower() if source_text is not None else None
    return [
        e for e in lexicon
        if in_scope(e["scope"], groups)
        and (lowered is None or term_occurs(e["english"], lowered))
    ]


def forced_terms() -> list[dict[str, Any]]:
    """The every-book entries as forced glossary candidates (``{term, type_guess}``).

    The shape ``scripts/extract_glossary_candidates.build_forced_candidates``
    has always read. Group-scoped entries are left out: the extractor runs
    without knowing the book, and the glossary step adds a book's group entries
    itself.
    """
    return [
        {"term": e["english"], "type_guess": e["type_guess"].upper()}
        for e in load_lexicon()
        if e["scope"] == EVERY_BOOK
    ]


def format_entry(entry: dict[str, Any]) -> str:
    """One entry as a prompt line: ``stall → casilla (also: ...) — why [scope]``."""
    line = f"{entry['english']} → {entry['spanish'] or '(no house rendering yet; decide it here)'}"
    if entry["alternatives"]:
        line += f" (also: {', '.join(entry['alternatives'])})"
    if entry["why"]:
        line += f" — {entry['why']}"
    if entry["scope"] != EVERY_BOOK:
        line += f" [{entry['scope']}]"
    return line
