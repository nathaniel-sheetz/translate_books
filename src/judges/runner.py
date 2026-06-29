"""
Judge runners: a single error-isolated judge call, and a cost-gated suite.

``run_judge`` mirrors ``src/evaluators/run_evaluator`` — it never raises, it
returns an error :class:`EvalResult` instead, so one bad judge can't sink a
suite. ``run_judge_suite`` adds the LLM-specific concerns coded evaluators
don't need: a pre-run cost estimate + ``cost_limit``/``confirm`` gate, and a
reproducibility run header (judge versions, prompt-template hashes, model, git
commit).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import Any, Optional

from src.evaluators import aggregate_results
from src.judges import llm_io
from src.judges.base import JudgeTarget
from src.judges.registry import get_judge
from src.models import EvalResult, Issue, IssueLevel

logger = logging.getLogger(__name__)


def _git_commit() -> Optional[str]:
    """Best-effort current git commit for the reproducibility header."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        pass
    return None


def run_judge(
    judge_name: str, target: JudgeTarget, context: dict[str, Any]
) -> EvalResult:
    """Run one judge on one target, returning an error result on failure."""
    try:
        judge = get_judge(judge_name)
        return judge.run(target, context)
    except Exception as exc:  # noqa: BLE001 - intentional isolation, mirrors run_evaluator
        logger.error("Judge %r failed on %s: %s", judge_name, target.id, exc, exc_info=True)
        return EvalResult(
            eval_name=judge_name,
            eval_version="unknown",
            target_id=target.id,
            target_type=target.target_type,
            passed=False,
            score=None,
            issues=[
                Issue(
                    severity=IssueLevel.ERROR,
                    message=f"Judge {judge_name!r} crashed: {exc}",
                    location=target.id,
                    suggestion="Check logs; this is a judge bug or a config/API error.",
                )
            ],
            metadata={"error": str(exc), "error_type": type(exc).__name__},
            executed_at=datetime.now(),
        )


def estimate_suite_cost(
    judge_names: list[str],
    targets: list[JudgeTarget],
    context: dict[str, Any],
) -> float:
    """Approximate total USD cost of running ``judge_names`` over ``targets``.

    Coarse by design — a guardrail, not an invoice. Sums a per-call estimate
    built from the judge's template plus the target's source + translation.
    """
    model = context.get("judge_model")
    provider = context.get("judge_provider")
    total = 0.0
    for judge_name in judge_names:
        try:
            judge = get_judge(judge_name)
            template = llm_io.load_template(judge.spec.template)
        except (ValueError, OSError):
            template = ""
        for target in targets:
            # Add a fixed allowance for rules/context blocks not in the template.
            approx_prompt = (
                template + target.source_text + target.translated_text + (" " * 2500)
            )
            total += llm_io.estimate_call_cost(
                approx_prompt, provider=provider, model=model
            )
    return round(total, 6)


def run_judge_suite(
    judge_names: list[str],
    targets: list[JudgeTarget],
    context: dict[str, Any],
    *,
    cost_limit: Optional[float] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Run judges over targets with a cost gate; aggregate + stamp a run header.

    Returns a dict with ``status`` (``"ok"`` or ``"cost_exceeded"``),
    ``estimated_cost``, and — when it ran — ``results``, ``aggregated``, and
    ``run_header``.
    """
    estimated_cost = estimate_suite_cost(judge_names, targets, context)

    if cost_limit is not None and estimated_cost > cost_limit and not confirm:
        return {
            "status": "cost_exceeded",
            "estimated_cost": estimated_cost,
            "cost_limit": cost_limit,
            "message": (
                f"Estimated ${estimated_cost:.4f} exceeds --cost-limit "
                f"${cost_limit:.2f}. Re-run with --confirm to proceed."
            ),
        }

    started_at = datetime.now().isoformat()
    results: list[EvalResult] = []
    for target in targets:
        for judge_name in judge_names:
            results.append(run_judge(judge_name, target, context))

    aggregated = aggregate_results(results)

    prompt_versions: dict[str, str] = {}
    judge_versions: dict[str, str] = {}
    for judge_name in judge_names:
        try:
            judge = get_judge(judge_name)
            judge_versions[judge_name] = judge.spec.version
            prompt_versions[judge_name] = llm_io.prompt_version(judge.spec.template)
        except (ValueError, OSError):
            continue

    run_header = {
        "judges": judge_versions,
        "prompt_versions": prompt_versions,
        "model": context.get("judge_model"),
        "provider": context.get("judge_provider"),
        "temperature": 0.0,
        "git_commit": _git_commit(),
        "started_at": started_at,
        "target_count": len(targets),
        "judge_count": len(judge_names),
    }

    return {
        "status": "ok",
        "estimated_cost": estimated_cost,
        "results": results,
        "aggregated": aggregated,
        "run_header": run_header,
    }
