"""
Parse the ``[bracketed]`` anchor convention out of annotation content.

The reader seeds a new note with the tapped word in brackets
(``reader_sheet_v2.js``: ``'[' + tappedWord + '] '``), so a note reads
``[muserón] ¿muserola?``. For ``footnote`` annotations that bracket is load-bearing
— ``src/endnotes.py`` matches it verbatim against the Spanish sentence to place the
superscript marker, and strips it from the published endnote text. For the other
three types nothing downstream parses it; it is a human-readable pointer at the
words the note is about.

Real books show four shapes this module has to survive:

- front bracket — ``[muserón] ¿muserola?`` (what the reader seeds today)
- trailing bracket — ``"la ficción es irrelevante" [nadería;]`` (rows migrated by
  ``scripts/migrate_annotations.py``, which appended ``[word]``)
- several brackets — ``[Neuve-Celle,]; [Esaú,]; [Montélimar.]`` (one note standing
  in for three separate glosses)
- no bracket at all — ``humilde``, ``biblia``, or empty

Anchors keep their inner text verbatim, punctuation included, because that is what
``endnotes.py`` searches for.
"""

from __future__ import annotations

import re
from typing import List

# Every [...] token, in document order. Matches src/endnotes.py:_BRACKET_RE, which
# takes only the first one.
_BRACKET_RE = re.compile(r"\[([^\]]*)\]")


def parse_anchors(content: str) -> List[str]:
    """Return the verbatim text of every ``[...]`` token, in order.

    Empty brackets are dropped; surrounding whitespace is stripped but inner
    punctuation is preserved (``[by then,]`` -> ``by then,``).

    Example:
        >>> parse_anchors("[Neuve-Celle,]; [Esaú,]; [Montélimar.]")
        ['Neuve-Celle,', 'Esaú,', 'Montélimar.']
        >>> parse_anchors("humilde")
        []
    """
    if not content:
        return []
    return [m.group(1).strip() for m in _BRACKET_RE.finditer(content) if m.group(1).strip()]


def bare_hint(content: str) -> str:
    """Return the note text with every ``[...]`` token removed, whitespace collapsed.

    This is the part the user actually typed — a question, an instruction, a
    proposed replacement, or nothing. Empty when the note is only an anchor.

    Example:
        >>> bare_hint("[muserón] ¿muserola?")
        '¿muserola?'
        >>> bare_hint("[Sancerre]")
        ''
        >>> bare_hint("humilde")
        'humilde'
    """
    if not content:
        return ""
    return re.sub(r"\s+", " ", _BRACKET_RE.sub(" ", content)).strip()


def is_effectively_blank(content: str) -> bool:
    """True when the note carries no instruction beyond its anchor(s).

    Both ``""`` and ``"[Sancerre]"`` are blank in this sense: the user marked a
    span and left the reasoning implicit.
    """
    return not bare_hint(content)
