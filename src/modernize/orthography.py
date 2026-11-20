"""Deterministic spelling modernization for old Spanish (pre-1952/2010 RAE rules).

Only word forms that can be fixed by rule, with no reading of the sentence:

* accented one-letter words: á ó é ú → a o e u (preposition / conjunctions)
* old accented monosyllables: fué fuí dió vió → fue fui dio vio, and a short
  list of others (pié fé ví dí són tí véis)
* sólo → solo, and the accented demonstratives (éste ése aquél …) → unaccented,
  as the 2010 Ortografía recommends
* obsolete variant spellings with a single modern form: rehacio → reacio, reirte → reírte
* capitals the old type set without an accent (ARBOL, Africa) → accented, when
  the bare form is no word and exactly one accented form is

Everything grammar decides (enclitics like vióse, vosotros, laísmo, dialogue
punctuation) is left to the model. The function returns the same text when run
on its own output (idempotent), so it can run before the prompt and again at
commit.
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import Callable, Optional

# Letters for word boundaries: \w covers accented letters in Python 3 str regexes.
_W = r"[^\W\d_]"

_ONE_LETTER = {"á": "a", "Á": "A", "ó": "o", "Ó": "O", "é": "e", "É": "E", "ú": "u", "Ú": "U"}
_ONE_LETTER_RE = re.compile(rf"(?<!{_W})[áÁóÓéÉúÚ](?!{_W})")

_MONOSYLLABLES = {
    "fué": "fue", "fuí": "fui", "dió": "dio", "vió": "vio",
    "pié": "pie", "fé": "fe", "ví": "vi", "dí": "di",
    "són": "son", "tí": "ti", "véis": "veis",
    "sólo": "solo",
    "éste": "este", "ésta": "esta", "éstos": "estos", "éstas": "estas",
    "ése": "ese", "ésa": "esa", "ésos": "esos", "ésas": "esas",
    "aquél": "aquel", "aquélla": "aquella", "aquéllos": "aquellos", "aquéllas": "aquellas",
    # obsolete variant spellings with one modern form
    "rehacio": "reacio", "rehacia": "reacia", "rehacios": "reacios", "rehacias": "reacias",
    "iliada": "ilíada",
    # reír + enclitic keeps the accent (RAE 2010)
    "reirte": "reírte", "reirse": "reírse", "reirme": "reírme", "reirnos": "reírnos",
}
_MONO_RE = re.compile(
    rf"(?<!{_W})(" + "|".join(sorted(_MONOSYLLABLES, key=len, reverse=True)) + rf")(?!{_W})",
    re.IGNORECASE,
)

_WORD_RE = re.compile(rf"{_W}+")
_VOWEL_ACCENT = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}


def _match_case(src: str, repl: str) -> str:
    if src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


_ACCENTED = set("áéíóúÁÉÍÓÚ")


def _has_accent(s: str) -> bool:
    return any(c in _ACCENTED for c in s)


@lru_cache(maxsize=1)
def _default_checker() -> Optional[Callable[[str], bool]]:
    try:
        import enchant  # type: ignore
        dicts = [enchant.Dict("es_MX"), enchant.Dict("es_ES")]
    except Exception:
        return None
    return lambda w: any(d.check(w) for d in dicts)


def _accent_variants(lower: str) -> list[str]:
    out = []
    for i, ch in enumerate(lower):
        if ch in _VOWEL_ACCENT:
            out.append(lower[:i] + _VOWEL_ACCENT[ch] + lower[i + 1:])
    return out


def _capital_accent(word: str, is_word: Callable[[str], bool]) -> str:
    """ARBOL → ÁRBOL, Africa → África. Only words in capitals or with a capital
    initial that carry no accent, whose bare lowercase form is no word, and that
    have exactly one accented form that is a word."""
    if not word[:1].isupper() or len(word) < 3:
        return word
    if _has_accent(word):
        return word
    lower = word.lower()
    if is_word(lower) or is_word(word):
        return word
    hits = [v for v in _accent_variants(lower) if is_word(v) or is_word(v.capitalize())]
    if len(hits) != 1:
        return word
    return _match_case(word, hits[0])


def modernize_spelling(
    text: str,
    *,
    is_word: Optional[Callable[[str], bool]] = None,
    counts: Optional[Counter] = None,
) -> str:
    """Return ``text`` with the rule-fixable old spellings modernized.

    ``is_word`` checks a word against a Spanish dictionary (defaults to enchant
    es_MX/es_ES; capitals are left alone when no dictionary is available).
    ``counts``, when given, is incremented with ``"old -> new"`` per change.
    """

    def note(old: str, new: str) -> str:
        if counts is not None and old != new:
            counts[f"{old} -> {new}"] += 1
        return new

    text = _ONE_LETTER_RE.sub(lambda m: note(m.group(0), _ONE_LETTER[m.group(0)]), text)
    text = _MONO_RE.sub(
        lambda m: note(m.group(0), _match_case(m.group(0), _MONOSYLLABLES[m.group(0).lower()])),
        text,
    )
    checker = is_word if is_word is not None else _default_checker()
    if checker is not None:
        text = _WORD_RE.sub(lambda m: note(m.group(0), _capital_accent(m.group(0), checker)), text)
    return text
