"""
Does an English form occur in a passage?

One matcher for the glossary filter, house-rule triggers, the lexicon and the
precedent index, so "this chunk needs the casilla entry" and "this chunk gets
the Old + surname note" are decided the same way.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

#: A trigger that starts with this is a regular expression, matched
#: case-sensitively against the source ("re:\\bOld [A-Z][a-z]+").
REGEX_PREFIX = "re:"


def term_variants(term_lower: str) -> list[str]:
    """``term_lower`` and its common English variants: plural, possessive, past tense."""
    variants = [
        term_lower,
        term_lower + "s",
        term_lower + "es",
        term_lower + "'s",
        term_lower + "’s",  # curly apostrophe
    ]
    if not term_lower.endswith("e"):
        variants.append(term_lower + "ed")
    if not term_lower.endswith("d"):
        variants.append(term_lower + "d")
    return variants


def ies_variants(term_lower: str) -> list[str]:
    """"party" → "parties", "partied"; nothing for a vowel + y ("day" takes a plain s)."""
    if term_lower.endswith("y") and not term_lower.endswith(("ay", "ey", "oy", "uy")):
        return [term_lower[:-1] + "ies", term_lower[:-1] + "ied"]
    return []


_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_DASHES_RE = re.compile(r"[-‐‑‒–—]+")
_SPACES_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def fold(text: str) -> str:
    """Lower-case, straighten apostrophes, read hyphens as spaces, squeeze spaces.

    Applied to both sides of a match, so "Bell Wether" finds "Bell-Wether",
    "'Lias" finds "’Lias" and a two-word term finds itself across a line break.
    """
    text = (text or "").lower().translate(_APOSTROPHES)
    text = _DASHES_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


@lru_cache(maxsize=4096)
def _folded(text_lower: str) -> str:
    """``fold(text_lower)``, kept: one chunk is tested against every glossary term."""
    return fold(text_lower)


def match_forms(term: str) -> list[str]:
    """The folded forms a key is looked for under.

    A sense label is for the reader, not the matcher: "stalk (mushroom)" is
    looked for as "stalk". A slash key names one thing two ways: "the old stag
    / the old Prince" is looked for as either. The prompt still prints the key
    as written, label and all, so the translator gets the sense.
    """
    stripped = _PAREN_RE.sub("", term or "").strip() or (term or "")
    parts = stripped.split("/") if "/" in stripped else [stripped]
    forms: list[str] = []
    for part in parts:
        form = fold(part)
        if form and form not in forms:
            forms.append(form)
    return forms


def term_occurs(term: str, text_lower: str) -> bool:
    """Whether ``term`` or a common variant of it is a substring of ``text_lower``.

    ``text_lower`` is already lower-cased so a caller testing many terms against
    one passage lowers it once. Both sides are folded (:func:`fold`) and the term
    is looked for under each of its :func:`match_forms`. Substring, not
    whole-word: that is what the glossary filter has always done, and a glossary
    entry that reaches a chunk it does not need costs a line, while one that
    misses a chunk costs a wrong word.
    """
    forms = match_forms(term)
    if not forms:
        return False
    text = _folded(text_lower)
    return any(
        v in text for form in forms for v in (*term_variants(form), *ies_variants(form))
    )


def entry_occurs(english: str, aliases: list[str] | tuple[str, ...], text_lower: str) -> bool:
    """Whether a glossary entry's key, or any of its aliases, occurs in ``text_lower``.

    The one test for "does this chunk need this entry": the translator filter,
    the dead-key warning and the book review all ask it this way.
    """
    return any(term_occurs(form, text_lower) for form in (english, *(aliases or ())))


@lru_cache(maxsize=4096)
def _lowered(text: str) -> str:
    """``text.lower()``, kept: the same book and chunks are tested against many terms."""
    return text.lower()


@lru_cache(maxsize=4096)
def _word_pattern(term_lower: str) -> re.Pattern:
    variants = term_variants(term_lower)
    if term_lower.endswith("y"):
        variants += [term_lower[:-1] + "ies", term_lower[:-1] + "ied"]
    alternation = "|".join(
        re.escape(v).replace(r"\ ", r"\s+") for v in sorted(set(variants), key=len, reverse=True)
    )
    return re.compile(r"(?<![^\W\d_])(?:" + alternation + r")(?![^\W\d_])", re.IGNORECASE)


def word_occurs(term: str, text: str) -> bool:
    """Whether ``term`` or a common variant of it occurs in ``text`` as whole words.

    For deciding whether a book uses a word at all (house precedent, the
    lexicon), where :func:`term_occurs`'s substring test is wrong: it finds
    "oars" in "hoarsely" and "Napoli" in "Indianapolis".
    """
    term_lower = (term or "").strip().lower()
    if not term_lower:
        return False
    # Every variant starts with the term's first word (less a final y, for
    # "ies"). The pattern opens with a lookbehind, so a search is tried at every
    # position of a whole book; a substring miss rules the term out first.
    stem = term_lower.split()[0]
    if " " not in term_lower and stem.endswith("y"):
        stem = stem[:-1]
    if stem and stem not in _lowered(text):
        return False
    return _word_pattern(term_lower).search(text) is not None


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error as exc:
        logger.warning("Ignoring invalid house-rule trigger %r: %s", pattern, exc)
        return None


def trigger_matches(trigger: str, text: str, text_lower: str | None = None) -> bool:
    """Whether one trigger fires on ``text``.

    A plain trigger is matched like a glossary term; ``re:<pattern>`` is a
    case-sensitive regular expression, for shapes no word list can name (a
    capitalised surname after "Old"). An invalid pattern never fires.
    """
    trigger = str(trigger or "")
    if trigger.startswith(REGEX_PREFIX):
        compiled = _compiled(trigger[len(REGEX_PREFIX):])
        return bool(compiled and compiled.search(text))
    return term_occurs(trigger, text_lower if text_lower is not None else text.lower())


def any_trigger(triggers: list[str], text: str) -> bool:
    lowered = text.lower()
    return any(trigger_matches(t, text, lowered) for t in triggers)


def count_word(word: str, text: str) -> int:
    """Whole-word, case-insensitive occurrences of ``word`` (and its plural) in ``text``."""
    word = (word or "").strip()
    if not word or word.lower() not in _lowered(text):
        return 0
    pattern = r"(?<![^\W\d_])" + re.escape(word) + r"(?:e?s)?(?![^\W\d_])"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))
