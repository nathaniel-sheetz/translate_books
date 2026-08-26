"""English-neighborhood retrieval for a Spanish excerpt.

The editorial judge reads Spanish alone and nominates the findings it cannot
settle without the original. This module answers those nominations: given the
Spanish the judge quoted, what did the English say around there?

No new alignment work is involved. ``alignments/<chapter>.json`` already pairs
every Spanish sentence with its English one — ``es_idx``, ``en_idx``, both texts,
a similarity and a confidence, produced by ``src/sentence_aligner.py`` and
refreshed by every realign. Across the 502 alignment files in ``projects/`` that
is 47,091 high-confidence rows against 1,076 low, with two coverage gaps in the
whole corpus. So retrieval is a lookup, not a second alignment problem: locate
the rows the excerpt covers, widen by a few rows either side, and read off the
English that is already sitting on them.

Matching is done in folded space (``src/utils/text_utils.fold``) because a judge
quoting from prose reproduces the letters far more reliably than the accents, and
because the excerpt may span a sentence boundary — so a row matches when either
string contains the other.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.utils.text_utils import fold

logger = logging.getLogger(__name__)

#: Rows of context either side of the matched span. Three sentences is roughly a
#: short paragraph on each side — enough to settle a wrong connector or a missing
#: clause without pasting the chapter into the prompt.
DEFAULT_WINDOW = 3

#: Widening applied when the matched row's own alignment is low-confidence. That
#: row's English pairing is the thing in doubt, so the fix is to show more of the
#: neighbourhood rather than to trust the single pairing harder.
LOW_CONFIDENCE_EXTRA = 2

#: Shortest excerpt worth matching. Below this, a folded substring search finds
#: something almost everywhere and the "neighbourhood" it returns is noise.
MIN_MATCH_CHARS = 12

#: Hard ceiling on window size. The contiguous-run rule in :func:`_match_rows`
#: is what keeps windows honest; this is the backstop for a genuinely long
#: excerpt, so a single finding can never paste most of a chapter into the
#: verification prompt.
MAX_WINDOW_ROWS = 30

#: Minimum length for a token to count towards the fallback overlap score, so
#: articles and prepositions do not decide which row a fallback lands on.
_MIN_TOKEN_CHARS = 4


@dataclass
class EnglishWindow:
    """The English around a Spanish excerpt, plus how it was found.

    ``matched`` is False when no row could be located. The window is then empty
    and the caller should say so in the prompt rather than silently present an
    unrelated passage as the source — a verifier shown the wrong English is worse
    than one shown none, because it will confidently adjudicate against it.
    """

    matched: bool = False
    method: str = "none"  # "contains" | "overlap" | "none"
    confidence: Optional[str] = None
    es_idx_start: Optional[int] = None
    es_idx_end: Optional[int] = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def english_text(self) -> str:
        """The window's English sentences, in order, space-joined."""
        return " ".join(
            str(row.get("en") or "").strip()
            for row in self.rows
            if str(row.get("en") or "").strip()
        )

    def spanish_text(self) -> str:
        """The window's Spanish sentences, in order, space-joined."""
        return " ".join(
            str(row.get("es") or "").strip()
            for row in self.rows
            if str(row.get("es") or "").strip()
        )


def load_alignment_rows(
    project_dir: Path, chapter_id: str, chunk_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """Alignment rows for a chapter, in ``es_idx`` order.

    Restricting to ``chunk_id`` is what keeps a repeated line ("—Sí —dijo.")
    from matching in the wrong part of the chapter: the judge always names the
    chunk it was given, so the search space is the chunk, not the book.
    """
    path = Path(project_dir) / "alignments" / f"{chapter_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No usable alignment at %s: %s", path, exc)
        return []

    rows = [r for r in (data.get("alignments") or []) if isinstance(r, dict)]
    if chunk_id:
        scoped = [r for r in rows if r.get("chunk_id") == chunk_id]
        # A chapter aligned before chunk_id was stamped on its rows has none to
        # scope by; fall back to the chapter rather than returning nothing.
        if scoped:
            return scoped
    return rows


def _significant_tokens(folded: str) -> set[str]:
    return {t for t in folded.split() if len(t) >= _MIN_TOKEN_CHARS}


def _contiguous_runs(indices: list[int]) -> list[list[int]]:
    """Split sorted ``indices`` into runs of consecutive values."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _match_rows(rows: list[dict[str, Any]], excerpt: str) -> tuple[list[int], str]:
    """Indices into ``rows`` that the excerpt covers, and how they were found.

    Primary test is containment in folded space, in both directions: a short
    excerpt sits inside one sentence, a long one swallows several.

    Containment hits are then reduced to a single **contiguous run**, scored by
    how much of the excerpt each run accounts for. Without that, one short
    sentence that happens to appear inside the excerpt anchors the window from
    somewhere else in the chunk: a real case in ``gaudenzia`` matched the
    12-character row "El Paragüero" at index 2 alongside the true span at
    176-177, and min-to-max spanned 181 rows — a whole chapter presented to the
    verifier as the neighbourhood of one line.

    Failing containment, a single best row by significant-token overlap, which
    catches the case where the judge silently normalized a dash or an ellipsis
    while quoting.
    """
    folded_excerpt = fold(excerpt).strip()
    if len(folded_excerpt) < MIN_MATCH_CHARS:
        return [], "none"

    folded_rows = [fold(str(row.get("es") or "")).strip() for row in rows]
    hits = [
        i
        for i, folded_row in enumerate(folded_rows)
        if folded_row
        and (folded_row in folded_excerpt or folded_excerpt in folded_row)
    ]
    if hits:
        best = max(
            _contiguous_runs(hits),
            key=lambda run: sum(len(folded_rows[i]) for i in run),
        )
        return best, "contains"

    excerpt_tokens = _significant_tokens(folded_excerpt)
    if not excerpt_tokens:
        return [], "none"

    best_index, best_score = -1, 0
    for i, row in enumerate(rows):
        score = len(excerpt_tokens & _significant_tokens(fold(str(row.get("es") or ""))))
        if score > best_score:
            best_index, best_score = i, score

    # Half the excerpt's significant tokens must land on one sentence. Below
    # that the "match" is incidental vocabulary and a wrong window is worse than
    # no window.
    if best_index >= 0 and best_score * 2 >= len(excerpt_tokens):
        return [best_index], "overlap"
    return [], "none"


def english_window(
    project_dir: Path,
    chapter_id: str,
    excerpt: str,
    *,
    chunk_id: Optional[str] = None,
    window: int = DEFAULT_WINDOW,
    rows: Optional[list[dict[str, Any]]] = None,
) -> EnglishWindow:
    """Find the English around ``excerpt``.

    Args:
        project_dir: The book's project directory.
        chapter_id: Chapter the excerpt belongs to.
        excerpt: Verbatim Spanish quoted by the judge.
        chunk_id: Restrict the search to one chunk's rows when known.
        window: Rows of context either side of the match.
        rows: Pre-loaded alignment rows, so a caller adjudicating many findings
            in one chapter reads the file once.

    Returns:
        An :class:`EnglishWindow`; ``matched`` is False when nothing was found.
    """
    if rows is None:
        rows = load_alignment_rows(project_dir, chapter_id, chunk_id)
    if not rows:
        return EnglishWindow()

    hits, method = _match_rows(rows, excerpt)
    if not hits:
        return EnglishWindow()

    confidence = str(rows[hits[0]].get("confidence") or "") or None
    if confidence == "low":
        window += LOW_CONFIDENCE_EXTRA

    start = max(0, min(hits) - window)
    end = min(len(rows), max(hits) + window + 1)
    if end - start > MAX_WINDOW_ROWS:
        end = start + MAX_WINDOW_ROWS
    selected = rows[start:end]

    return EnglishWindow(
        matched=True,
        method=method,
        confidence=confidence,
        es_idx_start=selected[0].get("es_idx"),
        es_idx_end=selected[-1].get("es_idx"),
        rows=selected,
    )


__all__ = [
    "DEFAULT_WINDOW",
    "EnglishWindow",
    "english_window",
    "load_alignment_rows",
]
