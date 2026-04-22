"""Evaluators for translation quality assessment."""

from typing import Optional, Any
import logging
import traceback
from datetime import datetime

from .base import BaseEvaluator
from .length_eval import LengthEvaluator
from .paragraph_eval import ParagraphEvaluator
from .dictionary_eval import DictionaryEvaluator
from .glossary_eval import GlossaryEvaluator
from .completeness_eval import CompletenessEvaluator
from .blacklist_eval import BlacklistEvaluator
from .grammar_eval import GrammarEvaluator

# Import models for type hints and result creation
from ..models import Chunk, EvalResult, Issue, IssueLevel, EvaluationConfig, Glossary, Blacklist
from ..app_config import get_length_config, get_blacklist_path
from ..utils.file_io import load_blacklist

# Module logger
logger = logging.getLogger(__name__)

# Registry mapping evaluator names to classes
_EVALUATOR_REGISTRY: dict[str, type[BaseEvaluator]] = {
    "length": LengthEvaluator,
    "paragraph": ParagraphEvaluator,
    "dictionary": DictionaryEvaluator,
    "glossary": GlossaryEvaluator,
    "completeness": CompletenessEvaluator,
    "blacklist": BlacklistEvaluator,
    "grammar": GrammarEvaluator,
}

# Cache LanguageTool JVM instances by dialect to avoid repeated JVM startups
_lt_cache: dict[str, GrammarEvaluator] = {}


def get_evaluator(name: str, dialect: Optional[str] = None) -> BaseEvaluator:
    """
    Get evaluator instance by name.

    Args:
        name: Evaluator name (e.g., "length", "paragraph", "dictionary", "glossary", "completeness", "blacklist", "grammar")
        dialect: Optional dialect for grammar evaluator (e.g., "es", "es-MX"). Cached by dialect to avoid repeated JVM startups.

    Returns:
        Initialized evaluator instance

    Raises:
        ValueError: If evaluator name is unknown or initialization fails

    Example:
        >>> evaluator = get_evaluator("length")
        >>> result = evaluator.evaluate(chunk, context)
    """
    if name not in _EVALUATOR_REGISTRY:
        available = ", ".join(_EVALUATOR_REGISTRY.keys())
        raise ValueError(
            f"Unknown evaluator: '{name}'. "
            f"Available evaluators: {available}"
        )

    # Return cached GrammarEvaluator if available
    if name == "grammar":
        cache_key = dialect or "es"
        if cache_key in _lt_cache:
            logger.debug(f"Returning cached grammar evaluator for dialect: {cache_key}")
            return _lt_cache[cache_key]

    evaluator_class = _EVALUATOR_REGISTRY[name]

    try:
        # Instantiate evaluator
        # DictionaryEvaluator may raise RuntimeError if enchant dicts not available
        if name == "grammar" and dialect:
            evaluator = evaluator_class(dialect=dialect)
        else:
            evaluator = evaluator_class()

        # Cache grammar evaluator instances
        if name == "grammar":
            cache_key = dialect or "es"
            _lt_cache[cache_key] = evaluator
            logger.debug(f"Cached grammar evaluator for dialect: {cache_key}")

        logger.debug(f"Initialized evaluator: {name} (v{evaluator.version})")
        return evaluator
    except RuntimeError as e:
        # DictionaryEvaluator initialization failure
        logger.error(f"Failed to initialize {name} evaluator: {e}")
        raise ValueError(
            f"Failed to initialize {name} evaluator: {e}. "
            f"Ensure required dependencies are installed."
        ) from e
    except Exception as e:
        # Unexpected initialization error
        logger.error(f"Unexpected error initializing {name} evaluator: {e}")
        raise ValueError(
            f"Unexpected error initializing {name} evaluator: {e}"
        ) from e


def run_evaluator(
    chunk: Chunk,
    evaluator_name: str,
    context: dict[str, Any]
) -> EvalResult:
    """
    Run a single evaluator on a chunk with error handling.

    If the evaluator raises an exception, this function catches it and returns
    an EvalResult with an ERROR-level issue describing the failure. This allows
    the pipeline to continue even if one evaluator fails.

    Args:
        chunk: Chunk to evaluate
        evaluator_name: Name of evaluator to run (e.g., "length", "paragraph")
        context: Context dict with configuration and resources (glossary, etc.)

    Returns:
        EvalResult from the evaluator, or error result if evaluation failed

    Example:
        >>> result = run_evaluator(chunk, "length", {})
        >>> if result.passed:
        ...     print("Evaluation passed!")
    """
    try:
        # Get evaluator instance (may raise ValueError if unknown/unavailable)
        evaluator = get_evaluator(evaluator_name)

        # Run evaluation (may raise any exception)
        result = evaluator.evaluate(chunk, context)

        logger.debug(
            f"Evaluator '{evaluator_name}' completed: "
            f"passed={result.passed}, score={result.score}, "
            f"issues={len(result.issues)}"
        )

        return result

    except ValueError as e:
        # Evaluator unknown or failed to initialize
        logger.error(f"Failed to get evaluator '{evaluator_name}': {e}")

        # Return error result
        return EvalResult(
            eval_name=evaluator_name,
            eval_version="unknown",
            target_id=chunk.id,
            target_type="chunk",
            passed=False,
            score=0.0,
            issues=[
                Issue(
                    severity=IssueLevel.ERROR,
                    message=f"Evaluator '{evaluator_name}' failed to initialize: {e}",
                    location="evaluator_initialization",
                    suggestion="Check that required dependencies are installed"
                )
            ],
            metadata={
                "error": str(e),
                "error_type": type(e).__name__
            },
            executed_at=datetime.now()
        )

    except Exception as e:
        # Evaluator raised unexpected exception during evaluate()
        logger.error(
            f"Evaluator '{evaluator_name}' raised exception: {e}",
            exc_info=True
        )

        # Capture stack trace for debugging
        tb = traceback.format_exc()

        # Return error result with stack trace in metadata
        return EvalResult(
            eval_name=evaluator_name,
            eval_version="unknown",
            target_id=chunk.id,
            target_type="chunk",
            passed=False,
            score=0.0,
            issues=[
                Issue(
                    severity=IssueLevel.ERROR,
                    message=f"Evaluator '{evaluator_name}' crashed: {e}",
                    location="evaluator_execution",
                    suggestion="This is a bug - check logs for stack trace"
                )
            ],
            metadata={
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": tb
            },
            executed_at=datetime.now()
        )


def _build_context(
    config: EvaluationConfig,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
) -> dict[str, Any]:
    """
    Build context dictionary for evaluators from configuration.

    The context dict is passed to each evaluator's evaluate() method and
    contains configuration options and resources (like glossary) that
    evaluators need.

    Args:
        config: Evaluation configuration with evaluator settings
        glossary: Optional glossary for dictionary/glossary evaluators
        blacklist: Optional blacklist for the blacklist evaluator.
            If not provided, attempts to load from ``blacklist_path``
            in ``app_config.json``.

    Returns:
        Context dict with evaluator-specific configuration

    Example:
        >>> config = EvaluationConfig(enabled_evals=["length"])
        >>> context = _build_context(config, glossary)
        >>> # context contains glossary for use by evaluators
    """
    context: dict[str, Any] = {}

    # Add glossary if provided (used by dictionary and glossary evaluators)
    if glossary is not None:
        context["glossary"] = glossary

    # Add length evaluator config from app_config.json (if present)
    length_cfg = get_length_config()
    if length_cfg:
        context["length_config"] = length_cfg

    # Add blacklist (used by blacklist evaluator)
    if blacklist is not None:
        context["blacklist"] = blacklist
    else:
        bl_path = get_blacklist_path()
        if bl_path and bl_path.exists():
            try:
                context["blacklist"] = load_blacklist(bl_path)
            except Exception as e:
                logger.warning("Failed to load blacklist from %s: %s", bl_path, e)

    return context


def run_evaluators(
    chunk: Chunk,
    evaluators: list[str],
    context: dict[str, Any]
) -> list[EvalResult]:
    """
    Run multiple evaluators on a chunk.

    Evaluators are run sequentially. If one evaluator fails, the others
    continue to run. Each evaluator's result is collected, including error results.

    Args:
        chunk: Chunk to evaluate
        evaluators: List of evaluator names to run (e.g., ["length", "paragraph"])
        context: Context dict with configuration and resources

    Returns:
        List of EvalResult objects, one per evaluator (in same order as input)

    Example:
        >>> results = run_evaluators(chunk, ["length", "paragraph"], {})
        >>> for result in results:
        ...     print(f"{result.eval_name}: {result.passed}")
    """
    results: list[EvalResult] = []

    logger.info(f"Running {len(evaluators)} evaluators on chunk {chunk.id}")

    for evaluator_name in evaluators:
        logger.debug(f"Running evaluator: {evaluator_name}")
        result = run_evaluator(chunk, evaluator_name, context)
        results.append(result)

    logger.info(
        f"Completed {len(evaluators)} evaluators: "
        f"{sum(1 for r in results if r.passed)} passed, "
        f"{sum(1 for r in results if not r.passed)} failed"
    )

    return results


def run_all_evaluators(
    chunk: Chunk,
    config: EvaluationConfig,
    glossary: Optional[Glossary] = None,
    blacklist: Optional[Blacklist] = None,
) -> list[EvalResult]:
    """
    Run all enabled evaluators from configuration.

    This is a convenience function that builds the context from config
    and glossary, then runs all evaluators specified in config.enabled_evals.

    Args:
        chunk: Chunk to evaluate
        config: Evaluation configuration specifying which evaluators to run
        glossary: Optional glossary for dictionary/glossary evaluators
        blacklist: Optional blacklist for the blacklist evaluator.
            Loaded from ``app_config.json`` if not provided.

    Returns:
        List of EvalResult objects from all enabled evaluators

    Example:
        >>> config = EvaluationConfig(enabled_evals=["length", "paragraph"])
        >>> results = run_all_evaluators(chunk, config)
        >>> all_passed = all(r.passed for r in results)
    """
    # Build context from config and glossary
    context = _build_context(config, glossary, blacklist)

    # Run all enabled evaluators
    results = run_evaluators(chunk, config.enabled_evals, context)

    logger.info(
        f"Evaluation complete for chunk {chunk.id}: "
        f"{len(results)} evaluators run"
    )

    return results


def aggregate_results(results: list[EvalResult]) -> dict[str, Any]:
    """
    Aggregate results from multiple evaluators into a summary.

    Provides comprehensive statistics about evaluation results including
    pass/fail counts, issue breakdown, and quality scores.

    Args:
        results: List of EvalResult objects from evaluators

    Returns:
        Dictionary with aggregated statistics:
        - total_evaluators: Number of evaluators run
        - passed_evaluators: Number that passed
        - failed_evaluators: Number that failed
        - overall_passed: True if all evaluators passed
        - total_issues: Total number of issues across all evaluators
        - issues_by_severity: Count of errors, warnings, info
        - issues_by_evaluator: Issue count per evaluator
        - average_score: Average of non-null scores (0.0-1.0)
        - evaluator_results: List of per-evaluator summaries

    Example:
        >>> results = run_evaluators(chunk, ["length", "paragraph"], {})
        >>> summary = aggregate_results(results)
        >>> print(f"Overall: {summary['overall_passed']}")
        >>> print(f"Score: {summary['average_score']:.2f}")
    """
    if not results:
        # Empty results list
        return {
            "total_evaluators": 0,
            "passed_evaluators": 0,
            "failed_evaluators": 0,
            "overall_passed": True,  # Vacuously true
            "total_issues": 0,
            "issues_by_severity": {"error": 0, "warning": 0, "info": 0},
            "issues_by_evaluator": {},
            "average_score": None,
            "evaluator_results": []
        }

    # Count pass/fail
    passed_evaluators = sum(1 for r in results if r.passed)
    failed_evaluators = sum(1 for r in results if not r.passed)
    overall_passed = all(r.passed for r in results)

    # Count issues by severity
    total_errors = sum(r.error_count for r in results)
    total_warnings = sum(r.warning_count for r in results)
    total_info = sum(r.info_count for r in results)
    total_issues = sum(len(r.issues) for r in results)

    # Count issues by evaluator
    issues_by_evaluator = {r.eval_name: len(r.issues) for r in results}

    # Calculate average score (only for non-null scores)
    scores = [r.score for r in results if r.score is not None]
    average_score = sum(scores) / len(scores) if scores else None

    # Create per-evaluator summaries
    evaluator_results = [
        {
            "name": r.eval_name,
            "version": r.eval_version,
            "passed": r.passed,
            "score": r.score,
            "issues": len(r.issues),
            "errors": r.error_count,
            "warnings": r.warning_count,
            "info": r.info_count
        }
        for r in results
    ]

    return {
        "total_evaluators": len(results),
        "passed_evaluators": passed_evaluators,
        "failed_evaluators": failed_evaluators,
        "overall_passed": overall_passed,
        "total_issues": total_issues,
        "issues_by_severity": {
            "error": total_errors,
            "warning": total_warnings,
            "info": total_info
        },
        "issues_by_evaluator": issues_by_evaluator,
        "average_score": round(average_score, 2) if average_score is not None else None,
        "evaluator_results": evaluator_results
    }


__all__ = [
    "BaseEvaluator",
    "LengthEvaluator",
    "ParagraphEvaluator",
    "DictionaryEvaluator",
    "GlossaryEvaluator",
    "CompletenessEvaluator",
    "get_evaluator",
    "run_evaluator",
    "run_evaluators",
    "run_all_evaluators",
    "aggregate_results",
]
