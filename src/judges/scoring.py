"""Shared finding→issue mapping and compliance scoring for verdict judges.

Extracted from the dialogue judge so a second verdict judge (the ``address``
judge) reuses the exact same severity weighting, per-rule penalty cap, and
self-described-non-issue filter instead of a third copy. Both judges emit a
``findings`` list of ``{rule, severity, excerpt, message, suggestion}`` dicts, so
the mapping is identical; only the human-readable default message differs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from src.judges.base import coerce_severity
from src.models import Issue, IssueLevel

# Severity weights for the compliance score (1.0 = clean).
SEVERITY_WEIGHT: dict[IssueLevel, float] = {
    IssueLevel.ERROR: 0.25,
    IssueLevel.WARNING: 0.10,
    IssueLevel.INFO: 0.02,
}

# Per-rule penalty cap. A systemic habit that recurs (e.g. the same mistake 20x)
# counts as one significant problem, not twenty catastrophes, so a single
# repeated pattern can't alone floor the score. The score only reaches 0.0 when
# several *distinct* rules are each heavily violated.
RULE_CAP = 0.30

# Defensive filter: judge prompts forbid reporting compliant passages, but the
# LLM sometimes narrates its inspection ("this is fine — no violation here") and
# tags it with a severity anyway. Drop those self-described non-violations so
# they don't inflate the issue count or depress the score. Deliberately keyed on
# unambiguous no-op phrasing — not bare "compliant"/"correct", which legitimately
# appear in real findings ("usted is correctly used here, BUT ...").
_NONISSUE_RE = re.compile(
    r"\bno change (?:needed|required|strictly)\b"
    r"|\bno violation\b"
    r"|\bnot a (?:strict )?(?:rule )?violation\b"
    r"|\bthis is fine\b"
    r"|\bacceptable as (?:written|is)\b"
    r"|\bno strict rule\b",
    re.I,
)


def is_nonissue(finding: dict[str, Any]) -> bool:
    """True if the model self-describes this 'finding' as needing no change."""
    blob = f"{finding.get('message', '')} {finding.get('suggestion', '')}"
    return bool(_NONISSUE_RE.search(blob))


def finding_to_issue(finding: dict[str, Any], *, default_message: str) -> Issue:
    """Map one judge finding dict to an :class:`Issue`.

    The ``rule`` id is prefixed onto the message (``[rule] message``) so the
    reader's finding card shows which rule fired; ``excerpt`` becomes the
    location (the verbatim offending snippet).
    """
    rule = str(finding.get("rule", "other")).strip() or "other"
    base_msg = str(finding.get("message", "")).strip() or default_message
    message = f"[{rule}] {base_msg}"
    return Issue(
        severity=coerce_severity(finding.get("severity")),
        message=message,
        location=(str(finding.get("excerpt")).strip() or None)
        if finding.get("excerpt")
        else None,
        suggestion=(str(finding.get("suggestion")).strip() or None)
        if finding.get("suggestion")
        else None,
    )


def compliance_score(findings: list[dict[str, Any]]) -> float:
    """Map findings to a 0-1 compliance score (1.0 = no violations).

    Penalty is severity-weighted but capped per rule (see :data:`RULE_CAP`), so
    one recurring habit can't drive the score straight to 0; the score only
    floors when several distinct rules are each heavily violated. ``findings`` are
    the raw judge dicts (each with ``rule`` + ``severity``) so the per-rule
    grouping reads the model's own rule ids.
    """
    by_rule: dict[str, float] = defaultdict(float)
    for finding in findings:
        rule = str(finding.get("rule", "other")).strip() or "other"
        severity = coerce_severity(finding.get("severity"))
        by_rule[rule] += SEVERITY_WEIGHT.get(severity, 0.10)
    penalty = sum(min(weight, RULE_CAP) for weight in by_rule.values())
    return round(max(0.0, 1.0 - penalty), 4)
