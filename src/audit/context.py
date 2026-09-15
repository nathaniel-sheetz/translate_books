"""Locate a reader edit in its chunk and describe the text around it.

Judged from its sentence alone, an edit is misread in one known way: a paragraph
opening with » (the Spanish mark for the same speaker's speech continuing into a
new paragraph) looks like a misplaced closing mark unless the paragraph before
it is visible. So every audit item carries:

- ``starts_paragraph``: whether the sentence opens its Spanish paragraph.
- ``context_before_en`` / ``context_before_es``: the previous paragraph when it
  does, otherwise the earlier part of its own paragraph, cut by :func:`tail` so
  the paragraph's opening words survive. Image and caption lines are skipped:
  they sit between two paragraphs of prose without being prose.
- ``context_after_en`` / ``context_after_es``: the rest of the sentence's own
  paragraph, cut by :func:`head`. A quote's speaker tag ("Maureen asked") sits
  there; without it the panel guessed the speaker and misjudged an usted.
- ``quote_continues``: read from the English, where it is unambiguous. True when
  the same speaker is still talking at this sentence: the English paragraph
  opens with a quotation mark after one that left its quotation open, or the
  Spanish starts a paragraph where the English runs on inside an open quotation.

The sentence is found by its text, never by ``es_idx``: a realign can move the
index onto a different sentence while the ledger row keeps the old one.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from src.utils.text_utils import is_caption_block

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

#: Text after the sentence is cut to its first ``CONTEXT_AFTER_CHARS``. The
#: speaker tag that follows a quote comes first.
CONTEXT_AFTER_CHARS = 200

#: Image paragraphs in a chunk start with this.
IMAGE_PREFIX = "[IMAGE:"

#: An unmarked English caption: at most this many words, and no punctuation.
BARE_CAPTION_WORDS = 8

#: Context for a row whose chunk could not be read.
NO_CONTEXT: dict[str, Any] = {
    "starts_paragraph": False,
    "quote_continues": None,
    "context_before_en": "",
    "context_before_es": "",
    "context_after_en": "",
    "context_after_es": "",
    "en_found": False,
    "es_found": False,
}

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_PUNCTUATION = re.compile(r"[.,;:!?¡¿“”\"«»—]")


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


def head(text: str, limit: int = CONTEXT_AFTER_CHARS) -> str:
    """``text`` cut at a word break to about its first ``limit`` characters."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip() + " …"


def quote_left_open(text: str) -> bool:
    """Whether English text ends inside a quotation.

    English leaves a quotation unclosed at a paragraph break when the same
    speaker carries on into the next paragraph. Curly quotes show it directly;
    straight quotes can only be counted, and an odd count leaves one open.
    """
    return text.count("“") > text.count("”") or text.count('"') % 2 == 1


def is_layout(paragraph: str, *, bare_captions: bool = False) -> bool:
    """Whether a paragraph is an image or a caption rather than prose.

    Spanish captions carry the ``[CAPTION]`` marker, but many English sources
    leave theirs unmarked ("Stratus"). ``bare_captions`` also counts a short
    line with no punctuation. Pass it only where the Spanish showed a caption
    or image at that spot, or a heading would count too.
    """
    text = paragraph.strip()
    if text.startswith(IMAGE_PREFIX) or is_caption_block(text):
        return True
    return bare_captions and len(text.split()) <= BARE_CAPTION_WORDS and not _PUNCTUATION.search(text)


#: Quote marks a sentence can open with, directly before its first word.
_OPENING_QUOTES = "“\"'‘"


def _rest_of_paragraph(para: str, j: int, line: str, sentence: str) -> str:
    """What follows the sentence found at ``j``, past its own closing marks.

    Empty when the sentence ends its paragraph, or when the paragraph does not
    hold the sentence's whole first line (the key matched only its opening).
    """
    if not para.startswith(sentence, j):
        return ""
    rest = para[j + len(sentence):]
    closing = line[len(line.rstrip(MARKS)):].strip()
    if closing and rest.startswith(closing):
        rest = rest[len(closing):]
    return head(rest.strip())


def locate(
    text: Optional[str],
    candidates: Iterable[Optional[str]],
    *,
    bare_captions: bool = False,
) -> Optional[dict[str, Any]]:
    """Find the first candidate sentence in ``text`` and describe what precedes it.

    Returns ``None`` when no candidate is found, otherwise:

    - ``starts_paragraph`` and ``before``, as the module docstring describes.
    - ``after``: the rest of the sentence's paragraph (:func:`_rest_of_paragraph`).
    - ``quote_continues``: ``None`` when nothing but images and captions
      precede the paragraph in ``text``, since the previous prose is in
      another chunk.
    - ``open_before``: whether a quotation is open where the sentence starts
      inside its paragraph, not counting the sentence's own opening quote.
    - ``skipped``: image and caption paragraphs passed over to reach the
      previous paragraph.
    """
    paras = paragraphs(text)
    for candidate in candidates:
        # Only the first line: an edit that split a paragraph carries the break,
        # and no single paragraph of the chunk contains text across it.
        line = (candidate or "").strip().split("\n", 1)[0].strip()
        sentence = line.strip(MARKS)
        key = sentence[:KEY_CHARS]
        if not key or (len(key) < MIN_KEY_CHARS and sum(p.count(key) for p in paras) != 1):
            continue
        for i, para in enumerate(paras):
            j = para.find(key)
            if j < 0:
                continue
            starts = not para[:j].strip(MARKS)
            k = i - 1
            while k >= 0 and is_layout(paras[k], bare_captions=bare_captions):
                k -= 1
            prev = paras[k] if k >= 0 else ""
            if not starts:
                quote_continues: Optional[bool] = False
            elif k < 0:
                quote_continues = None
            else:
                quote_continues = para.lstrip().startswith(("“", '"')) and quote_left_open(prev)
            return {
                "starts_paragraph": starts,
                "before": tail(prev if starts else para[:j].rstrip()),
                "after": _rest_of_paragraph(para, j, line, sentence),
                "quote_continues": quote_continues,
                # The key starts past the sentence's own opening quote. Counted,
                # that quote made every new turn read as an open quotation.
                "open_before": quote_left_open(para[:j].rstrip(_OPENING_QUOTES)),
                "skipped": i - 1 - k,
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
    en = locate(
        chunk.get("source_text"),
        [row.get("en")],
        bare_captions=bool(es and es["starts_paragraph"] and es["skipped"]),
    )
    quote_continues = en["quote_continues"] if en else None
    if es and en and es["starts_paragraph"] and not en["starts_paragraph"]:
        # The Spanish opened a paragraph where the English runs on. The same
        # speaker is still talking exactly when the English quotation is open here.
        quote_continues = en["open_before"]
    return {
        "starts_paragraph": bool(es and es["starts_paragraph"]),
        "quote_continues": quote_continues,
        "context_before_en": en["before"] if en else "",
        "context_before_es": es["before"] if es else "",
        "context_after_en": en["after"] if en else "",
        "context_after_es": es["after"] if es else "",
        "en_found": en is not None,
        "es_found": es is not None,
    }
