"""
Block-level verse detection for preserving poetry line breaks downstream.

The pipeline stores verse with single `\\n` between lines and `\\n\\n`
between stanzas, both in source and in translated text. Renderers
(EPUB builder, web reader) need a cheap, per-block heuristic to decide
whether a `\\n\\n`-delimited block is a stanza so they can emit visible
line breaks instead of collapsing them to whitespace.

This is intentionally separate from `text_feature_detector.detect_verse`,
which scans the whole book and is tuned to minimize false positives at
corpus scale. Per-block decisions can afford a more permissive bar -- a
false-positive stanza only causes slightly tighter typography, while a
false-negative produces visibly broken poetry.

The dominant signal is average line length: prose paragraphs that
contain literal `\\n` after the source loader are vanishingly rare,
and prose lines (when soft-wrapped) routinely average >70 chars. Verse
lines in this corpus average 25-45 chars even in translation. Terminal
punctuation is intentionally NOT used as a signal: many quatrains end
each line with a period, and other short-line content (lists, address
blocks, captions) also benefits from visible line-break preservation.
"""


def is_verse_block(block: str) -> bool:
    """
    Decide whether a single `\\n\\n`-delimited text block is verse/stanza.

    Heuristic (all must hold):
      - block contains at least one '\\n'
      - >= 2 non-empty lines after splitting on '\\n'
      - average non-empty-line length <= 65 chars
      - at least one line has 2-12 words (filters labels / dense junk)
    """
    if not block or "\n" not in block:
        return False

    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    if len(lines) < 2:
        return False

    avg_len = sum(len(ln) for ln in lines) / len(lines)
    if avg_len > 65:
        return False

    for ln in lines:
        words = ln.split()
        if 2 <= len(words) <= 12:
            return True

    return False
