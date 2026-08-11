"""Tailored LLM judges: single-purpose LLM evaluators.

Run a judge independently, as a suite, or from the judge-review skill. Verdict
judges emit :class:`~src.models.EvalResult` and reuse the existing
``evaluations/*.json`` persistence + feedback loop; the framework also leaves a
``kind="corrector"`` slot for rewrite-style judges (see ``src/retranslator.py``).

Public API:
    >>> from src.judges import build_targets, get_judge, run_judge, run_judge_suite
"""

from src.judges.base import Judge, JudgeSpec, JudgeTarget, VerdictJudge
from src.judges.context import build_judge_context
from src.judges.llm_io import JudgeParseError
from src.judges.registry import (
    all_suites,
    available_judges,
    get_judge,
    resolve_suite,
)
from src.judges.runner import estimate_suite_cost, run_judge, run_judge_suite
from src.judges.scope import ScopeError, build_targets

__all__ = [
    "Judge",
    "JudgeSpec",
    "JudgeTarget",
    "VerdictJudge",
    "JudgeParseError",
    "ScopeError",
    "build_judge_context",
    "build_targets",
    "get_judge",
    "available_judges",
    "all_suites",
    "resolve_suite",
    "run_judge",
    "run_judge_suite",
    "estimate_suite_cost",
]
