"""Turn judge findings into mechanically-applicable chunk corrections.

The judge-review ``apply`` verb (``scripts/run_judges.py``) uses this to decide,
*carefully*, which persisted findings can be applied as a clean text swap and
which must be left for a human. The house rule (see the friction-log Issue #5
plan): only mechanically apply a finding when it is a **uniquely-locatable text
swap** — the problem excerpt occurs exactly once in the translation and the
suggestion is a concrete replacement snippet, not an instruction. Everything
else is reported as ``ManualFinding`` so the operator can fix it in the reader /
web editor.

This module is pure (no I/O) so it is cheap to unit-test against strings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Union

# ``ManualFinding.reason`` values — stable strings the skill can relay verbatim.
REASON_NO_SUGGESTION = "no_suggestion"
REASON_NO_EXCERPT = "no_excerpt"
REASON_SUGGESTION_EQUALS_EXCERPT = "suggestion_equals_excerpt"
REASON_EXCERPT_NOT_FOUND = "excerpt_not_found"
REASON_EXCERPT_AMBIGUOUS = "excerpt_ambiguous"
REASON_SUGGESTION_NOT_LITERAL = "suggestion_not_literal"
REASON_SUGGESTION_RESTATES_CONTEXT = "suggestion_restates_context"
REASON_SUGGESTION_PLACEHOLDER = "suggestion_placeholder"
REASON_SUGGESTION_ADDS_ELLIPSIS = "suggestion_adds_ellipsis"
REASON_SUGGESTION_TOO_LONG = "suggestion_too_long"
REASON_SUGGESTION_TOO_SHORT = "suggestion_too_short"
REASON_SUGGESTION_UNBALANCED_RAYA = "suggestion_unbalanced_raya"
REASON_MIXED_REGISTER_REMAINS = "mixed_register_remains"

# A conservative detector for suggestions that describe *how to fix* rather than
# giving the corrected text (e.g. "split into two paragraphs", "fold into inciso
# #42"). These read as English/meta imperatives; a real replacement is Spanish
# prose. Kept deliberately narrow so a genuine short replacement is never hidden
# behind the manual list — a missed instruction still falls to the per-edit
# preview + approval gate in the skill.
_INSTRUCTION_RE = re.compile(
    r"""^\s*(
        split|move|add|remove|delete|use|combine|fold|break|put|start|wrap|
        replace|insert|join|separate|change|make|keep|place|indent|capitali[sz]e
    )\b
    |
    \b(
        two\s+paragraphs|new\s+line|line\s+break|own\s+paragraph|
        inciso\s*\#|paragraph\s+break
    )\b
    |
    # A trailing English conditional parenthetical is the judge hedging rather
    # than replacing — "…¡Una vez más! (if Lucy is meant to be treated as a
    # courteous-default professional contact)". Splicing it prints the hedge.
    \(\s*(if|assuming|unless|when|whether|or)\b[^)]*\)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_RULE_RE = re.compile(r"^\s*\[([^\]]+)\]")

_WORD_RE = re.compile(r"[0-9a-záéíóúüñ]+")

# How far either side of the span the restatement check looks. Bounds the cost;
# no observed corruption restated more than 15 words of context.
_BOUNDARY_WINDOW = 60

# Smallest boundary repetition treated as a restatement. Measured by replaying
# every archived judge fix across the local books (104 reconstructable records):
# 99 score 0 on both sides; the 5 known corrupting applies produce the six
# side-scores 1, 2, 3, 10, 14, 15 (one corruption contributes both head and
# tail). A threshold of 1 is false-positive-free on that corpus and catches the
# chapter_23 head-only single-word restatement; prefer catching silent dupes
# over sparing borderline FPs. See tests/test_applied_corrections_audit.py.
MIN_RESTATED_WORDS = 1

# Suggestions that mean "there is no replacement" rather than naming one.
_PLACEHOLDER_RE = re.compile(
    r"\s*(n/?a|none|null|nil|tbd|-{1,2}|\.{2,}|…)\s*", re.IGNORECASE
)

_ELLIPSES = ("...", "…")

# Length sanity for a *span replacement*: a judge fix rewrites its excerpt, it
# does not extend or truncate it. Calibrated on the 239 archived judge fixes in
# ``projects/*/corrections_applied.jsonl`` — every fix that has ever landed in a
# book. Legitimate ones top out at +13 characters. Known corrupting applies sat
# at +25, +51, +68 and +89; the absolute floor is 30 (so +25 alone does not trip
# this guard — those cases need the ratio trip or another check). A growth
# *ratio* alone is unusable, since adding "usted" to a short line swings it hard
# (`Te equivocas.` → `Se equivoca usted.` is 1.38× and perfectly correct), so
# both the ratio and the absolute delta must trip. See
# tests/test_applied_corrections_audit.py.
MAX_GROWTH_RATIO = 1.2
MAX_GROWTH_CHARS = 30

# Below this fraction of the excerpt a suggestion is dropping prose, not
# rewriting it — the archived case replaced an excerpt spanning a narration
# paragraph plus the speech after it with the speech alone. No archived fix
# shrinks this far.
MIN_SHRINK_RATIO = 0.4

_RAYA = "—"
# Characters a closing inciso raya may butt against: ``dijo—.`` / ``dijo—,``.
_AFTER_CLOSING_RAYA = ".,;:!?)»"
# What may precede a paragraph's speech-opening raya (» marks a same-speaker
# continuation), so it is not mistaken for an inciso opener.
_TURN_OPENERS = " \t»"

# The address pronouns whose register is unambiguous. ``tu``/``tus`` are left out
# deliberately: the unaccented possessive is a weak signal (and a common
# accent-stripping artefact), where ``tú``/``ti``/``contigo`` are not.
_USTED_RE = re.compile(r"\busted(?:es)?\b", re.IGNORECASE)
_TU_RE = re.compile(r"\b(tú|ti|contigo)\b", re.IGNORECASE)


def _words(s: str) -> list[str]:
    """Lowercased word tokens, punctuation-blind.

    Deliberately drops rayas, guillemets and commas: a suggestion that restates
    surrounding prose *and* repunctuates it (the `chapter_03` guillemets case)
    is still a restatement, and a byte-level duplicate check would miss it.
    """
    return _WORD_RE.findall(unicodedata.normalize("NFC", s.lower()))


def _overlap(a: list[str], b: list[str]) -> int:
    """Largest ``k`` where the last ``k`` of ``a`` equal the first ``k`` of ``b``."""
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


def boundary_overlap(text: str, start: int, end: int, replacement: str) -> tuple[int, int]:
    """``(head, tail)`` word overlap between ``replacement`` and ``text``'s context.

    ``head`` is how many words ``replacement`` opens with that already sit
    immediately *before* ``start``; ``tail`` is how many it ends with that
    already sit immediately *after* ``end``.
    """
    before = _words(text[:start])[-_BOUNDARY_WINDOW:]
    after = _words(text[end:])[:_BOUNDARY_WINDOW]
    new_words = _words(replacement)
    return _overlap(before, new_words), _overlap(new_words, after)


def restated_context(
    text: str,
    start: int,
    end: int,
    replacement: str,
    *,
    baseline: str | None = None,
    min_words: int = MIN_RESTATED_WORDS,
) -> str | None:
    """Words ``replacement`` would *newly* repeat from outside ``text[start:end]``.

    Splicing ``replacement`` into ``text[start:end]`` duplicates prose whenever
    the replacement restates text that lies outside the span it replaces — the
    judges do this when they rewrite a whole dialogue turn but key it to a short
    excerpt. Returns the repeated words (space-joined, for a warning message) or
    ``None`` when the splice is clean.

    ``baseline`` is the text being replaced. Overlap the baseline already had is
    subtracted, so a fix that merely preserves an existing boundary repetition is
    not flagged — only a newly introduced one is.
    """
    head_new, tail_new = boundary_overlap(text, start, end, replacement)
    head_old, tail_old = (
        boundary_overlap(text, start, end, baseline) if baseline is not None else (0, 0)
    )
    new_words = _words(replacement)

    if head_new > head_old and head_new >= min_words:
        return " ".join(new_words[:head_new])
    if tail_new > tail_old and tail_new >= min_words:
        return " ".join(new_words[len(new_words) - tail_new:])
    return None


@dataclass
class ProposedFix:
    """A finding that can be applied as a single, unambiguous span replacement."""

    excerpt: str  # old text, located verbatim in translated_text
    suggestion: str  # new text to substitute
    char_start: int
    char_end: int
    rule: str | None
    severity: str
    message: str


@dataclass
class ManualFinding:
    """A finding that must not be auto-applied, with a machine-readable reason."""

    reason: str
    excerpt: str | None
    suggestion: str | None
    rule: str | None
    severity: str
    message: str


Classification = Union[ProposedFix, ManualFinding]


def _issue_field(issue: Mapping[str, Any] | Any, name: str) -> Any:
    """Read a field from either a serialized-issue dict or an ``Issue`` model."""
    if isinstance(issue, Mapping):
        return issue.get(name)
    return getattr(issue, name, None)


def _extract_rule(message: str | None) -> str | None:
    """Pull the ``[rule]`` prefix the dialogue judge stamps onto each message."""
    if not message:
        return None
    m = _RULE_RE.match(message)
    return m.group(1).strip() if m else None


def looks_like_instruction(suggestion: str) -> bool:
    """True if ``suggestion`` reads as a fix instruction, not replacement text."""
    return bool(_INSTRUCTION_RE.search(suggestion))


def is_placeholder(suggestion: str) -> bool:
    """True if ``suggestion`` is a "nothing to replace" token, not replacement text.

    The judges write these to hang a note on a passage they have decided is
    actually fine ("found usted here but note the pronoun is correct — however
    ..."). A literal swap then splices the token into the book and *deletes* the
    line it was keyed to: three of these reached ``applicable[]`` on the first
    whole-book pollyanna apply (2026-07-29 friction log, item 0).
    """
    return bool(_PLACEHOLDER_RE.fullmatch(suggestion))


def adds_ellipsis(excerpt: str, suggestion: str) -> bool:
    """True if ``suggestion`` elides text that ``excerpt`` did not elide.

    A judge that needs ``...`` is quoting across material it is not replacing:
    the pollyanna ``chapter_03_chunk_000`` suggestion ellipsis-joined three
    separate passages, so splicing it duplicated the two downstream ones (item 2 —
    the boundary measure in :func:`restated_context` cannot see them, because the
    reused prose does not touch the span). It is also how a truncated or otherwise
    malformed suggestion presents, e.g. a stray trailing ``…``.
    """
    return any(e in suggestion for e in _ELLIPSES) and not any(e in excerpt for e in _ELLIPSES)


def is_too_long(excerpt: str, suggestion: str) -> bool:
    """True if ``suggestion`` grows the span past what a rewrite plausibly needs."""
    return (
        len(suggestion) - len(excerpt) >= MAX_GROWTH_CHARS
        and len(suggestion) > len(excerpt) * MAX_GROWTH_RATIO
    )


def is_too_short(excerpt: str, suggestion: str) -> bool:
    """True if ``suggestion`` is dropping prose rather than rewriting the span."""
    return len(suggestion) < len(excerpt) * MIN_SHRINK_RATIO


# Shape checks on the ``old → new`` pair alone, in report order. These run *last*
# in :func:`classify_fix` — after the excerpt has been located and after
# :func:`restated_context` — so they never mask a more specific diagnosis. A
# finding that quotes text the chunk does not contain is ``excerpt_not_found``
# (the operator's cue to fix it in the web editor) whatever its length ratio is,
# and the ``suggestion_restates_context`` rate is a number two friction logs
# track. These reasons therefore appear only on fixes that would otherwise have
# been applied.
_SPLICE_GUARDS: tuple[tuple[str, Callable[[str, str], bool]], ...] = (
    (REASON_SUGGESTION_ADDS_ELLIPSIS, adds_ellipsis),
    (REASON_SUGGESTION_TOO_LONG, is_too_long),
    (REASON_SUGGESTION_TOO_SHORT, is_too_short),
)


def _paragraph_at(text: str, pos: int) -> str:
    """The blank-line-delimited paragraph containing ``pos``."""
    start = text.rfind("\n\n", 0, pos)
    start = 0 if start == -1 else start + 2
    end = text.find("\n\n", pos)
    return text[start : end if end != -1 else len(text)]


def unopened_rayas(paragraph: str) -> int:
    """How many closing inciso rayas in ``paragraph`` have no opening raya.

    A Spanish speech turn opens with a raya, and each *inciso* (``—dijo él—``)
    opens and closes with one. So within a paragraph the rayas after the turn's
    own opener must alternate open, close, open, close — a closing raya arriving
    with no inciso open is malformed prose (``continuó—.`` where nothing opened).

    Openness is read off adjacency: a raya with whitespace before and a character
    after opens; one with a character before and whitespace, end-of-paragraph or
    ``.,;:!?)»`` after closes. Rayas that are neither or both (``ella — dijo``,
    ``bien—dijo``) are unclassifiable and left alone rather than guessed at —
    deliberately conservative, since a missed imbalance still meets the operator's
    ``old → new`` preview. Measured over 11,232 real dialogue paragraphs in the
    local books, 33 (0.29%) score above zero, all of them genuinely malformed.
    """
    p = paragraph.strip()
    depth = 0
    unopened = 0
    first = True
    for i, ch in enumerate(p):
        if ch != _RAYA:
            continue
        if first:
            first = False
            # The turn's own opening raya (optionally behind a » same-speaker
            # continuation marker) opens speech, not an inciso: no partner needed.
            if not p[:i].strip(_TURN_OPENERS):
                continue
        before = p[i - 1] if i else ""
        after = p[i + 1] if i + 1 < len(p) else ""
        opening = (not before or before.isspace()) and bool(after) and not after.isspace()
        closing = (
            bool(before)
            and not before.isspace()
            and (not after or after.isspace() or after in _AFTER_CLOSING_RAYA)
        )
        if opening and not closing:
            depth = 1
        elif closing and not opening:
            if depth == 0:
                unopened += 1
            depth = 0
    return unopened


def newly_unbalanced_raya(text: str, start: int, end: int, replacement: str) -> bool:
    """True if splicing ``replacement`` leaves a closing raya with nothing opened.

    Compares the containing paragraph before and after the splice, so only an
    imbalance the fix *introduces* is withheld — the same baseline discipline
    :func:`restated_context` uses, and necessary here because a malformed
    paragraph is frequently the very thing a dialogue finding is reporting. The
    2026-07-29 pollyanna case (item 6): ``continuó—:`` → ``continuó—.`` gave a
    second inciso a closing raya with no opening one, in a paragraph that balanced
    before the edit.
    """
    if _RAYA not in replacement and _RAYA not in text[start:end]:
        return False
    spliced = text[:start] + replacement + text[end:]
    return unopened_rayas(_paragraph_at(spliced, start)) > unopened_rayas(
        _paragraph_at(text, start)
    )


def mixed_register_remains(
    text: str, start: int, end: int, excerpt: str, suggestion: str
) -> str | None:
    """The address pronoun a normalization drops here but leaves standing elsewhere.

    ``inconsistent-address`` asserts that one speaker→addressee pair mixes tú and
    usted *within this passage*, which makes any single-line fix partial by
    construction: it has to be checked against the rest of the chunk. On
    2026-07-29 the judge normalized Nancy's ``usted le dijo`` to tú while the very
    next, unflagged line still read ``—Sí. Usted le dijo que podía estar
    contenta...`` — applying it would have *created* the inconsistency it claimed
    to fix (item 6).

    Only the unambiguous pronouns are measured; verb-conjugation register is not
    mechanically detectable and is deliberately not guessed at. Returns the
    pronoun still standing outside the replaced span, or ``None``.
    """
    rest = text[:start] + text[end:]
    for pattern, pronoun in ((_USTED_RE, "usted"), (_TU_RE, "tú")):
        if pattern.search(excerpt) and not pattern.search(suggestion) and pattern.search(rest):
            return pronoun
    return None


def classify_fix(issue: Mapping[str, Any] | Any, translated_text: str) -> Classification:
    """Classify one persisted judge issue against the chunk's ``translated_text``.

    Returns a :class:`ProposedFix` only when the excerpt (``location``) occurs
    **exactly once**, the ``suggestion`` is a concrete replacement that differs
    from the excerpt and does not read as an instruction or a placeholder, its
    length is in the range a span rewrite can plausibly need, and splicing it
    would neither restate adjacent context
    (``suggestion_restates_context``) nor break the paragraph's rayas. Otherwise a
    :class:`ManualFinding` explains why it was withheld.
    """
    severity = str(_issue_field(issue, "severity") or "warning")
    message = str(_issue_field(issue, "message") or "")
    rule = _extract_rule(message)
    excerpt = _issue_field(issue, "location")
    suggestion = _issue_field(issue, "suggestion")

    excerpt = excerpt.strip() if isinstance(excerpt, str) else ""
    suggestion = suggestion.strip() if isinstance(suggestion, str) else ""

    def manual(reason: str) -> ManualFinding:
        return ManualFinding(
            reason=reason,
            excerpt=excerpt or None,
            suggestion=suggestion or None,
            rule=rule,
            severity=severity,
            message=message,
        )

    if not suggestion:
        return manual(REASON_NO_SUGGESTION)
    if not excerpt:
        return manual(REASON_NO_EXCERPT)
    if suggestion == excerpt:
        return manual(REASON_SUGGESTION_EQUALS_EXCERPT)
    if looks_like_instruction(suggestion):
        return manual(REASON_SUGGESTION_NOT_LITERAL)
    # Like the instruction check, this asks whether the suggestion is replacement
    # text *at all*, so it belongs ahead of anything that consults the chunk:
    # "there is no fix here" is more useful than "this text isn't in the chunk".
    if is_placeholder(suggestion):
        return manual(REASON_SUGGESTION_PLACEHOLDER)

    occurrences = translated_text.count(excerpt)
    if occurrences == 0:
        return manual(REASON_EXCERPT_NOT_FOUND)
    if occurrences > 1:
        return manual(REASON_EXCERPT_AMBIGUOUS)

    start = translated_text.find(excerpt)
    end = start + len(excerpt)

    # The suggestion must be a drop-in for the excerpt *alone*. When it also
    # restates the prose on either side of the excerpt, splicing it in leaves
    # that prose duplicated in the book — the excerpt-uniqueness check above
    # cannot see this, because the excerpt itself still located cleanly.
    if restated_context(translated_text, start, end, suggestion, baseline=excerpt):
        return manual(REASON_SUGGESTION_RESTATES_CONTEXT)

    for reason, is_unsafe in _SPLICE_GUARDS:
        if is_unsafe(excerpt, suggestion):
            return manual(reason)

    # Two more checks that need the located span, both keyed on what the splice
    # would *newly* break rather than on what the paragraph already gets wrong.
    if newly_unbalanced_raya(translated_text, start, end, suggestion):
        return manual(REASON_SUGGESTION_UNBALANCED_RAYA)
    if rule == "inconsistent-address" and mixed_register_remains(
        translated_text, start, end, excerpt, suggestion
    ):
        return manual(REASON_MIXED_REGISTER_REMAINS)

    return ProposedFix(
        excerpt=excerpt,
        suggestion=suggestion,
        char_start=start,
        char_end=end,
        rule=rule,
        severity=severity,
        message=message,
    )


def to_correction_record(
    fix: ProposedFix,
    *,
    chunk_id: str,
    chapter_id: str,
    project_id: str,
    judge_name: str,
) -> dict[str, Any]:
    """Build a corrections-shaped record (see ``src/corrections_apply.py``).

    Carries the located offsets so :func:`~src.corrections_apply.apply_to_chunk`
    takes its exact-span tier (never a naive first-match replace) plus judge
    provenance so ``corrections_applied.jsonl`` records where the edit came from.
    """
    return {
        "chunk_id": chunk_id,
        "chapter_id": chapter_id,
        "project_id": project_id,
        "original_es": fix.excerpt,
        "corrected_es": fix.suggestion,
        "chunk_offset_start": fix.char_start,
        "chunk_offset_end": fix.char_end,
        "es_idx": None,
        "timestamp": datetime.now().isoformat(),
        "source": f"judge:{judge_name}",
        "rule": fix.rule,
        "severity": fix.severity,
        "message": fix.message,
    }
