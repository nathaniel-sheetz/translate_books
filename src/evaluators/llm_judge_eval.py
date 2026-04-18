"""
LLM Judge evaluator — wraps ``judge_absolute()`` into the BaseEvaluator
interface so it can slot into the existing ``run_all_evaluators`` pipeline.

Score normalization: the judge produces 1-5 per dimension.  The normalized
score ``(avg(dims) - 1) / 4`` maps to the [0.0, 1.0] range required by
``EvalResult.score``.
"""

import logging
from pathlib import Path
from typing import Any, Optional

from ..evaluators.base import BaseEvaluator
from ..models import Chunk, EvalResult, JudgeScore

logger = logging.getLogger(__name__)


class LLMJudgeEvaluator(BaseEvaluator):
    """Evaluator that uses an LLM judge for absolute translation scoring.

    Context keys consumed:
        ``style_json_path`` (Path | None): Path to the project's style.json.
        ``coded_eval_results`` (list[EvalResult] | None): Prior coded evaluator
            results to feed the judge as context signals.
        ``judge_provider`` (str | None): LLM provider override.
        ``judge_model`` (str | None): LLM model override.
    """

    name: str = "llm_judge"
    version: str = "1.0.0"
    description: str = "LLM-based translation quality judge (absolute scoring)"

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """Run absolute LLM judge scoring on a translated chunk.

        Args:
            chunk: Chunk with ``translated_text`` to evaluate.
            context: Dict with optional keys ``style_json_path``,
                ``coded_eval_results``, ``judge_provider``, ``judge_model``.

        Returns:
            EvalResult with normalized score and per-dimension metadata.

        Raises:
            ValueError: If chunk has no translated text.
        """
        if not chunk.translated_text:
            raise ValueError(f"Chunk {chunk.id} has no translated text to evaluate.")

        # Import here to avoid circular imports at module level
        from ..judge import judge_absolute, JudgeParseError

        style_json_path: Optional[Path] = context.get("style_json_path")
        coded_eval_results: Optional[list[EvalResult]] = context.get("coded_eval_results")
        judge_provider: Optional[str] = context.get("judge_provider")
        judge_model: Optional[str] = context.get("judge_model")

        try:
            score: JudgeScore = judge_absolute(
                source_text=chunk.source_text,
                translation_text=chunk.translated_text,
                style_json_path=style_json_path,
                coded_eval_results=coded_eval_results,
                judge_provider=judge_provider,
                judge_model=judge_model,
            )
        except JudgeParseError as exc:
            logger.error("Judge parse error on chunk %s: %s", chunk.id, exc)
            return self.create_result(
                chunk,
                issues=[self.create_issue(
                    severity="error",
                    message=f"LLM judge returned unparseable response: {exc}",
                    location=chunk.id,
                )],
                score=None,
                metadata={"error": str(exc)},
            )

        metadata = {
            "fluency": score.fluency,
            "fidelity": score.fidelity,
            "regional": score.regional,
            "voice": score.voice,
            "rationale": score.rationale,
        }

        return self.create_result(
            chunk,
            issues=[],
            score=score.normalized_score,
            metadata=metadata,
        )
