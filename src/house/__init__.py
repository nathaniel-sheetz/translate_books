"""
The house layer: what holds across books rather than inside one.

Four kinds of house knowledge, each with its own cheapest route to the models
(``docs/design/decisions-stage-plan.md``, "House rules: decisions taken"):

- **mechanical** rules (rio, never rió) are fixed at chunk commit and logged,
  and reach no prompt (:mod:`src.house.mechanical`);
- **lexical** entries (deer → venado; stall → casilla in the horse books) live
  in the house lexicon and reach the translator through the book glossary
  (:mod:`src.house.lexicon`);
- **triggered** rules (Old + surname) reach a chunk's prompt only when the
  chunk's English can trigger them; **global** rules sit in the translator's
  cached prefix. Both are house rules, loaded in :mod:`src.judges.context`.

Books can be grouped (:mod:`src.house.groups`), and a book's siblings in a group
contribute precedent (:mod:`src.house.precedent`): derived, never authored.

Every file a user authors here is per-user and gitignored, with a tracked
``.example`` twin that a fresh clone reads instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
PROJECTS_DIR = REPO_ROOT / "projects"

#: The scope a house rule or lexicon entry has when it names none.
EVERY_BOOK = "every_book"


def project_dir_for(slug: Optional[str]) -> Optional[Path]:
    """The book's folder, else ``None``.

    ``projects/<slug>``, or a book nested in a grouping folder
    (``projects/.macdonald/<slug>``) found the way the harness finds it. A slug
    is a single folder name: anything carrying a path separator is refused.
    """
    if not slug or slug in (".", "..") or Path(slug).name != slug:
        return None
    path = PROJECTS_DIR / slug
    if path.is_dir():
        return path
    from src.harness.state import _iter_nested_match

    return next(_iter_nested_match(PROJECTS_DIR, slug), None)


def book_slug(project_dir: Optional[Path]) -> Optional[str]:
    return Path(project_dir).name if project_dir else None


def in_scope(scope: Optional[str], groups: set[str]) -> bool:
    """Whether a rule or entry with ``scope`` applies to a book in ``groups``.

    ``every_book`` (or no scope) applies everywhere; ``group:<id>`` only to a
    book that belongs to that group. An unknown scope applies nowhere, so a
    typo narrows a rule rather than widening it.
    """
    scope = (scope or EVERY_BOOK).strip()
    if scope == EVERY_BOOK:
        return True
    if scope.startswith("group:"):
        return scope[len("group:"):].strip() in groups
    return False


def read_source(project_dir: Optional[Path]) -> Optional[str]:
    """The book's English source text, or ``None`` when there is none to read."""
    if project_dir is None:
        return None
    path = Path(project_dir) / "source.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_body(project_dir: Optional[Path]) -> Optional[str]:
    """The book's chapters without the front matter, else ``source.txt``.

    For "does this book use the word": a copyright page's "Made in the United
    States of America" is not a use of "United States".
    """
    if project_dir is None:
        return None
    from src.utils.source_text import load_clean_source_text

    text, _, _ = load_clean_source_text(Path(project_dir))
    return text or read_source(project_dir)
