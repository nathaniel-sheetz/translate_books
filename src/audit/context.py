"""Locate a reader edit in its chunk and describe the text around it.

Judged from its sentence alone, an edit is misread in one known way: a paragraph
opening with » (the Spanish mark for the same speaker's speech continuing into a
new paragraph) looks like a misplaced closing mark unless the paragraph before
it is visible. So every audit item carries:

- ``starts_paragraph``: whether the sentence opens its Spanish paragraph.
- ``context_before_en`` / ``context_before_es``: the previous paragraph when it
  does, otherwise the earlier part of its own paragraph, cut by :func:`tail` so
  the paragraph's opening words survive.
- ``quote_continues``: read from the English, where it is unambiguous. True when
  the paragraph opens with a quotation mark and the previous paragraph left its
  quotation open, which means the same speaker is still talking.

The sentence is found by its text, never by ``es_idx``: a realign can move the
index onto a different sentence while the ledger row keeps the old one.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: Stripped from both ends of a sentence before it becomes a search key, so a
#: raya or guillemet the edit added or removed does not stop the match.
MARKS = "»«—–-“”\"' "

#: A search key is the first ``KEY_CHARS`` characters of the sentence's first
#: line. A key shorter than ``MIN_KEY_CHARS`` could match anywhere, so it is
#: trusted only when it occurs exactly once in the text.
KEY_CHARS = 80
MIN_KEY_CHARS = 12

#: Context is cut to about ``CONTEXT_CHARS``, keeping the first ``CONTEXT_HEAD_CHARS``.
CONTEXT_CHARS = 400
CONTEXT_HEAD_CHARS = 80

#: Context for a row whose chunk could not be read.
NO_CONTEXT: dict[str, Any] = {
    "starts_paragraph": False,
    "quote_continues": None,
    "context_before_en": "",
    "context_before_es": "",
    "en_found": False,
    "es_found": False,
}

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def paragraphs(text: Optional[str]) -> list[str]:
    """Paragraphs split at blank lines, including lines holding only whitespace."""
    return [p.strip() for p in _PARAGRAPH_BREAK.split(text or "") if p.strip()]


def tail(text: str, limit: int = CONTEXT_CHARS, head: int = CONTEXT_HEAD_CHARS) -> str:
    """``text`` cut to about ``limit`` characters, keeping its first ``head``.

    The end is what leads into the edited sentence, but the opening is what the
    continuation rule reads: cut to its last 400 characters, a paragraph loses
    its leading » and reads as narration.
    """
    if len(text) <= limit:
        return text
    return text[:head].rstrip() + " … " + text[-(limit - head):].lstrip()


def quote_left_open(paragraph: str) -> bool:
    """Whether an English paragraph ends inside a quotation.

    English leaves a quotation unclosed at a paragraph break when the same
    speaker carries on into the next paragraph. Curly quotes show it directly;
    straight quotes can only be counted, and an odd count leaves one open.
    """
    return paragraph.count("“") > paragraph.count("”") or paragraph.count('"') % 2 == 1


def locate(text: Optional[str], candidates: Iterable[Optional[str]]) -> Optional[dict[str, Any]]:
    """Find the first candidate sentence in ``text`` and describe what precedes it.

    Returns ``{starts_paragraph, before, quote_continues}``, or ``None`` when no
    candidate is found. ``quote_continues`` is ``None`` when the paragraph opens
    ``text``, since the previous paragraph is in another chunk.
    """
    paras = paragraphs(text)
    for candidate in candidates:
        # Only the first line: an edit that split a paragraph carries the break,
        # and no single paragraph of the chunk contains text across it.
        key = (candidate or "").strip().split("\n", 1)[0].strip(MARKS)[:KEY_CHARS]
        if not key or (len(key) < MIN_KEY_CHARS and sum(p.count(key) for p in paras) != 1):
            continue
        for i, para in enumerate(paras):
            j = para.find(key)
            if j < 0:
                continue
            starts = not para[:j].strip(MARKS)
            prev = paras[i - 1] if i > 0 else ""
            if not starts:
                quote_continues: Optional[bool] = False
            elif i == 0:
                quote_continues = None
            else:
                quote_continues = para.lstrip().startswith(("“", '"')) and quote_left_open(prev)
            return {
                "starts_paragraph": starts,
                "before": tail(prev if starts else para[:j].rstrip()),
                "quote_continues": quote_continues,
            }
    return None


def edit_context(chunk: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The context fields for one export row, from its chunk.

    The Spanish is searched in the chunk's current text, which carries the
    reader's edit once it has landed, so ``es_after`` is tried before
    ``es_before``. ``starts_paragraph`` comes from the Spanish and
    ``quote_continues`` from the English.
    """
    es = locate(chunk.get("translated_text"), [row.get("es_after"), row.get("es_before")])
    en = locate(chunk.get("source_text"), [row.get("en")])
    return {
        "starts_paragraph": bool(es and es["starts_paragraph"]),
        "quote_continues": en["quote_continues"] if en else None,
        "context_before_en": en["before"] if en else "",
        "context_before_es": es["before"] if es else "",
        "en_found": en is not None,
        "es_found": es is not None,
    }
