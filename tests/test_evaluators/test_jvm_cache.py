"""
Tests for LanguageTool JVM caching in evaluator registry.

Verifies that get_evaluator('grammar', dialect) returns the same cached
instance on repeated calls, avoiding repeated JVM startups.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.evaluators import get_evaluator, _lt_cache


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the LT cache before and after each test."""
    _lt_cache.clear()
    yield
    _lt_cache.clear()


@patch("src.evaluators.grammar_eval.language_tool_python")
@patch("src.evaluators.grammar_eval.LANGUAGETOOL_AVAILABLE", True)
def test_jvm_cache_returns_same_instance(mock_lt):
    """Second get_evaluator('grammar', dialect) returns the same instance."""
    mock_lt.LanguageTool.return_value = MagicMock()

    first = get_evaluator("grammar", dialect="es-MX")
    second = get_evaluator("grammar", dialect="es-MX")

    assert first is second
    # LanguageTool constructor called only once for this dialect
    mock_lt.LanguageTool.assert_called_once_with("es-MX")


@patch("src.evaluators.grammar_eval.language_tool_python")
@patch("src.evaluators.grammar_eval.LANGUAGETOOL_AVAILABLE", True)
def test_jvm_cache_different_dialects_are_separate(mock_lt):
    """Different dialects get separate cached instances."""
    mock_lt.LanguageTool.return_value = MagicMock()

    es = get_evaluator("grammar", dialect="es")
    es_mx = get_evaluator("grammar", dialect="es-MX")

    assert es is not es_mx
    assert mock_lt.LanguageTool.call_count == 2


@patch("src.evaluators.grammar_eval.language_tool_python")
@patch("src.evaluators.grammar_eval.LANGUAGETOOL_AVAILABLE", True)
def test_jvm_cache_default_dialect(mock_lt):
    """Calling without dialect uses 'es' as cache key."""
    mock_lt.LanguageTool.return_value = MagicMock()

    first = get_evaluator("grammar")
    second = get_evaluator("grammar")

    assert first is second
    assert "es" in _lt_cache


def test_non_grammar_evaluators_not_cached():
    """Non-grammar evaluators are not affected by the cache."""
    first = get_evaluator("length")
    second = get_evaluator("length")

    # Length evaluators are cheap; no caching, separate instances
    assert first is not second
    assert len(_lt_cache) == 0
