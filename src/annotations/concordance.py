"""
Book-wide term search over a project's sentence alignments.

An ``inconsistency`` annotation is defined by usage *across the whole book*, so
resolving one means asking two questions the reader cannot answer from a single
sentence:

1. Where else does this Spanish word appear, and how often per chapter?
2. What *other* Spanish words render the same English source term?

Question 2 is the one that actually finds an inconsistency, and the alignments
answer it directly: search the ``en`` side for the English term and read back the
paired ``es``. A note like ``[Fantasma] The Phantom`` gets both — every use of
"Fantasma" in the translation, and every Spanish rendering the book gave to
"The Phantom".

Search is accent- and case-folded substring matching (the same
``src.utils.text_utils`` primitives the reader's "Find in book" uses), so
``ostion`` finds ``ostión``. Retrieval happens here, in Python, at prepare time —
the model reasons over the evidence, it does not go looking for it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from src.utils.text_utils import (
    _IMAGE_PLACEHOLDER_RE,
    KWIC_WORDS,
    count_folded,
    fold,
    iter_folded,
)

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\S+")


def _text_window(text: str, start: int, end: int, words: int = KWIC_WORDS) -> str:
    """A contiguous ``words``-either-side excerpt of ``text`` around ``[start, end)``.

    ``text_utils.kwic_window`` splits the match out and re-joins on whitespace so
    the reader can highlight it by offset; that renders ``relámpago , sigue``.
    Here the snippet is read by a model as evidence about spelling and
    collocation, so it must stay a verbatim slice of the sentence.
    """
    left_starts = [m.start() for m in _TOKEN_RE.finditer(text[:start])]
    right_ends = [end + m.end() for m in _TOKEN_RE.finditer(text[end:])]

    lo = left_starts[-words] if len(left_starts) > words else 0
    hi = right_ends[words - 1] if len(right_ends) >= words else len(text)

    snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
    if lo > 0:
        snippet = "… " + snippet
    if hi < len(text):
        snippet = snippet + " …"
    return snippet

# Shorter queries are function words in every language this targets ("en", "de",
# "a"), so they carry no signal about a translation choice.
MIN_TERM_LEN = 3

# A note's free text is only a search term when it is short enough to *be* one.
# Longer hints are the reader's prose commentary ("error? ...comiencen a crecer
# y algún día se transformen en hormigas."), and searching them finds nothing.
MAX_TERM_WORDS = 4

# Hits quoted per term. The per-chapter distribution is always complete — this
# caps only the KWIC lines, so a term used 300 times still reports 300.
DEFAULT_MAX_HITS = 25

# Above this, a term is part of the book's ordinary vocabulary rather than a
# translation choice worth auditing. Counts are still reported (the model should
# know it is everywhere) but no lines are quoted and the distribution is
# summarized — otherwise one common word crowds out the real evidence.
TOO_COMMON_TOTAL = 150

# Chapters named in a distribution line before it is summarized.
MAX_CHAPTERS_IN_DIST = 12


def is_searchable_term(term: str) -> bool:
    """True when a string is specific enough to be worth a book-wide search."""
    cleaned = (term or "").strip()
    if len(cleaned) < MIN_TERM_LEN:
        return False
    return len(cleaned.split()) <= MAX_TERM_WORDS


class BookIndex:
    """Every aligned sentence in a book, pre-folded for repeated term search.

    Folding is the expensive part (a Python char loop), so each sentence is
    folded once at construction and reused across every term a run looks up.
    Offset-accurate ``fold_with_map`` work is deferred to the handful of rows
    that actually become quoted hits.
    """

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        align_dir = self.project_dir / "alignments"
        if not align_dir.exists():
            return
        for path in sorted(align_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("skipping unreadable alignment %s: %s", path, exc)
                continue
            chapter_id = data.get("chapter_id") or path.stem
            for a in data.get("alignments") or []:
                es = a.get("es") or ""
                en = a.get("en") or ""
                # [IMAGE:...] placeholder rows are not readable text.
                if not es.strip() or _IMAGE_PLACEHOLDER_RE.fullmatch(es.strip()):
                    continue
                self.rows.append(
                    {
                        "chapter_id": chapter_id,
                        "es_idx": a.get("es_idx"),
                        "chunk_id": a.get("chunk_id"),
                        "es": es,
                        "en": en,
                        "_es_folded": fold(es),
                        "_en_folded": fold(en),
                    }
                )

    def __len__(self) -> int:
        return len(self.rows)

    def search(
        self,
        term: str,
        *,
        side: str = "es",
        max_hits: int = DEFAULT_MAX_HITS,
        skip: Optional[tuple] = None,
    ) -> dict[str, Any]:
        """Find ``term`` across the book on the ``es`` or ``en`` side.

        Args:
            term: Raw (unfolded) search term.
            side: ``"es"`` to search the translation, ``"en"`` the source.
            max_hits: Maximum quoted KWIC hits; counts are never capped.
            skip: ``(chapter_id, es_idx)`` to exclude — the annotated sentence
                itself, which is context the prompt already carries.

        Returns:
            ``{term, side, total, chapters, by_chapter, hits, truncated}``.
            Each hit carries ``chapter_id``, ``es_idx``, ``snippet`` and — when
            searching ``en`` — the paired ``es``, which is what exposes a
            competing rendering.
        """
        if not is_searchable_term(term):
            return {
                "term": term,
                "side": side,
                "total": 0,
                "chapters": 0,
                "by_chapter": {},
                "hits": [],
                "truncated": False,
                "too_common": False,
                "skipped_short": True,
            }
        folded_term = fold(term)

        folded_key = "_es_folded" if side == "es" else "_en_folded"
        text_key = "es" if side == "es" else "en"

        total = 0
        by_chapter: dict[str, int] = {}
        matched: list[dict[str, Any]] = []

        for row in self.rows:
            n = count_folded(row[folded_key], folded_term, whole_word=True)
            if not n:
                continue
            if skip is not None and (row["chapter_id"], row["es_idx"]) == skip:
                continue
            total += n
            by_chapter[row["chapter_id"]] = by_chapter.get(row["chapter_id"], 0) + n
            if len(matched) < max_hits:
                matched.append(row)

        # A word the book uses everywhere says nothing about this annotation.
        # Report the scale and stop — quoting 25 arbitrary lines of it would
        # crowd out the evidence that does matter.
        too_common = total > TOO_COMMON_TOTAL

        hits: list[dict[str, Any]] = []
        if not too_common:
            for row in matched:
                text = row[text_key]
                span = next(iter_folded(text, folded_term, whole_word=True), None)
                if span is None:
                    # Counted in folded space but the offset walk disagreed: only
                    # reachable via exotic casefold expansions. Quote the whole
                    # sentence rather than drop the evidence.
                    snippet = re.sub(r"\s+", " ", text).strip()
                else:
                    snippet = _text_window(text, span[0], span[1])
                hit = {
                    "chapter_id": row["chapter_id"],
                    "es_idx": row["es_idx"],
                    "snippet": snippet,
                }
                if side == "en":
                    hit["es"] = row["es"]
                hits.append(hit)

        return {
            "term": term,
            "side": side,
            "total": total,
            "chapters": len(by_chapter),
            "by_chapter": by_chapter,
            "hits": hits,
            "truncated": total > len(hits),
            "too_common": too_common,
        }


def search_terms(
    index: BookIndex,
    terms: Iterable[str],
    *,
    sides: Iterable[str] = ("es",),
    max_hits: int = DEFAULT_MAX_HITS,
    skip: Optional[tuple] = None,
) -> list[dict[str, Any]]:
    """Run several terms over both sides, dropping empty and duplicate queries."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for term in terms:
        cleaned = (term or "").strip()
        if not cleaned:
            continue
        for side in sides:
            key = (fold(cleaned), side)
            if key in seen:
                continue
            seen.add(key)
            result = index.search(cleaned, side=side, max_hits=max_hits, skip=skip)
            if result.get("skipped_short") or result["total"] == 0:
                continue
            out.append(result)
    return out


def format_concordance(results: list[dict[str, Any]]) -> str:
    """Render concordance results as the prompt's evidence block.

    Empty results render as an explicit "no occurrences" line rather than an
    empty string, so the model can tell "nothing found" from "not searched".
    """
    if not results:
        return "(no book-wide occurrences found for this annotation's terms)"

    blocks: list[str] = []
    for res in results:
        side_label = "translation" if res["side"] == "es" else "source"
        lines = [
            f"TERM {res['term']!r} ({side_label} side): "
            f"{res['total']} occurrence(s) across {res['chapters']} chapter(s)"
        ]

        ordered = sorted(res["by_chapter"].items())
        if len(ordered) > MAX_CHAPTERS_IN_DIST:
            shown = ordered[:MAX_CHAPTERS_IN_DIST]
            dist = ", ".join(f"{ch}: {n}" for ch, n in shown)
            dist += f", … and {len(ordered) - MAX_CHAPTERS_IN_DIST} more chapter(s)"
        else:
            dist = ", ".join(f"{ch}: {n}" for ch, n in ordered)
        lines.append(f"  distribution: {dist}")

        if res.get("too_common"):
            lines.append(
                "  (used throughout the book — ordinary vocabulary rather than a "
                "distinctive choice; no lines quoted)"
            )
        else:
            for hit in res["hits"]:
                lines.append(f"  - [{hit['chapter_id']} #{hit['es_idx']}] {hit['snippet']}")
                if "es" in hit:
                    lines.append(f"      translated as: {hit['es']}")
            if res["truncated"]:
                lines.append(
                    f"  … {res['total'] - len(res['hits'])} further occurrence(s) not quoted"
                )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
