"""
Backfill of ``[QUOTE]`` markers into a book's ``source.txt`` before translation.

New Gutenberg and EPUB books get their markers at ingest
(``scripts/ingest_gutenberg.py`` reads ``p.poem``, ``div.poetry``,
``div.blockquot``, ``<blockquote>`` and kin). Two cases ingest cannot reach:

- A book seeded from plain text (corazon-de-gigante, The Giant's Heart): its
  stanzas survive only as blocks indented four spaces, their line breaks gone.
- A set-off passage the HTML does not mark (the-light-princess's prose
  inscription, an unclassed ``<p>``): only a person can point at it.

So candidates come from two sources: indented blocks (``indented``), and
blocks containing a phrase the user names (``match``). Nothing is accepted
without being listed first; there is no automatic tier.

Pre-translation only. The markers go into ``source.txt`` alone, and the user
re-runs ``setup`` so the chapters and chunks are regenerated from it. A book
with any translated chunk is refused: ``setup`` would discard the translation,
and marking only the English of a translated pair breaks quote parity. (For a
finished book, mark the passage in both sides of the chunk by hand.)
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from src.captions import iter_blocks
from src.utils.text_utils import QUOTE_MARKER, fold, is_caption_block, is_quote_block

_IMAGE_RE = re.compile(r"^\[IMAGE:[^\]]+\]$")
_HR_RE = re.compile(r"^-{3,}$")
#: An indented block: its first line opens with 4+ spaces or a tab.
_INDENT_RE = re.compile(r"^(?: {4,}|\t)")

BACKUP_SUFFIX = ".bak-quotes"


@dataclass
class Candidate:
    """One source block that may be a quoted passage."""

    index: int          # 1-based, stable for an unchanged source.txt and match list
    start: int          # offset of the block's first character in source.txt
    reason: str         # "indented" | "match: <phrase>"
    text: str           # the block, stripped
    passage: int        # 1-based; consecutive candidates share a passage

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "reason": self.reason,
            "passage": self.passage,
            "text": self.text,
        }


def _raw_blocks(text: str):
    """Yield ``(start, end, raw_first_line, stripped_block)`` for each block.

    ``iter_blocks`` reports offsets past leading whitespace, which is exactly
    what loses the indentation; the raw first line is recovered from the
    line start.
    """
    for start, end, block in iter_blocks(text):
        line_start = text.rfind("\n", 0, start) + 1
        yield line_start, end, text[line_start:start] + block.split("\n", 1)[0], block


_HEADING_PATTERNS: List[re.Pattern] | None = None


def _heading_like(block: str) -> bool:
    """A one-line block that reads as a heading, not a quotation.

    Plain-text books often centre headings with leading spaces, so an indented
    ``CHAPTER I`` looks like a stanza. Marking it would hide it from the chapter
    regex and the chapter would vanish on the setup re-run. Heading-like: any
    named split pattern matches the line, or the line is short and has no
    lowercase letter.
    """
    global _HEADING_PATTERNS
    if "\n" in block:
        return False
    line = block.strip()
    if _HEADING_PATTERNS is None:
        from src.book_splitter import get_chapter_pattern, get_pattern_names
        pats: List[re.Pattern] = []
        try:
            for name in get_pattern_names():
                try:
                    pats.append(get_chapter_pattern(name))
                except ValueError:
                    continue
        except (OSError, ValueError, KeyError):
            pass
        _HEADING_PATTERNS = pats
    if any(p.match(line) for p in _HEADING_PATTERNS):
        return True
    return len(line.split()) <= 8 and not any(ch.islower() for ch in line)


def _eligible(block: str) -> bool:
    return bool(block) and not (
        is_quote_block(block) or is_caption_block(block)
        or _IMAGE_RE.match(block) or _HR_RE.match(block)
    )


def scan_text(text: str, matches: Iterable[str] = ()) -> List[Candidate]:
    """Return quote candidates in *text*, in document order."""
    needles = [(m, fold(m)) for m in matches if m and m.strip()]
    found: List[Candidate] = []
    prev_block_i = None
    passage = 0
    for block_i, (start, end, first_line, block) in enumerate(_raw_blocks(text)):
        if not _eligible(block):
            continue
        reason = ""
        # A named --match is the user pointing at the block; only the indent
        # tier needs the heading guard.
        if _INDENT_RE.match(first_line) and not _heading_like(block):
            reason = "indented"
        else:
            folded = fold(block)
            for raw, needle in needles:
                if needle in folded:
                    reason = f"match: {raw}"
                    break
        if not reason:
            continue
        if prev_block_i is None or block_i != prev_block_i + 1:
            passage += 1
        prev_block_i = block_i
        found.append(Candidate(len(found) + 1, start, reason, block, passage))
    return found


def scan_project(project_dir: Path, matches: Iterable[str] = ()) -> List[Candidate]:
    source = Path(project_dir) / "source.txt"
    if not source.exists():
        return []
    return scan_text(source.read_text(encoding="utf-8"), matches)


def translated_chunks(project_dir: Path) -> List[str]:
    """Ids of chunks that already carry a translation (the refusal signal)."""
    out = []
    chunks_dir = Path(project_dir) / "chunks"
    if not chunks_dir.exists():
        return out
    for path in sorted(chunks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (data.get("translated_text") or "").strip():
            out.append(path.stem)
    return out


def mark_text(text: str, selected: Iterable[Candidate]) -> str:
    """Return *text* with ``[QUOTE] `` opening each selected block.

    The block's leading indentation is replaced by the marker (the indent was
    the only sign it was quoted; the marker now carries that). Applied back to
    front so earlier offsets stay valid.
    """
    out = text
    for c in sorted(selected, key=lambda c: c.start, reverse=True):
        line_end = out.find("\n", c.start)
        line_end = len(out) if line_end < 0 else line_end
        first = out[c.start:line_end]
        out = out[:c.start] + f"{QUOTE_MARKER} " + first.lstrip() + out[line_end:]
    return out


def apply_marks(project_dir: Path, selected: List[Candidate]) -> dict:
    """Write the markers into ``source.txt`` (backup kept beside it)."""
    project_dir = Path(project_dir)
    done = translated_chunks(project_dir)
    if done:
        raise ValueError(
            f"{len(done)} chunk(s) already translated (first: {done[0]}); the quote "
            "backfill is pre-translation only -- re-running setup would discard "
            "them. Mark both sides of the chunk by hand instead."
        )
    source = project_dir / "source.txt"
    text = source.read_text(encoding="utf-8")
    backup = source.with_name(source.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(source, backup)
    new = mark_text(text, selected)
    source.write_text(new, encoding="utf-8")
    return {
        "marked": len(selected),
        "passages": len({c.passage for c in selected}),
        "backup": backup.name,
    }
