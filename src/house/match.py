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


def term_occurs(term: str, text_lower: str) -> bool:
    """Whether ``term`` or a common variant of it is a substring of ``text_lower``.

    ``text_lower`` is already lower-cased so a caller testing many terms against
    one passage lowers it once. Substring, not whole-word: that is what the
    glossary filter has always done, and a glossary entry that reaches a chunk it
    does not need costs a line, while one that misses a chunk costs a wrong word.
    """
    term_lower = (term or "").strip().lower()
    if not term_lower:
        return False
    return any(v in text_lower for v in term_variants(term_lower))


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
    if not word:
        return 0
    pattern = r"(?<![^\W\d_])" + re.escape(word) + r"(?:e?s)?(?![^\W\d_])"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))
