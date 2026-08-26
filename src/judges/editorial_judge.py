"""Editorial defect judge.

Answers a different question from the dialogue and address judges. Those check
compliance: *does this passage follow rule X?* This one checks editorial
quality: *would a competent editor stop here and fix this?* — the clarity,
naturalness and fidelity defects no rulebook enumerates.

Two things distinguish it from its siblings:

**It reads Spanish-first.** ``item_prompt_variables`` deliberately omits
``source_text``. The dialogue judge is shown the English so it can tell which
passages are speech; here the English is the thing being withheld, because a
judge that can see the original stops evaluating the Spanish as Spanish and
starts diffing it against the source. Findings that genuinely need the original
say so via ``source_check`` and are settled by
:mod:`src.judges.editorial_verify` in a second, batched pass.

**Precision is the feature.** The human marks in ``_feedback.jsonl`` put the
coded evaluators at 81-91% false positive and the two existing judges at 34-45%.
A judge in that range gets ignored, correctly. So the threshold is defended
structurally rather than by instruction alone: a per-passage findings budget, a
confidence floor applied here in code, the shared ``is_nonissue`` filter, a list
of what the coded evaluators already reported (so the same defect is not counted
twice), and the adjudication pass.

It is a pure *verdict* judge: it flags defects as :class:`Issue`s and assigns a
score; it never rewrites text. Its ``suggestion`` is shaped for
``run_judges.py apply`` — a drop-in replacement for its own ``excerpt`` — so the
gates in :mod:`src.judges.fixes` decide what is safe to splice.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.judges import llm_io
from src.judges.base import JudgeSpec, JudgeTarget, VerdictJudge
from src.judges.llm_io import JudgeParseError
from src.judges.scoring import compliance_score, finding_to_issue, is_nonissue
from src.models import EvalResult, Issue, IssueLevel

logger = logging.getLogger(__name__)

#: Findings allowed per 1,000 translated words. A ceiling, not a target: the
#: measured density of the existing checkers is 0.8-3.2 findings per chunk, and
#: this judge aims below that. It exists to stop a model that has decided to be
#: thorough from emitting fifteen marginal notes on one passage — at which point
#: ranking and dropping is the correct behaviour, and the prompt says so.
FINDINGS_PER_1000_WORDS = 3

#: Floor for very short chunks (some books chunk at ~76 words a piece), so the
#: budget never rounds down to zero and forbids a real finding.
MIN_FINDINGS_BUDGET = 2

#: Confidence values the judge may return, most confident first. There is no
#: "low": a low-confidence finding is not a finding, and the prompt says so.
CONFIDENCE_ORDER = ("high", "medium")

#: Default floor. Stage 1 ships high-only; relax it once the accept rate is
#: known from ``scripts/editorial_metrics.py``. Overridable per run via the
#: ``editorial_min_confidence`` context key.
DEFAULT_MIN_CONFIDENCE = "high"

#: The five Stage 1 categories. CLARITY and READABILITY are deliberately folded
#: into NATURALNESS (they overlap it almost entirely), and OMISSION_ADDITION
#: into FIDELITY_SUSPECT (both are the one decision "does the English change my
#: judgment"). Fewer categories means thicker per-category dismissal stats,
#: which is what the calibration in Stage 3 actually needs.
CATEGORIES = (
    "GRAMMAR",
    "NATURALNESS",
    "STYLE_GUIDE",
    "CONSISTENCY",
    "FIDELITY_SUSPECT",
)

SOURCE_CHECK_VALUES = ("not_needed", "recommended", "required")

_NO_STYLE_RULES = (
    "(this book has no extracted rule list — judge STYLE_GUIDE findings against "
    "the prose style guide above, and leave `rule` as a short defect slug.)"
)
_NO_GLOSSARY = "(no glossary for this book.)"
_NO_STYLE_GUIDE = "(no style guide for this book.)"
_NOTHING_REPORTED = "(nothing has been reported on this passage yet.)"
_NO_EXAMPLES = (
    "(no calibrated examples yet — apply the threshold as written above.)"
)


def findings_budget(word_count: int) -> int:
    """Maximum findings allowed for a passage of ``word_count`` words."""
    scaled = round((max(word_count, 0) / 1000.0) * FINDINGS_PER_1000_WORDS)
    return max(MIN_FINDINGS_BUDGET, int(scaled))


def normalize_confidence(value: Any) -> str:
    """Coerce a judge-supplied confidence to one of :data:`CONFIDENCE_ORDER`.

    Anything unrecognized — including the "low" the prompt forbids — is read as
    the weakest allowed value rather than the strongest, so a malformed field
    can never promote a finding past the floor.
    """
    text = str(value or "").strip().lower()
    if text in CONFIDENCE_ORDER:
        return text
    return CONFIDENCE_ORDER[-1]


def _meets_confidence(finding: dict[str, Any], minimum: str) -> bool:
    floor = minimum if minimum in CONFIDENCE_ORDER else DEFAULT_MIN_CONFIDENCE
    return CONFIDENCE_ORDER.index(
        normalize_confidence(finding.get("confidence"))
    ) <= CONFIDENCE_ORDER.index(floor)


def normalize_source_check(value: Any) -> str:
    """Coerce a judge-supplied ``source_check`` to a known value.

    Unknown values read as ``"not_needed"``: a finding the judge could not
    classify does not get to conscript an English window it never asked for.
    """
    text = str(value or "").strip().lower()
    return text if text in SOURCE_CHECK_VALUES else SOURCE_CHECK_VALUES[0]


def normalize_category(value: Any) -> str:
    """Coerce a judge-supplied category to one of :data:`CATEGORIES`.

    An unrecognized category becomes ``NATURALNESS`` — the broadest bucket —
    rather than being dropped, so a real defect is never lost to a typo. The
    per-category precision report will show if this is firing often.
    """
    text = str(value or "").strip().upper()
    return text if text in CATEGORIES else "NATURALNESS"


def _finding_to_issue(finding: dict[str, Any]) -> Issue:
    return finding_to_issue(
        finding,
        default_message="Editorial defect.",
        stable_identity=True,
    )


def format_already_reported(entries: list[str]) -> str:
    """Render the coded evaluators' live findings as a do-not-repeat list."""
    cleaned = [str(e).strip() for e in entries if str(e or "").strip()]
    if not cleaned:
        return _NOTHING_REPORTED
    return "\n".join(f"- {line}" for line in cleaned)


class EditorialJudge(VerdictJudge):
    """LLM judge that reports editorial defects in a Spanish translation."""

    spec = JudgeSpec(
        name="editorial",
        version="1.0.0",
        kind="verdict",
        template="judge_editorial.txt",
        required_inputs=("translation", "style_guide", "glossary"),
        output_fields=("findings",),
        description=(
            "Reports editorial defects (grammar, naturalness, style-guide, "
            "consistency, suspected fidelity) in a Spanish translation, judged "
            "Spanish-first"
        ),
    )

    # The style guide, rule list, glossary and calibration examples are
    # book-level and target-independent, so several passages can share one
    # rendered block. Each item carries its own translation, do-not-repeat list
    # and budget.
    batch_template = "judge_editorial_batch.txt"

    def shared_prompt_variables(self, context: dict[str, Any]) -> dict[str, str]:
        """The book-level inputs — identical for every target in a run.

        Context keys consumed (all optional):
            ``style_guide`` (str): the prose style guide (``style.json`` content).
            ``style_rules`` (str): rendered hard rules with ids, from the
                optional ``style_rules.json`` sidecar.
            ``glossary`` (str): the book's glossary, prompt-formatted.
            ``calibration_examples`` (str): accepted/dismissed examples from the
                human feedback corpus.
        """
        return {
            "style_guide": str(context.get("style_guide") or "").strip() or _NO_STYLE_GUIDE,
            "style_rules": str(context.get("style_rules") or "").strip() or _NO_STYLE_RULES,
            "glossary": str(context.get("glossary") or "").strip() or _NO_GLOSSARY,
            "calibration_examples": str(
                context.get("calibration_examples") or ""
            ).strip()
            or _NO_EXAMPLES,
        }

    def item_prompt_variables(
        self, target: JudgeTarget, context: dict[str, Any]
    ) -> dict[str, str]:
        """Per-target Spanish, its do-not-repeat list, and its findings budget.

        ``source_text`` is deliberately absent. This judge reads the Spanish as
        Spanish; the English is withheld until the adjudication pass, and only
        for the findings that asked for it.

        Context keys consumed (optional):
            ``coded_findings`` (dict): ``{target_id: [str, ...]}`` — live
                findings the coded evaluators already reported for that chunk.
        """
        coded = context.get("coded_findings") or {}
        entries = coded.get(target.id) if isinstance(coded, dict) else None
        word_count = len((target.translated_text or "").split())
        return {
            "translation_text": target.translated_text,
            "already_reported": format_already_reported(list(entries or [])),
            "max_findings": str(findings_budget(word_count)),
        }

    def select_findings(
        self, raw_findings: list[dict[str, Any]], target: JudgeTarget, context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Apply the coded half of the threshold, in order, and report the drops.

        Three gates the prompt asks for and code enforces, because a prompt-only
        threshold is exactly what the 45%-false-positive dialogue judge already
        has:

        1. ``is_nonissue`` — the shared filter for a model that narrates its
           inspection ("this is fine") and tags it with a severity anyway.
        2. the confidence floor.
        3. the per-passage budget, applied last and by severity so that
           truncation drops the least serious rather than the last-listed.
        """
        minimum = str(context.get("editorial_min_confidence") or DEFAULT_MIN_CONFIDENCE)
        budget = findings_budget(len((target.translated_text or "").split()))

        kept = [f for f in raw_findings if not is_nonissue(f)]
        dropped_nonissue = len(raw_findings) - len(kept)

        confident = [f for f in kept if _meets_confidence(f, minimum)]
        dropped_confidence = len(kept) - len(confident)

        if len(confident) > budget:
            severity_rank = {"error": 0, "warning": 1, "info": 2}
            confident = sorted(
                confident,
                key=lambda f: (
                    severity_rank.get(str(f.get("severity", "")).strip().lower(), 3),
                    CONFIDENCE_ORDER.index(normalize_confidence(f.get("confidence"))),
                ),
            )[:budget]
        dropped_budget = len(kept) - dropped_confidence - len(confident)

        return confident, {
            "dropped_nonissue": dropped_nonissue,
            "dropped_confidence": dropped_confidence,
            "dropped_budget": dropped_budget,
            "budget": budget,
        }

    def parse_response(
        self, target: JudgeTarget, raw: str, context: dict[str, Any]
    ) -> EvalResult:
        """Map one raw judge response to an :class:`EvalResult`.

        Shared by both backends (API ``run`` and subagent ``commit``). Raises
        :class:`JudgeParseError` on unparseable output so each backend decides
        retry-vs-re-spawn.

        The survivors become ``issues``; the *pre-adjudication* set is kept in
        ``metadata.candidates`` with its normalized ``category``, ``confidence``
        and ``source_check``. That is not redundancy — the adjudication pass and
        the precision report both need to know what was proposed before anything
        retracted it, and ``issues`` alone cannot answer "how often did the
        second pass change the outcome".
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        data = llm_io.parse_judge_json(raw, self.spec.output_fields)
        raw_findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]

        findings, drops = self.select_findings(raw_findings, target, context)
        candidates = [self._normalize_finding(f) for f in findings]
        issues = [_finding_to_issue(f) for f in candidates]
        score = compliance_score(candidates)

        if drops["dropped_nonissue"] or drops["dropped_confidence"] or drops["dropped_budget"]:
            logger.info(
                "Editorial judge on %s dropped %d non-issue(s), %d below confidence, "
                "%d over the budget of %d",
                target.id,
                drops["dropped_nonissue"],
                drops["dropped_confidence"],
                drops["dropped_budget"],
                drops["budget"],
            )

        return self.make_result(
            target,
            issues=issues,
            score=score,
            metadata={
                "clean": not issues,
                "summary": str(data.get("summary", "")).strip(),
                "finding_count": len(issues),
                "proposed_count": len(raw_findings),
                "filtered_nonissues": drops["dropped_nonissue"],
                "filtered_low_confidence": drops["dropped_confidence"],
                "filtered_over_budget": drops["dropped_budget"],
                "findings_budget": drops["budget"],
                "min_confidence": str(
                    context.get("editorial_min_confidence") or DEFAULT_MIN_CONFIDENCE
                ),
                "candidates": candidates,
                "verified": False,
            },
            prompt_version=llm_io.prompt_version(self.spec.template),
            model=model,
            provider=provider,
        )

    @staticmethod
    def _normalize_finding(finding: dict[str, Any]) -> dict[str, Any]:
        """Return the finding with its three enum fields coerced to known values."""
        normalized = dict(finding)
        normalized["category"] = normalize_category(finding.get("category"))
        normalized["confidence"] = normalize_confidence(finding.get("confidence"))
        normalized["source_check"] = normalize_source_check(finding.get("source_check"))
        return normalized

    def run(self, target: JudgeTarget, context: dict[str, Any]) -> EvalResult:
        """API backend: build the prompt, call the LLM, parse the response.

        Context keys consumed (all optional): ``style_guide``, ``style_rules``,
        ``glossary``, ``calibration_examples``, ``coded_findings``,
        ``editorial_min_confidence``, and ``judge_model`` / ``judge_provider``.
        On an unparseable response it retries once with a stricter JSON-only
        suffix, then returns a single error issue if it still cannot parse.
        """
        model: Optional[str] = context.get("judge_model")
        provider: Optional[str] = context.get("judge_provider")

        # Split rather than build_prompt(): the prefix is the book-level style
        # guide, rules, glossary and examples — identical for every target, so it
        # caches across the suite. prefix + suffix is byte-identical to
        # build_prompt(), which keeps the prompt_version hash and the
        # API/subagent prompt parity intact.
        prefix, suffix = self.build_prompt_parts(target, context)
        prompt = prefix + suffix

        raw = llm_io.call_judge(
            prompt,
            provider=provider,
            model=model,
            call_type="judge_editorial",
            cache_prefix=prefix or None,
        )
        try:
            return self.parse_response(target, raw, context)
        except JudgeParseError:
            logger.warning("Editorial judge parse failed; retrying with stricter suffix.")
            retry_prompt = prompt + (
                "\n\nYour previous response was not valid JSON. "
                "Respond with ONLY the JSON object described above."
            )
            raw = llm_io.call_judge(
                retry_prompt,
                provider=provider,
                model=model,
                call_type="judge_editorial",
                max_retries=1,
                # The note is appended, so prefix still leads retry_prompt: the
                # retry reads the cache rather than paying a second write.
                cache_prefix=prefix or None,
            )
            try:
                return self.parse_response(target, raw, context)
            except JudgeParseError as exc:
                logger.error("Editorial judge unparseable on %s: %s", target.id, exc)
                return self.make_result(
                    target,
                    issues=[
                        Issue(
                            severity=IssueLevel.ERROR,
                            message=f"Editorial judge returned unparseable response: {exc}",
                            location=target.id,
                        )
                    ],
                    score=None,
                    metadata={"error": str(exc), "raw_response": raw[:2000]},
                    prompt_version=llm_io.prompt_version(self.spec.template),
                    model=model,
                    provider=provider,
                )
