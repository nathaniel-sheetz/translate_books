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


def _batch_item_block(item_id: str, item_vars: dict[str, str]) -> str:
    """Render one ``<item>`` block for a batched judge prompt.

    Uses the same ``<source>`` / ``<translation>`` tags as the solo templates so
    a judge's rules read identically whether it judges one target or several.
    Values are interpolated directly (not ``str.format``-substituted) so literal
    braces in the source/translation text can never break rendering.
    """
    return (
        f'<item id="{item_id}">\n'
        f"<source>\n{item_vars.get('source_text', '')}\n</source>\n"
        f"<translation>\n{item_vars.get('translation_text', '')}\n</translation>\n"
        "</item>"
    )


class Judge(ABC):
    """Abstract judge. Concrete judges set ``spec`` and implement ``run``.

    A judge is split across three methods so the same judge can run via two
    interchangeable backends — the metered **API** path and the zero-spend
    **subagent** path — without duplicating prompt-building or response-parsing:

    - :meth:`build_prompt` renders the exact prompt string. The API path
      (:meth:`run`) sends it to the LLM; the subagent backend writes it to a
      file for a spawned ``judge-worker`` to answer. The prompt is *identical*
      either way, so the two backends are comparable (the judge analog of
      translate-harness's byte-identical translation prompt).
    - :meth:`parse_response` maps a raw judge response to an
      :class:`EvalResult`. Both backends call it, so a persisted result looks
      the same regardless of who produced the raw text.
    - :meth:`run` is the API backend: build → call → parse, plus any retry.

    A judge that implements :meth:`build_prompt` + :meth:`parse_response` gets
    the subagent backend for free (see ``src/judges/subagent.py``).
    """

    spec: JudgeSpec

    #: Optional template (a filename in ``prompts/``) for a *batched* subagent
    #: prompt that judges several targets in one worker. A judge sets this only
    #: if its :meth:`shared_prompt_variables` are target-independent (so the
    #: shared block can be rendered once, followed by one ``<item>`` per target).
    #: Judges that leave it ``None`` are always run one target per worker. The
    #: subagent backend uses this for density-gated target grouping.
    batch_template: Optional[str] = None

    @abstractmethod
    def run(self, target: JudgeTarget, context: dict[str, Any]) -> EvalResult:
        """Evaluate ``target`` via the API backend and return an :class:`EvalResult`."""

    def shared_prompt_variables(self, context: dict[str, Any]) -> dict[str, str]:
        """Prompt variables that are the SAME for every target.

        These are safe to render once at the top of a batched prompt (e.g. the
        dialogue judge's house rules). Default: none. Override for judge-wide,
        target-independent inputs.
        """
        return {}

    def item_prompt_variables(
        self, target: JudgeTarget, context: dict[str, Any]
    ) -> dict[str, str]:
        """Per-target prompt variables (the source + translation under test).

        These vary from target to target and become one ``<item>`` block in a
        batched prompt. Override to add judge-specific per-target inputs.
        """
        return {
            "source_text": target.source_text,
            "translation_text": target.translated_text,
        }

    def prompt_variables(self, target: JudgeTarget, context: dict[str, Any]) -> dict[str, str]:
        """Template variables for :meth:`build_prompt` (the solo prompt).

        The union of the shared (judge-wide) and per-item (per-target) variables,
        so each input is defined in one place and the solo and batched prompts
        stay in sync. Override only if a judge needs a different assembly.
        """
        return {
            **self.shared_prompt_variables(context),
            **self.item_prompt_variables(target, context),
        }

    def build_prompt(self, target: JudgeTarget, context: dict[str, Any]) -> str:
        """Render the judge's prompt.

        Shared by the API path (:meth:`run`) and the subagent backend (which
        writes the result to a file for a worker), so both send byte-identical
        prompts. Override only if a judge needs non-template prompt assembly.
        """
        from src.judges import llm_io  # local import avoids any import-order coupling

        template = llm_io.load_template(self.spec.template)
        return llm_io.render(template, self.prompt_variables(target, context))

    def build_batch_prompt(
        self, targets: list[JudgeTarget], context: dict[str, Any]
    ) -> str:
        """Render one prompt covering several targets for a single worker.

        Requires :attr:`batch_template`. The shared block is rendered once; each
        target becomes an ``<item id=...>`` block filling the template's
        ``{{items}}`` slot. The subagent backend splits the worker's ``verdicts``
        object back out per target and feeds each through :meth:`parse_response`
        — the same parser the solo path uses — so a batched result is identical
        to a solo one.
        """
        if not self.batch_template:
            raise NotImplementedError(
                f"{type(self).__name__} has no batch_template; it cannot build a "
                "batched prompt. Run it one target per worker."
            )
        from src.judges import llm_io  # local import avoids any import-order coupling

        template = llm_io.load_template(self.batch_template)
        items = "\n\n".join(
            _batch_item_block(target.id, self.item_prompt_variables(target, context))
            for target in targets
        )
        variables = {**self.shared_prompt_variables(context), "items": items}
        return llm_io.render(template, variables)

    def parse_response(
        self, target: JudgeTarget, raw: str, context: dict[str, Any]
    ) -> EvalResult:
        """Map a raw judge response to an :class:`EvalResult`.

        Both backends call this. It must raise
        :class:`~src.judges.llm_io.JudgeParseError` on unparseable output so each
        backend can decide what to do: the API path retries with a stricter
        suffix; the subagent ``commit`` marks the draft failed for re-spawn.

        Override per judge. The default raises so a judge that has not yet
        implemented it fails loudly rather than silently producing empty results.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement parse_response(); implement "
            "it (and build_prompt()) to support both the API and subagent backends."
        )

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
