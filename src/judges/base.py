"""
Core abstractions for tailored LLM judges.

A *judge* is a small, single-purpose LLM evaluator. Two kinds are modelled:

- ``"verdict"`` judges score / flag a target and return an :class:`EvalResult`
  (issues + optional 0-1 score). This reuses the existing result model and the
  ``evaluations/*.json`` persistence + ``_feedback.jsonl`` loop. The dialogue
  compliance judge is the first of these.
- ``"corrector"`` judges (designed for, not yet implemented) propose replacement
  text. ``src/retranslator.py`` is the existing prototype of that shape; the
  spec leaves a clean slot for it.

Judges deliberately do **not** subclass ``BaseEvaluator``: that base is
chunk-scoped and lives in the cheap/deterministic coded-evaluator run path.
Judges call the LLM, are cost-gated, and live in their own registry — but they
emit the same :class:`EvalResult` so everything downstream (persistence, web
badges, feedback) works unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.models import EvalResult, Issue, IssueLevel


@dataclass
class JudgeTarget:
    """A unit of text for a judge to evaluate, at any granularity.

    Built by ``src.judges.scope.build_targets``. ``target_type`` flows straight
    into :attr:`EvalResult.target_type` so persistence keys correctly.
    """

    id: str
    target_type: str  # "chunk" | "chapter" | "sentence"
    source_text: str
    translated_text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeSpec:
    """Declarative description of a judge.

    Adding a judge is mostly: write a prompt template, fill one of these, and
    register the class. ``required_inputs`` documents what the judge needs in
    its context dict; ``output_fields`` are the required top-level keys of the
    judge's JSON response (validated by :func:`llm_io.parse_judge_json`).
    """

    name: str
    version: str
    kind: str  # "verdict" | "corrector"
    template: str
    required_inputs: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    description: str = ""


def coerce_severity(value: Any, default: IssueLevel = IssueLevel.WARNING) -> IssueLevel:
    """Map a judge-supplied severity string onto :class:`IssueLevel`."""
    if isinstance(value, IssueLevel):
        return value
    try:
        return IssueLevel(str(value).strip().lower())
    except ValueError:
        return default


class Judge(ABC):
    """Abstract judge. Concrete judges set ``spec`` and implement ``run``."""

    spec: JudgeSpec

    @abstractmethod
    def run(self, target: JudgeTarget, context: dict[str, Any]) -> EvalResult:
        """Evaluate ``target`` and return an :class:`EvalResult`."""

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def version(self) -> str:
        return self.spec.version

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{type(self).__name__}(name={self.spec.name}, v{self.spec.version})"


class VerdictJudge(Judge):
    """Base for judges that emit issues + an optional score."""

    def make_result(
        self,
        target: JudgeTarget,
        issues: list[Issue],
        *,
        score: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
        prompt_version: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> EvalResult:
        """Construct an :class:`EvalResult` for ``target``.

        Stamps reproducibility metadata (judge version, prompt-template hash,
        resolved model/provider) so a persisted judge result is self-describing.
        ``passed`` is False if any error-level issue is present.
        """
        meta: dict[str, Any] = {
            "judge_version": self.spec.version,
            "judge_kind": self.spec.kind,
        }
        if prompt_version is not None:
            meta["prompt_version"] = prompt_version
        if model is not None:
            meta["model"] = model
        if provider is not None:
            meta["provider"] = provider
        if metadata:
            meta.update(metadata)

        passed = not any(i.severity == IssueLevel.ERROR for i in issues)
        return EvalResult(
            eval_name=self.spec.name,
            eval_version=self.spec.version,
            target_id=target.id,
            target_type=target.target_type,
            passed=passed,
            score=score,
            issues=issues,
            metadata=meta,
            executed_at=datetime.now(),
        )
