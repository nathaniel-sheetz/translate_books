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
from typing import Any, Mapping, Union

# ``ManualFinding.reason`` values — stable strings the skill can relay verbatim.
REASON_NO_SUGGESTION = "no_suggestion"
REASON_NO_EXCERPT = "no_excerpt"
REASON_SUGGESTION_EQUALS_EXCERPT = "suggestion_equals_excerpt"
REASON_EXCERPT_NOT_FOUND = "excerpt_not_found"
REASON_EXCERPT_AMBIGUOUS = "excerpt_ambiguous"
REASON_SUGGESTION_NOT_LITERAL = "suggestion_not_literal"
REASON_SUGGESTION_RESTATES_CONTEXT = "suggestion_restates_context"

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


def classify_fix(issue: Mapping[str, Any] | Any, translated_text: str) -> Classification:
    """Classify one persisted judge issue against the chunk's ``translated_text``.

    Returns a :class:`ProposedFix` only when the excerpt (``location``) occurs
    **exactly once**, the ``suggestion`` is a concrete replacement that differs
    from the excerpt and does not read as an instruction, and splicing it would
    not restate adjacent context (``suggestion_restates_context``). Otherwise a
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
