"""
Dialogue-compliance judge.

Verifies that a Spanish translation follows the house dialogue rules already
written for the translator in ``prompts/dialogue.txt`` (raya usage,
one-turn-one-paragraph, incisos, guillemets for thoughts, ...). It is a pure
*verdict* judge: it flags violations as :class:`Issue`s and assigns a
compliance score; it never rewrites text.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from src.judges import llm_io
from src.judges.base import JudgeSpec, JudgeTarget, VerdictJudge, coerce_severity
from src.judges.llm_io import JudgeParseError
from src.models import EvalResult, Issue, IssueLevel

logger = logging.getLogger(__name__)

# Default rules source: the same spec the translator was given.
_DEFAULT_RULES_FILE = Path(__file__).resolve().parent.parent.parent / "prompts" / "dialogue.txt"

# Severity weights for the compliance score (1.0 = clean).
_SEVERITY_WEIGHT = {
    IssueLevel.ERROR: 0.25,
    IssueLevel.WARNING: 0.10,
    IssueLevel.INFO: 0.02,
}

# Per-rule penalty cap. A systemic habit that recurs (e.g. the same raya mistake
# 20x) is counted as one significant problem, not twenty catastrophes, so a
# single repeated pattern can't alone floor the score. The score only reaches
# 0.0 when several *distinct* rules are each heavily violated.
_RULE_CAP = 0.30

# Defensive filter: the prompt forbids reporting compliant passages, but the LLM
# sometimes narrates its inspection ("this is fine — no violation here") and
# tags it with a severity anyway. Drop those self-described non-violations so
# they don't inflate the issue count or depress the score. Deliberately keyed on
# unambiguous no-op phrasing — not bare "compliant"/"correct", which legitimately
# appear in real findings ("guillemets are correctly used here, BUT ...").
_NONISSUE_RE = re.compile(
    r"\bno change (?:needed|required|strictly)\b"
    r"|\bno violation\b"
    r"|\bnot a (?:strict )?(?:rule )?violation\b"
    r"|\bthis is fine\b"
    r"|\bacceptable as (?:written|is)\b"
    r"|\bno strict rule\b",
    re.I,
)


def _is_nonissue(finding: dict[str, Any]) -> bool:
    """True if the model self-describes this 'finding' as needing no change."""
    blob = f"{finding.get('message', '')} {finding.get('suggestion', '')}"
    return bool(_NONISSUE_RE.search(blob))


def _load_default_rules() -> str:
    try:
        return _DEFAULT_RULES_FILE.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not read default dialogue rules: %s", exc)
        return ""


def _finding_to_issue(finding: dict[str, Any]) -> Issue:
    rule = str(finding.get("rule", "other")).strip() or "other"
    base_msg = str(finding.get("message", "")).strip() or "Dialogue rule violation."
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


def _compliance_score(findings: list[dict[str, Any]]) -> float:
    """Map findings to a 0-1 compliance score (1.0 = no violations).

    Penalty is severity-weighted but capped per rule (see ``_RULE_CAP``), so one
    recurring habit can't drive the score straight to 0; the score only floors
    when several distinct rules are each heavily violated. ``findings`` are the
    raw judge dicts (each with ``rule`` + ``severity``) rather than mapped
    ``Issue``s so the per-rule grouping reads the model's own rule ids.
    """
    by_rule: dict[str, float] = defaultdict(float)
    for finding in findings:
        rule = str(finding.get("rule", "other")).strip() or "other"
        severity = coerce_severity(finding.get("severity"))
        by_rule[rule] += _SEVERITY_WEIGHT.get(severity, 0.10)
    penalty = sum(min(weight, _RULE_CAP) for weight in by_rule.values())
    return round(max(0.0, 1.0 - penalty), 4)


class DialogueComplianceJudge(VerdictJudge):
    """LLM judge that checks Spanish dialogue formatting against the house rules."""

    spec = JudgeSpec(
        name="dialogue",
        version="1.1.0",
        kind="verdict",
        template="judge_dialogue.txt",
        required_inputs=("source", "translation", "dialogue_rules"),
        output_fields=("findings",),
        description="Checks Spanish dialogue formatting against prompts/dialogue.txt",
    )

    def prompt_variables(
        self, target: JudgeTarget, context: dict[str, Any]
    ) -> dict[str, str]:
        """Template variables: the house rules plus the source + translation.

        Context keys consumed (all optional):
            ``dialogue_rules`` (str): rules text override; defaults to
                ``prompts/dialogue.txt``.
        """
        rules: Optional[str] = context.get("dialogue_rules")
        if not rules:
            rules = _load_default_rules()
        return {
            "dialogue_rules": rules,
            "source_text": target.source_text,
            "translation_text": target.translated_text,
        }

    def parse_response(
        self, target: JudgeTarget, raw: str, context: dict[str, Any]
    ) -> EvalResult:
        """Map one raw judge response to an :class:`EvalResult`.

        Shared by both backends (API ``run`` and subagent ``commit``). Raises
        :class:`JudgeParseError` on unparseable output so each backend decides
        retry-vs-re-spawn; the success path mirrors the old ``run`` body exactly.
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        data = llm_io.parse_judge_json(raw, self.spec.output_fields)

        raw_findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
        findings = [f for f in raw_findings if not _is_nonissue(f)]
        filtered = len(raw_findings) - len(findings)
        if filtered:
            logger.info(
                "Dialogue judge dropped %d self-described non-violation(s) on %s",
                filtered,
                target.id,
            )
        issues = [_finding_to_issue(f) for f in findings]
        score = _compliance_score(findings)

        return self.make_result(
            target,
            issues=issues,
            score=score,
            metadata={
                "compliant": not issues,
                "summary": str(data.get("summary", "")).strip(),
                "finding_count": len(issues),
                "filtered_nonissues": filtered,
            },
            prompt_version=llm_io.prompt_version(self.spec.template),
            model=model,
            provider=provider,
        )

    def run(self, target: JudgeTarget, context: dict[str, Any]) -> EvalResult:
        """API backend: build the prompt, call the LLM, parse the response.

        Context keys consumed (all optional): ``dialogue_rules`` (rules override),
        ``judge_model`` / ``judge_provider`` (LLM overrides). On an unparseable
        response it retries once with a stricter JSON-only suffix, then returns a
        single error issue if it still cannot parse.
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        prompt = self.build_prompt(target, context)

        raw = llm_io.call_judge(
            prompt, provider=provider, model=model, call_type="judge_dialogue"
        )
        try:
            return self.parse_response(target, raw, context)
        except JudgeParseError:
            logger.warning("Dialogue judge parse failed; retrying with stricter suffix.")
            retry_prompt = prompt + (
                "\n\nYour previous response was not valid JSON. "
                "Respond with ONLY the JSON object described above."
            )
            raw = llm_io.call_judge(
                retry_prompt,
                provider=provider,
                model=model,
                call_type="judge_dialogue",
                max_retries=1,
            )
            try:
                return self.parse_response(target, raw, context)
            except JudgeParseError as exc:
                logger.error("Dialogue judge unparseable on %s: %s", target.id, exc)
                return self.make_result(
                    target,
                    issues=[
                        Issue(
                            severity=IssueLevel.ERROR,
                            message=f"Dialogue judge returned unparseable response: {exc}",
                            location=target.id,
                        )
                    ],
                    score=None,
                    metadata={"error": str(exc), "raw_response": raw[:2000]},
                    prompt_version=llm_io.prompt_version(self.spec.template),
                    model=model,
                    provider=provider,
                )
