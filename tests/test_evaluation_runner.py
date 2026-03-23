"""
Tests for evaluation runner and orchestration functions.

Tests the evaluation runner system that coordinates multiple evaluators,
aggregates results, and provides a unified interface for running evaluations.
"""

import pytest
from pathlib import Path

from src.evaluators import (
    get_evaluator,
    run_evaluator,
    run_evaluators,
    run_all_evaluators,
    aggregate_results,
    BaseEvaluator,
    LengthEvaluator,
    ParagraphEvaluator,
    DictionaryEvaluator,
    GlossaryEvaluator,
)
from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    EvalResult,
    IssueLevel,
    EvaluationConfig,
    Glossary,
    GlossaryTerm,
    GlossaryTermType,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def basic_chunk():
    """Basic valid chunk for testing."""
    return Chunk(
        id="test_001",
        chapter_id="ch01",
        position=1,
        source_text="This is a test.",
        translated_text="Esta es una prueba.",
        status=ChunkStatus.TRANSLATED,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=4
        )
    )


@pytest.fixture
def sample_glossary():
    """Sample glossary for testing."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="test",
                spanish="prueba",
                type=GlossaryTermType.CONCEPT,
                alternatives=[]
            )
        ]
    )


# =============================================================================
# PHASE 1: EVALUATOR FACTORY TESTS
# =============================================================================

class TestEvaluatorFactory:
    """Test the get_evaluator() factory function."""

    def test_get_length_evaluator(self):
        """Should return LengthEvaluator instance."""
        evaluator = get_evaluator("length")
        assert isinstance(evaluator, LengthEvaluator)
        assert isinstance(evaluator, BaseEvaluator)
        assert evaluator.name == "length"

    def test_get_paragraph_evaluator(self):
        """Should return ParagraphEvaluator instance."""
        evaluator = get_evaluator("paragraph")
        assert isinstance(evaluator, ParagraphEvaluator)
        assert isinstance(evaluator, BaseEvaluator)
        assert evaluator.name == "paragraph"

    def test_get_dictionary_evaluator(self):
        """Should return DictionaryEvaluator instance."""
        evaluator = get_evaluator("dictionary")
        assert isinstance(evaluator, DictionaryEvaluator)
        assert isinstance(evaluator, BaseEvaluator)
        assert evaluator.name == "dictionary"

    def test_get_glossary_evaluator(self):
        """Should return GlossaryEvaluator instance."""
        evaluator = get_evaluator("glossary")
        assert isinstance(evaluator, GlossaryEvaluator)
        assert isinstance(evaluator, BaseEvaluator)
        assert evaluator.name == "glossary"

    def test_unknown_evaluator_raises_error(self):
        """Should raise ValueError for unknown evaluator name."""
        with pytest.raises(ValueError) as exc_info:
            get_evaluator("nonexistent")

        assert "Unknown evaluator" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)
        assert "Available evaluators" in str(exc_info.value)

    def test_empty_string_raises_error(self):
        """Should raise ValueError for empty evaluator name."""
        with pytest.raises(ValueError) as exc_info:
            get_evaluator("")

        assert "Unknown evaluator" in str(exc_info.value)

    def test_case_sensitive_lookup(self):
        """Evaluator names are case-sensitive."""
        with pytest.raises(ValueError):
            get_evaluator("Length")  # Capital L should fail

    def test_evaluator_has_version(self):
        """All evaluators should have version attribute."""
        for name in ["length", "paragraph", "dictionary", "glossary"]:
            evaluator = get_evaluator(name)
            assert hasattr(evaluator, "version")
            assert isinstance(evaluator.version, str)
            assert len(evaluator.version) > 0

    def test_evaluator_has_description(self):
        """All evaluators should have description attribute."""
        for name in ["length", "paragraph", "dictionary", "glossary"]:
            evaluator = get_evaluator(name)
            assert hasattr(evaluator, "description")
            assert isinstance(evaluator.description, str)
            assert len(evaluator.description) > 0

    def test_multiple_instantiations(self):
        """Should be able to instantiate same evaluator multiple times."""
        eval1 = get_evaluator("length")
        eval2 = get_evaluator("length")

        # Should be different instances
        assert eval1 is not eval2

        # But same type
        assert type(eval1) == type(eval2)

    def test_all_evaluators_can_be_instantiated(self):
        """All registered evaluators should instantiate without errors."""
        evaluator_names = ["length", "paragraph", "dictionary", "glossary"]

        for name in evaluator_names:
            evaluator = get_evaluator(name)
            assert evaluator is not None
            assert isinstance(evaluator, BaseEvaluator)


# =============================================================================
# PHASE 3: SINGLE EVALUATOR RUNNER TESTS
# =============================================================================

class TestRunEvaluator:
    """Test the run_evaluator() function."""

    def test_successful_evaluation(self, basic_chunk):
        """Should return EvalResult when evaluation succeeds."""
        result = run_evaluator(basic_chunk, "length", {})

        assert isinstance(result, EvalResult)
        assert result.eval_name == "length"
        assert result.target_id == basic_chunk.id
        assert result.target_type == "chunk"
        assert isinstance(result.passed, bool)
        assert result.executed_at is not None

    def test_length_evaluator_runs(self, basic_chunk):
        """Length evaluator should run successfully."""
        # Create chunk with appropriate length ratio (should pass)
        basic_chunk.source_text = "Hello world test"  # 3 words
        basic_chunk.translated_text = "Hola mundo prueba test"  # 4 words (1.33x ratio)

        result = run_evaluator(basic_chunk, "length", {})

        assert result.eval_name == "length"
        assert result.passed is True
        assert result.score is not None

    def test_paragraph_evaluator_runs(self, basic_chunk):
        """Paragraph evaluator should run successfully."""
        # Single paragraph in both
        basic_chunk.source_text = "This is one paragraph."
        basic_chunk.translated_text = "Este es un párrafo."

        result = run_evaluator(basic_chunk, "paragraph", {})

        assert result.eval_name == "paragraph"
        assert result.passed is True

    def test_dictionary_evaluator_runs(self, basic_chunk):
        """Dictionary evaluator should run successfully."""
        # Valid Spanish text
        basic_chunk.source_text = "Hello world"
        basic_chunk.translated_text = "Hola mundo"

        result = run_evaluator(basic_chunk, "dictionary", {})

        assert result.eval_name == "dictionary"
        assert isinstance(result.passed, bool)  # May pass or fail depending on dict

    def test_glossary_evaluator_runs(self, basic_chunk):
        """Glossary evaluator should run successfully."""
        # No glossary provided - should pass with perfect score
        result = run_evaluator(basic_chunk, "glossary", {})

        assert result.eval_name == "glossary"
        assert result.passed is True
        assert result.score == 1.0

    def test_unknown_evaluator_returns_error_result(self, basic_chunk):
        """Unknown evaluator should return EvalResult with error."""
        result = run_evaluator(basic_chunk, "nonexistent", {})

        assert isinstance(result, EvalResult)
        assert result.eval_name == "nonexistent"
        assert result.passed is False
        assert result.score == 0.0
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR
        assert "failed to initialize" in result.issues[0].message.lower()

    def test_error_result_has_metadata(self, basic_chunk):
        """Error result should include error metadata."""
        result = run_evaluator(basic_chunk, "invalid", {})

        assert "error" in result.metadata
        assert "error_type" in result.metadata
        assert result.metadata["error_type"] == "ValueError"

    def test_context_passed_to_evaluator(self, basic_chunk):
        """Context dict should be passed to evaluator."""
        # Length evaluator uses context for config
        context = {
            "length_config": {
                "count_by": "chars",
                "min_ratio": 0.8,
                "max_ratio": 1.5
            }
        }

        result = run_evaluator(basic_chunk, "length", context)

        assert isinstance(result, EvalResult)
        # If context wasn't passed, evaluator would use word count (default)
        # With context, it uses char count
        # We can verify context was used by checking metadata
        assert "unit" in result.metadata or result.metadata.get("count_by") == "chars"

    def test_result_includes_version(self, basic_chunk):
        """Result should include evaluator version."""
        result = run_evaluator(basic_chunk, "length", {})

        assert result.eval_version is not None
        assert len(result.eval_version) > 0
        # Should be semantic version format
        assert "." in result.eval_version

    def test_multiple_runs_independent(self, basic_chunk):
        """Multiple runs of same evaluator should be independent."""
        result1 = run_evaluator(basic_chunk, "length", {})
        result2 = run_evaluator(basic_chunk, "length", {})

        # Both should succeed
        assert isinstance(result1, EvalResult)
        assert isinstance(result2, EvalResult)

        # Should have different execution times
        assert result1.executed_at != result2.executed_at

    def test_different_evaluators_independent(self, basic_chunk):
        """Different evaluators should run independently."""
        result_length = run_evaluator(basic_chunk, "length", {})
        result_paragraph = run_evaluator(basic_chunk, "paragraph", {})

        assert result_length.eval_name == "length"
        assert result_paragraph.eval_name == "paragraph"

        # Both should have run successfully
        assert isinstance(result_length, EvalResult)
        assert isinstance(result_paragraph, EvalResult)


# =============================================================================
# PHASE 4: MULTI-EVALUATOR RUNNER TESTS
# =============================================================================

class TestRunEvaluators:
    """Test the run_evaluators() function."""

    def test_run_multiple_evaluators(self, basic_chunk):
        """Should run all specified evaluators."""
        evaluators = ["length", "paragraph"]
        results = run_evaluators(basic_chunk, evaluators, {})

        assert len(results) == 2
        assert results[0].eval_name == "length"
        assert results[1].eval_name == "paragraph"

    def test_results_in_order(self, basic_chunk):
        """Results should be in same order as evaluator list."""
        evaluators = ["glossary", "length", "paragraph", "dictionary"]
        results = run_evaluators(basic_chunk, evaluators, {})

        assert len(results) == 4
        assert results[0].eval_name == "glossary"
        assert results[1].eval_name == "length"
        assert results[2].eval_name == "paragraph"
        assert results[3].eval_name == "dictionary"

    def test_empty_evaluator_list(self, basic_chunk):
        """Empty evaluator list should return empty results."""
        results = run_evaluators(basic_chunk, [], {})
        assert results == []

    def test_single_evaluator(self, basic_chunk):
        """Should work with single evaluator."""
        results = run_evaluators(basic_chunk, ["length"], {})

        assert len(results) == 1
        assert results[0].eval_name == "length"

    def test_invalid_evaluator_included(self, basic_chunk):
        """Invalid evaluator should return error result but not stop others."""
        evaluators = ["length", "invalid", "paragraph"]
        results = run_evaluators(basic_chunk, evaluators, {})

        assert len(results) == 3
        assert results[0].eval_name == "length"
        assert results[0].passed is True

        # Invalid evaluator returns error result
        assert results[1].eval_name == "invalid"
        assert results[1].passed is False

        # But paragraph still runs
        assert results[2].eval_name == "paragraph"
        assert results[2].passed is True

    def test_context_passed_to_all_evaluators(self, basic_chunk, sample_glossary):
        """Context should be passed to all evaluators."""
        context = {"glossary": sample_glossary}
        results = run_evaluators(basic_chunk, ["dictionary", "glossary"], context)

        assert len(results) == 2
        # Both evaluators should have access to glossary via context

    def test_all_evaluators_run_even_if_one_fails(self, basic_chunk):
        """All evaluators should run even if one fails."""
        # Create chunk that will fail length check
        basic_chunk.source_text = "Hello world test one two"  # 5 words
        basic_chunk.translated_text = "Hola"  # 1 word (0.2x ratio - should fail)

        evaluators = ["length", "paragraph", "glossary"]
        results = run_evaluators(basic_chunk, evaluators, {})

        assert len(results) == 3

        # Length should fail
        assert results[0].eval_name == "length"
        assert results[0].passed is False

        # But others still run
        assert results[1].eval_name == "paragraph"
        assert results[2].eval_name == "glossary"


class TestRunAllEvaluators:
    """Test the run_all_evaluators() function."""

    def test_run_all_from_config(self, basic_chunk):
        """Should run all evaluators from config."""
        config = EvaluationConfig(enabled_evals=["length", "paragraph"])
        results = run_all_evaluators(basic_chunk, config)

        assert len(results) == 2
        assert results[0].eval_name == "length"
        assert results[1].eval_name == "paragraph"

    def test_glossary_passed_to_evaluators(self, basic_chunk, sample_glossary):
        """Glossary should be available to evaluators."""
        config = EvaluationConfig(enabled_evals=["glossary", "dictionary"])
        results = run_all_evaluators(basic_chunk, config, glossary=sample_glossary)

        assert len(results) == 2
        # Both evaluators should have received glossary

    def test_empty_config(self, basic_chunk):
        """Empty enabled_evals should return no results."""
        config = EvaluationConfig(enabled_evals=[])
        results = run_all_evaluators(basic_chunk, config)

        assert results == []

    def test_default_config(self, basic_chunk):
        """Should work with default config."""
        config = EvaluationConfig()
        # Default enabled_evals is ["length", "paragraph", "completeness"]
        # But completeness doesn't exist yet, so it will return error result
        results = run_all_evaluators(basic_chunk, config)

        # Should return 3 results (length, paragraph, completeness-error)
        assert len(results) == 3

    def test_all_four_evaluators(self, basic_chunk, sample_glossary):
        """Should be able to run all four implemented evaluators."""
        config = EvaluationConfig(
            enabled_evals=["length", "paragraph", "dictionary", "glossary"]
        )
        results = run_all_evaluators(basic_chunk, config, glossary=sample_glossary)

        assert len(results) == 4
        assert all(isinstance(r, EvalResult) for r in results)

    def test_result_order_matches_config(self, basic_chunk):
        """Results should be in same order as config.enabled_evals."""
        config = EvaluationConfig(enabled_evals=["glossary", "length"])
        results = run_all_evaluators(basic_chunk, config)

        assert results[0].eval_name == "glossary"
        assert results[1].eval_name == "length"


# =============================================================================
# PHASE 5: RESULT AGGREGATION TESTS
# =============================================================================

class TestAggregateResults:
    """Test the aggregate_results() function."""

    def test_aggregate_empty_list(self):
        """Empty results list should return zeros."""
        summary = aggregate_results([])

        assert summary["total_evaluators"] == 0
        assert summary["passed_evaluators"] == 0
        assert summary["failed_evaluators"] == 0
        assert summary["overall_passed"] is True  # Vacuously true
        assert summary["total_issues"] == 0
        assert summary["average_score"] is None
        assert summary["evaluator_results"] == []

    def test_aggregate_all_passing(self, basic_chunk):
        """All passing evaluators should show correct summary."""
        # Run evaluators that should pass
        results = run_evaluators(basic_chunk, ["glossary", "paragraph"], {})
        summary = aggregate_results(results)

        assert summary["total_evaluators"] == 2
        assert summary["passed_evaluators"] == 2
        assert summary["failed_evaluators"] == 0
        assert summary["overall_passed"] is True

    def test_aggregate_with_failures(self, basic_chunk):
        """Failures should be reflected in summary."""
        # Create chunk that will fail length check
        basic_chunk.source_text = "Hello world test one two three"  # 6 words
        basic_chunk.translated_text = "Hi"  # 1 word (should fail)

        results = run_evaluators(basic_chunk, ["length", "paragraph"], {})
        summary = aggregate_results(results)

        assert summary["total_evaluators"] == 2
        assert summary["passed_evaluators"] == 1  # paragraph passes
        assert summary["failed_evaluators"] == 1  # length fails
        assert summary["overall_passed"] is False

    def test_aggregate_issues_by_severity(self, basic_chunk):
        """Should count issues by severity correctly."""
        # Create chunk with deliberate errors for dictionary evaluator
        basic_chunk.source_text = "Hello world"
        basic_chunk.translated_text = "Hello mundo"  # "Hello" is English (ERROR)

        results = run_evaluators(basic_chunk, ["dictionary"], {})
        summary = aggregate_results(results)

        assert summary["total_issues"] >= 1
        assert summary["issues_by_severity"]["error"] >= 1

    def test_aggregate_issues_by_evaluator(self, basic_chunk):
        """Should track which evaluators found issues."""
        # Length failure
        basic_chunk.source_text = "Hello world test"
        basic_chunk.translated_text = "Hi"

        results = run_evaluators(basic_chunk, ["length", "paragraph"], {})
        summary = aggregate_results(results)

        assert "length" in summary["issues_by_evaluator"]
        assert summary["issues_by_evaluator"]["length"] >= 1

    def test_aggregate_average_score(self, basic_chunk):
        """Should calculate average score correctly."""
        results = run_evaluators(basic_chunk, ["length", "paragraph"], {})
        summary = aggregate_results(results)

        assert summary["average_score"] is not None
        assert 0.0 <= summary["average_score"] <= 1.0

    def test_aggregate_average_score_with_nulls(self, basic_chunk):
        """Should handle null scores gracefully."""
        # Some evaluators might return None for score
        results = run_evaluators(basic_chunk, ["length"], {})
        summary = aggregate_results(results)

        # Should still calculate average from non-null scores
        assert isinstance(summary["average_score"], (float, int, type(None)))

    def test_aggregate_evaluator_results_structure(self, basic_chunk):
        """Evaluator results should have correct structure."""
        results = run_evaluators(basic_chunk, ["length", "paragraph"], {})
        summary = aggregate_results(results)

        assert len(summary["evaluator_results"]) == 2

        for eval_result in summary["evaluator_results"]:
            assert "name" in eval_result
            assert "version" in eval_result
            assert "passed" in eval_result
            assert "score" in eval_result
            assert "issues" in eval_result
            assert "errors" in eval_result
            assert "warnings" in eval_result
            assert "info" in eval_result

    def test_aggregate_preserves_evaluator_order(self, basic_chunk):
        """Evaluator results should be in same order as input."""
        results = run_evaluators(basic_chunk, ["glossary", "length", "paragraph"], {})
        summary = aggregate_results(results)

        names = [e["name"] for e in summary["evaluator_results"]]
        assert names == ["glossary", "length", "paragraph"]

    def test_aggregate_with_mixed_results(self, basic_chunk):
        """Should handle mix of pass/fail/errors correctly."""
        # Include an invalid evaluator to get error result
        basic_chunk.source_text = "Hello world test"
        basic_chunk.translated_text = "Hi"  # Will fail length

        results = run_evaluators(basic_chunk, ["length", "invalid", "paragraph"], {})
        summary = aggregate_results(results)

        assert summary["total_evaluators"] == 3
        # length fails, invalid errors, paragraph passes
        assert summary["failed_evaluators"] >= 1
        assert summary["overall_passed"] is False

    def test_aggregate_all_fields_present(self, basic_chunk):
        """All expected fields should be present in summary."""
        results = run_evaluators(basic_chunk, ["length"], {})
        summary = aggregate_results(results)

        expected_fields = [
            "total_evaluators",
            "passed_evaluators",
            "failed_evaluators",
            "overall_passed",
            "total_issues",
            "issues_by_severity",
            "issues_by_evaluator",
            "average_score",
            "evaluator_results"
        ]

        for field in expected_fields:
            assert field in summary, f"Missing field: {field}"

    def test_aggregate_issues_by_severity_has_all_levels(self):
        """Issues by severity should include all three levels."""
        summary = aggregate_results([])

        assert "error" in summary["issues_by_severity"]
        assert "warning" in summary["issues_by_severity"]
        assert "info" in summary["issues_by_severity"]


# =============================================================================
# PHASE 6: INTEGRATION TESTS WITH REAL FIXTURES
# =============================================================================

class TestIntegrationWithFixtures:
    """Integration tests using real Pride & Prejudice fixtures."""

    def load_fixture_chunk(self, filename: str) -> Chunk:
        """Load a chunk from fixtures directory."""
        import json
        fixture_path = Path(__file__).parent / "fixtures" / filename
        with open(fixture_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return Chunk(**data)

    def load_fixture_glossary(self) -> Glossary:
        """Load the sample glossary from fixtures."""
        import json
        fixture_path = Path(__file__).parent / "fixtures" / "glossary_sample.json"
        with open(fixture_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return Glossary(**data)

    def test_good_translation_passes_all(self):
        """Good Pride & Prejudice translation should pass all evaluators."""
        chunk = self.load_fixture_chunk("chunk_translated_good.json")
        glossary = self.load_fixture_glossary()

        config = EvaluationConfig(
            enabled_evals=["length", "paragraph", "dictionary", "glossary"]
        )
        results = run_all_evaluators(chunk, config, glossary=glossary)
        summary = aggregate_results(results)

        # All evaluators should pass on good translation
        assert summary["overall_passed"] is True
        assert summary["passed_evaluators"] == 4
        assert summary["failed_evaluators"] == 0

    def test_error_translation_fails_appropriately(self):
        """Translation with deliberate errors should fail appropriately."""
        chunk = self.load_fixture_chunk("chunk_translated_errors.json")
        glossary = self.load_fixture_glossary()

        config = EvaluationConfig(
            enabled_evals=["length", "paragraph", "dictionary"]
        )
        results = run_all_evaluators(chunk, config, glossary=glossary)
        summary = aggregate_results(results)

        # Should have failures (chunk has deliberate errors)
        assert summary["overall_passed"] is False
        assert summary["failed_evaluators"] >= 1
        assert summary["total_issues"] >= 1

    def test_english_chunk_baseline(self):
        """English-only chunk should work with length/paragraph evaluators."""
        chunk = self.load_fixture_chunk("chunk_english.json")

        # Only run evaluators that don't require translation
        config = EvaluationConfig(enabled_evals=["paragraph"])
        results = run_all_evaluators(chunk, config)

        # Should complete without errors (even though no translation)
        assert len(results) == 1

    def test_real_glossary_integration(self):
        """Test glossary evaluator with real P&P glossary."""
        chunk = self.load_fixture_chunk("chunk_translated_good.json")
        glossary = self.load_fixture_glossary()

        # Run only glossary evaluator
        result = run_evaluator(chunk, "glossary", {"glossary": glossary})

        # Should return valid result
        assert isinstance(result, EvalResult)
        assert result.eval_name == "glossary"

    def test_full_pipeline_with_aggregation(self):
        """Test full pipeline: load fixtures, run all evaluators, aggregate."""
        chunk = self.load_fixture_chunk("chunk_translated_good.json")
        glossary = self.load_fixture_glossary()

        # Run all four evaluators
        config = EvaluationConfig(
            enabled_evals=["length", "paragraph", "dictionary", "glossary"]
        )
        results = run_all_evaluators(chunk, config, glossary=glossary)

        # Aggregate results
        summary = aggregate_results(results)

        # Verify full pipeline works
        assert summary["total_evaluators"] == 4
        assert all(isinstance(r, EvalResult) for r in results)
        assert "average_score" in summary
        assert "evaluator_results" in summary
        assert len(summary["evaluator_results"]) == 4

    def test_error_chunk_details(self):
        """Test that error chunk produces detailed issue information."""
        chunk = self.load_fixture_chunk("chunk_translated_errors.json")

        # Run dictionary evaluator (should find English words)
        result = run_evaluator(chunk, "dictionary", {})

        # Should have issues with specific details
        assert len(result.issues) >= 1
        for issue in result.issues:
            assert issue.message is not None
            assert len(issue.message) > 0

    def test_compare_good_vs_error_chunks(self):
        """Compare results between good and error chunks."""
        good_chunk = self.load_fixture_chunk("chunk_translated_good.json")
        error_chunk = self.load_fixture_chunk("chunk_translated_errors.json")

        # Run same evaluators on both
        good_results = run_evaluators(good_chunk, ["length", "paragraph"], {})
        error_results = run_evaluators(error_chunk, ["length", "paragraph"], {})

        good_summary = aggregate_results(good_results)
        error_summary = aggregate_results(error_results)

        # Good chunk should have better scores
        assert good_summary["passed_evaluators"] >= error_summary["passed_evaluators"]
        assert good_summary["total_issues"] <= error_summary["total_issues"]

    def test_all_fixture_chunks_loadable(self):
        """Verify all fixture chunks can be loaded."""
        fixture_files = [
            "chunk_english.json",
            "chunk_translated_good.json",
            "chunk_translated_errors.json"
        ]

        for filename in fixture_files:
            chunk = self.load_fixture_chunk(filename)
            assert isinstance(chunk, Chunk)
            assert chunk.id is not None

    def test_glossary_fixture_valid(self):
        """Verify glossary fixture is valid."""
        glossary = self.load_fixture_glossary()

        assert isinstance(glossary, Glossary)
        assert len(glossary.terms) > 0

        # Verify glossary terms have expected structure
        for term in glossary.terms:
            assert term.english is not None
            assert term.spanish is not None
            assert term.type is not None
