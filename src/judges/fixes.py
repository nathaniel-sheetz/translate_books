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
    **exactly once** and the ``suggestion`` is a concrete replacement that
    differs from the excerpt and does not read as an instruction. Otherwise a
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
    return ProposedFix(
        excerpt=excerpt,
        suggestion=suggestion,
        char_start=start,
        char_end=start + len(excerpt),
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
