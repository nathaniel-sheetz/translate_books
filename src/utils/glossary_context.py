"""Shared helpers for locating glossary-candidate occurrences in source text.

Used by both ``scripts/extract_glossary_candidates.py`` (production bootstrap
flow) and ``scripts/experimental_glossary_prompt.py``. Pure functions over
``(term, list[(label, text)])`` — no I/O, no project-layout assumptions.
"""

from __future__ import annotations

import re
from typing import Optional


_QUOTE_CHARS = "'’ʼ"  # straight, right-single, modifier-letter apostrophe


def _normalize_quotes(s: str) -> str:
    return re.sub(f"[{_QUOTE_CHARS}]", "'", s)


def _term_pattern(term: str) -> re.Pattern:
    """Build a regex for ``term``.

    * Apostrophes are normalized so straight/curly/modifier variants match.
    * Single tokens use word boundaries.
    * Multi-word terms allow any non-letter characters (commas, dashes,
      punctuation, whitespace) between tokens, so e.g. ``dictator Aulus``
      matches the text ``dictator, Aulus``.
    """
    parts = _normalize_quotes(term).split()
    escaped = [re.escape(p) for p in parts]
    if len(parts) == 1:
        return re.compile(rf"\b{escaped[0]}\b", re.IGNORECASE)
    sep = r"[^A-Za-z0-9]+"
    return re.compile(sep.join(escaped), re.IGNORECASE)


def find_first_contexts(
    term: str,
    chapter_sentences: list[tuple[str, list[str]]],
    max_contexts: int = 2,
) -> tuple[Optional[tuple[int, int]], list[tuple[str, str]]]:
    """Find up to ``max_contexts`` containing sentences across chapters.

    Returns:
      (first_position, contexts)
        first_position: (chapter_index, sentence_index) of the first hit
                        used purely for sorting; None if no hits.
        contexts: list of (chapter_label, sentence_text)
    """
    pattern = _term_pattern(term)
    contexts: list[tuple[str, str]] = []
    first_position: Optional[tuple[int, int]] = None

    for ch_idx, (label, sentences) in enumerate(chapter_sentences):
        for s_idx, sent in enumerate(sentences):
            if pattern.search(_normalize_quotes(sent)):
                if first_position is None:
                    first_position = (ch_idx, s_idx)
                contexts.append((label, sent.strip()))
                if len(contexts) >= max_contexts:
                    return first_position, contexts
    return first_position, contexts


# Token = a run of word characters (letters/digits/apostrophes) OR any single
# non-whitespace char. This lets us slice "N words before/after" while keeping
# punctuation attached visually via the original character offsets.
_TOKEN_RE = re.compile(r"\w+(?:['’ʼ]\w+)*|\S", re.UNICODE)


def find_first_word_contexts(
    term: str,
    chapter_texts: list[tuple[str, str]],
    max_contexts: int = 2,
    words_before: int = 10,
    words_after: int = 6,
) -> tuple[Optional[tuple[int, int]], list[tuple[str, str]]]:
    """Find up to ``max_contexts`` word-window fragments across chapters.

    For each match of the term, return a fragment containing roughly
    ``words_before`` word-tokens before the match and ``words_after`` after,
    using the original surrounding characters so punctuation/spacing is
    preserved.

    Returns:
      (first_position, contexts)
        first_position: (chapter_index, char_offset) of the first hit, used
                        purely for sorting; None if no hits.
        contexts: list of (chapter_label, fragment_text)
    """
    pattern = _term_pattern(term)
    contexts: list[tuple[str, str]] = []
    first_position: Optional[tuple[int, int]] = None

    for ch_idx, (label, text) in enumerate(chapter_texts):
        normalized = _normalize_quotes(text)
        # Pre-compute word-token spans so we can count "N words" away from a
        # match position quickly.
        token_spans = [m.span() for m in _TOKEN_RE.finditer(normalized)
                       if m.group().isalnum() or any(c.isalpha() for c in m.group())]

        for m in pattern.finditer(normalized):
            if first_position is None:
                first_position = (ch_idx, m.start())

            match_start, match_end = m.span()

            # Find index of first word-token whose start >= match_start
            # (i.e. the token where the match begins).
            lo, hi = 0, len(token_spans)
            while lo < hi:
                mid = (lo + hi) // 2
                if token_spans[mid][0] < match_start:
                    lo = mid + 1
                else:
                    hi = mid
            match_tok_idx = lo

            # Find last token fully ending at/before match_end.
            lo2, hi2 = 0, len(token_spans)
            while lo2 < hi2:
                mid = (lo2 + hi2) // 2
                if token_spans[mid][1] <= match_end:
                    lo2 = mid + 1
                else:
                    hi2 = mid
            last_match_tok_idx = max(match_tok_idx, lo2 - 1)

            before_idx = max(0, match_tok_idx - words_before)
            after_idx = min(len(token_spans) - 1,
                            last_match_tok_idx + words_after)

            if token_spans:
                frag_start = token_spans[before_idx][0]
                frag_end = token_spans[after_idx][1]
            else:
                frag_start, frag_end = match_start, match_end

            fragment = text[frag_start:frag_end].strip()
            # Mark elision when we clipped mid-stream.
            if frag_start > 0:
                fragment = "... " + fragment
            if frag_end < len(text):
                fragment = fragment + " ..."

            contexts.append((label, fragment))
            if len(contexts) >= max_contexts:
                return first_position, contexts
    return first_position, contexts
