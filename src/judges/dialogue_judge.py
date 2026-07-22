"""
Dialogue-compliance judge.

Verifies that a Spanish translation follows the house dialogue rules already
written for the translator in ``prompts/dialogue.txt`` (raya usage,
one-turn-one-paragraph, same-speaker » continuation, incisos, guillemets
for thoughts, ...). It is a pure
*verdict* judge: it flags violations as :class:`Issue`s and assigns a
compliance score; it never rewrites text.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from src.judges import llm_io
from src.judges.base import JudgeSpec, JudgeTarget, VerdictJudge
from src.judges.llm_io import JudgeParseError
from src.judges.scoring import compliance_score, finding_to_issue, is_nonissue
from src.models import EvalResult, Issue, IssueLevel

logger = logging.getLogger(__name__)

# Default rules source: the same spec the translator was given.
_DEFAULT_RULES_FILE = Path(__file__).resolve().parent.parent.parent / "prompts" / "dialogue.txt"


def _load_default_rules() -> str:
    try:
        return _DEFAULT_RULES_FILE.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not read default dialogue rules: %s", exc)
        return ""


def _finding_to_issue(finding: dict[str, Any]) -> Issue:
    return finding_to_issue(finding, default_message="Dialogue rule violation.")


class DialogueComplianceJudge(VerdictJudge):
    """LLM judge that checks Spanish dialogue formatting against the house rules."""

    spec = JudgeSpec(
        name="dialogue",
        version="1.3.0",
        kind="verdict",
        template="judge_dialogue.txt",
        required_inputs=("source", "translation", "dialogue_rules"),
        output_fields=("findings",),
        description="Checks Spanish dialogue formatting against prompts/dialogue.txt",
    )

    # The house rules are target-independent, so several targets can share one
    # rendered rules block — this judge supports density-gated target grouping in
    # the subagent backend. The per-item source/translation come from the base
    # ``item_prompt_variables``; ``parse_response`` is reused per member unchanged.
    batch_template = "judge_dialogue_batch.txt"

    def shared_prompt_variables(self, context: dict[str, Any]) -> dict[str, str]:
        """The house dialogue rules — identical for every target, so they render
        once at the top of a batched prompt.

        Context keys consumed (all optional):
            ``dialogue_rules`` (str): rules text override; defaults to
                ``prompts/dialogue.txt``.
        """
        rules: Optional[str] = context.get("dialogue_rules")
        if not rules:
            rules = _load_default_rules()
        return {"dialogue_rules": rules}

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
        findings = [f for f in raw_findings if not is_nonissue(f)]
        filtered = len(raw_findings) - len(findings)
        if filtered:
            logger.info(
                "Dialogue judge dropped %d self-described non-violation(s) on %s",
                filtered,
                target.id,
            )
        issues = [_finding_to_issue(f) for f in findings]
        score = compliance_score(findings)

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
