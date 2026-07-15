"""Forms-of-address (usted/tú) compliance judge.

Verifies that a Spanish translation addresses characters with the expected
register (formal *usted* vs. informal *tú*) according to the book's ADDRESS MAP
(``projects/<slug>/address_map.json``). Unlike the dialogue judge, the correct
form is book-specific — it depends on who addresses whom, the relationship, the
public/private situation, and can change as the story progresses — so the
per-book map is injected via ``context`` (the ``address_map`` key), while the
universal "how to detect address forms" rubric lives in ``prompts/address_forms.txt``.

It is a pure *verdict* judge: it flags violations as :class:`Issue`s and assigns
a compliance score; it never rewrites text.
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

# The universal methodology (how to spot tú/usted, attribute a line, infer the
# scene). Target- and book-independent, so it renders once at the top of a
# batched prompt alongside the per-book map.
_DEFAULT_RUBRIC_FILE = Path(__file__).resolve().parent.parent.parent / "prompts" / "address_forms.txt"

# Shown when a run reaches the judge with no per-book map. The CLI/skill is meant
# to precheck this and refuse first; this keeps the prompt honest if one slips through.
_NO_MAP_PLACEHOLDER = (
    "(no address map was provided — you cannot check explicit pair expectations; "
    "flag only unambiguous global-rule violations, if any, else return compliant.)"
)


def _load_default_rubric() -> str:
    try:
        return _DEFAULT_RUBRIC_FILE.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not read default address rubric: %s", exc)
        return ""


def _finding_to_issue(finding: dict[str, Any]) -> Issue:
    return finding_to_issue(finding, default_message="Forms-of-address violation.")


class AddressComplianceJudge(VerdictJudge):
    """LLM judge that checks usted/tú against the per-book address map."""

    spec = JudgeSpec(
        name="address",
        version="1.1.0",
        kind="verdict",
        template="judge_address.txt",
        required_inputs=("source", "translation", "address_map", "address_rubric"),
        output_fields=("findings",),
        description="Checks Spanish usted/tú address against projects/<slug>/address_map.json",
    )

    # The rubric + map are target-independent within a book, so several targets
    # can share one rendered shared block — this judge supports density-gated
    # target grouping in the subagent backend. The per-item source/translation +
    # chapter_ref come from ``item_prompt_variables``.
    batch_template = "judge_address_batch.txt"

    def shared_prompt_variables(self, context: dict[str, Any]) -> dict[str, str]:
        """The rubric + the book's address map — identical for every target.

        Context keys consumed (all optional):
            ``address_rubric`` (str): methodology override; defaults to
                ``prompts/address_forms.txt``.
            ``address_map`` (str): the book's expectations prose (the ``content``
                field of ``address_map.json``); a placeholder is used if absent.
        """
        rubric: Optional[str] = context.get("address_rubric")
        if not rubric:
            rubric = _load_default_rubric()
        address_map = str(context.get("address_map") or "").strip() or _NO_MAP_PLACEHOLDER
        return {"address_rubric": rubric, "address_map": address_map}

    def item_prompt_variables(
        self, target: JudgeTarget, context: dict[str, Any]
    ) -> dict[str, str]:
        """Per-target source + translation, plus a chapter reference.

        ``chapter_ref`` lets the judge apply story-stage-specific expectations
        (the map's ``since``/``until``/``after_event`` windows) to the right point
        in the book.
        """
        chapter_ref = str(target.context.get("chapter_id") or target.id)
        return {
            "source_text": target.source_text,
            "translation_text": target.translated_text,
            "chapter_ref": chapter_ref,
        }

    def parse_response(
        self, target: JudgeTarget, raw: str, context: dict[str, Any]
    ) -> EvalResult:
        """Map one raw judge response to an :class:`EvalResult`.

        Shared by both backends (API ``run`` and subagent ``commit``). Raises
        :class:`JudgeParseError` on unparseable output so each backend decides
        retry-vs-re-spawn.
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        data = llm_io.parse_judge_json(raw, self.spec.output_fields)

        raw_findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
        findings = [f for f in raw_findings if not is_nonissue(f)]
        filtered = len(raw_findings) - len(findings)
        if filtered:
            logger.info(
                "Address judge dropped %d self-described non-violation(s) on %s",
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

        Context keys consumed (all optional): ``address_map`` / ``address_rubric``
        (overrides), ``judge_model`` / ``judge_provider`` (LLM overrides). On an
        unparseable response it retries once with a stricter JSON-only suffix, then
        returns a single error issue if it still cannot parse.
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        prompt = self.build_prompt(target, context)

        raw = llm_io.call_judge(
            prompt, provider=provider, model=model, call_type="judge_address"
        )
        try:
            return self.parse_response(target, raw, context)
        except JudgeParseError:
            logger.warning("Address judge parse failed; retrying with stricter suffix.")
            retry_prompt = prompt + (
                "\n\nYour previous response was not valid JSON. "
                "Respond with ONLY the JSON object described above."
            )
            raw = llm_io.call_judge(
                retry_prompt,
                provider=provider,
                model=model,
                call_type="judge_address",
                max_retries=1,
            )
            try:
                return self.parse_response(target, raw, context)
            except JudgeParseError as exc:
                logger.error("Address judge unparseable on %s: %s", target.id, exc)
                return self.make_result(
                    target,
                    issues=[
                        Issue(
                            severity=IssueLevel.ERROR,
                            message=f"Address judge returned unparseable response: {exc}",
                            location=target.id,
                        )
                    ],
                    score=None,
                    metadata={"error": str(exc), "raw_response": raw[:2000]},
                    prompt_version=llm_io.prompt_version(self.spec.template),
                    model=model,
                    provider=provider,
                )
