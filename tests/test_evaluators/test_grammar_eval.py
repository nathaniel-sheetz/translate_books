"""
Tests for GrammarEvaluator

Tests the grammar evaluator's ability to detect grammar, spelling, and style
issues using LanguageTool, with proper glossary integration and filtering.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from src.models import Chunk, ChunkMetadata, Glossary, GlossaryTerm, GlossaryTermType, IssueLevel
from src.evaluators.grammar_eval import GrammarEvaluator, LANGUAGETOOL_AVAILABLE

# Skip tests that use patching if LanguageTool not available
# (patching fails when module doesn't exist)
skip_if_no_lt = pytest.mark.skipif(
    not LANGUAGETOOL_AVAILABLE,
    reason="LanguageTool not installed"
)


# Helper to create minimal chunk metadata
def make_metadata():
    return ChunkMetadata(
        char_start=0,
        char_end=100,
        overlap_start=0,
        overlap_end=0,
        paragraph_count=1,
        word_count=10
    )


# Helper to create a mock LanguageTool Match object
def make_mock_match(message, category, rule_id, offset=0, length=5, replacements=None, context=""):
    """Create a mock LanguageTool Match object (3.x snake_case API)."""
    match = Mock()
    match.message = message
    match.category = category
    match.rule_id = rule_id
    match.offset = offset
    match.error_length = length
    match.replacements = replacements or []
    match.context = context
    match.matched_text = context
    return match


class TestGrammarEvaluatorBasic:
    """Basic functionality tests."""

    def test_init_requires_languagetool(self):
        """Test that evaluator requires LanguageTool to be installed."""
        # This test will only run if LanguageTool is actually installed
        try:
            evaluator = GrammarEvaluator()
            assert evaluator.name == "grammar"
            assert evaluator.dialect == "es"
        except RuntimeError as e:
            # If LanguageTool not installed, check error message
            assert "LanguageTool is required" in str(e)

    def test_no_translation_provided(self):
        """Test evaluation when no translation provided."""
        pytest.importorskip("language_tool_python")

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0


@skip_if_no_lt
class TestGrammarChecking:
    """Tests for actual grammar checking (requires LanguageTool or mocks)."""

    @patch('language_tool_python.LanguageTool')
    def test_grammar_error_detected(self, mock_lt_class):
        """Test detection of grammar error."""
        pytest.importorskip("language_tool_python")

        # Setup mock
        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Subject-verb agreement error",
            category="GRAMMAR",
            rule_id="AGREEMENT_ERROR",
            replacements=["fue"]
        )
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="El niño fueron al parque.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert not result.passed  # Has ERROR-level issue
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR
        assert "agreement" in result.issues[0].message.lower()

    @patch('language_tool_python.LanguageTool')
    def test_spelling_error_detected(self, mock_lt_class):
        """Test detection of spelling error."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Possible spelling mistake",
            category="TYPOS",
            rule_id="MORFOLOGIK_RULE_ES",
            replacements=["casa"]
        )
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="La caza es bonita.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert not result.passed
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR  # TYPOS → ERROR

    @patch('language_tool_python.LanguageTool')
    def test_style_issue_detected(self, mock_lt_class):
        """Test detection of style issue."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Redundant repetition",
            category="REDUNDANCY",
            rule_id="REDUNDANCY_RULE",
            replacements=["muy"]
        )
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Es muy muy bueno.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        # Redundancy → INFO, so evaluation still passes
        assert result.passed
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.INFO


@skip_if_no_lt
class TestGlossaryIntegration:
    """Tests for glossary integration."""

    @patch('language_tool_python.LanguageTool')
    def test_glossary_term_not_flagged_for_spelling(self, mock_lt_class):
        """Test that glossary terms are not flagged as spelling errors."""
        pytest.importorskip("language_tool_python")

        # Create match for "Darcy" as spelling error
        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Unknown word: Darcy",
            category="TYPOS",
            rule_id="MORFOLOGIK_RULE_ES",
            context="Darcy"
        )
        mock_match.matched_text = "Darcy"
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="El Sr. Darcy era rico.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Create glossary with Darcy
        glossary = Glossary(terms=[
            GlossaryTerm(english="Darcy", spanish="Darcy", type=GlossaryTermType.CHARACTER)
        ])

        result = evaluator.evaluate(chunk, {"glossary": glossary})

        # Should be ignored because Darcy is in glossary
        assert result.passed
        assert len(result.issues) == 0

    @patch('language_tool_python.LanguageTool')
    def test_glossary_filters_spanish_term(self, mock_lt_class):
        """Test that glossary filters Spanish terms correctly (not just English-Spanish matches)."""
        pytest.importorskip("language_tool_python")

        # Create match for "magia" as spelling error
        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Unknown word: magia",
            category="TYPOS",
            rule_id="MORFOLOGIK_RULE_ES",
            context="magia"
        )
        mock_match.matched_text = "magia"
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="La magia era poderosa.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Create glossary with magic -> magia
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT)
        ])

        result = evaluator.evaluate(chunk, {"glossary": glossary})

        # Should be ignored because "magia" is the Spanish translation in glossary
        assert result.passed
        assert len(result.issues) == 0

    @patch('language_tool_python.LanguageTool')
    def test_glossary_does_not_prevent_grammar_errors(self, mock_lt_class):
        """Test that glossary doesn't prevent grammar error detection."""
        pytest.importorskip("language_tool_python")

        # Create grammar error involving glossary term
        mock_tool = Mock()
        mock_match = make_mock_match(
            message="Subject-verb agreement error",
            category="GRAMMAR",  # Not TYPOS
            rule_id="AGREEMENT_ERROR",
            replacements=["era"]
        )
        mock_tool.check.return_value = [mock_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Darcy fueron al baile.",  # Should be "fue"
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        glossary = Glossary(terms=[
            GlossaryTerm(english="Darcy", spanish="Darcy", type=GlossaryTermType.CHARACTER)
        ])

        result = evaluator.evaluate(chunk, {"glossary": glossary})

        # Grammar error should still be detected
        assert not result.passed
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR


@skip_if_no_lt
class TestCategoryFiltering:
    """Tests for category-based filtering."""

    @patch('language_tool_python.LanguageTool')
    def test_ignore_categories_typos(self, mock_lt_class):
        """Test ignoring TYPOS category."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        # Create both grammar and spelling errors
        grammar_match = make_mock_match("Grammar error", "GRAMMAR", "GRAMMAR_RULE")
        typos_match = make_mock_match("Spelling error", "TYPOS", "MORFOLOGIK")
        mock_tool.check.return_value = [grammar_match, typos_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test text",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"ignore_categories": ["TYPOS"]})

        # Only grammar error should be reported
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR
        assert "Grammar" in result.issues[0].message

    @patch('language_tool_python.LanguageTool')
    def test_skip_spelling_convenience_flag(self, mock_lt_class):
        """Test skip_spelling convenience flag."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        # Create both grammar and spelling errors
        grammar_match = make_mock_match("Grammar error", "GRAMMAR", "GRAMMAR_RULE")
        typos_match = make_mock_match("Spelling error", "TYPOS", "MORFOLOGIK")
        mock_tool.check.return_value = [grammar_match, typos_match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test text",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"skip_spelling": True})

        # Only grammar error should be reported
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR


@skip_if_no_lt
class TestRuleFiltering:
    """Tests for rule-specific filtering."""

    @patch('language_tool_python.LanguageTool')
    def test_ignore_specific_rules(self, mock_lt_class):
        """Test ignoring specific rule IDs."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        match1 = make_mock_match("Error 1", "GRAMMAR", "RULE_1")
        match2 = make_mock_match("Error 2", "GRAMMAR", "RULE_2")
        mock_tool.check.return_value = [match1, match2]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test text",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"ignore_rules": ["RULE_1"]})

        # Only match2 should be reported
        assert len(result.issues) == 1
        assert "Error 2" in result.issues[0].message


@skip_if_no_lt
class TestMaxIssuesLimit:
    """Tests for max_issues limiting."""

    @patch('language_tool_python.LanguageTool')
    def test_max_issues_limit(self, mock_lt_class):
        """Test that max_issues limits the number of issues reported."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        # Create 10 matches
        matches = [
            make_mock_match(f"Error {i}", "GRAMMAR", f"RULE_{i}")
            for i in range(10)
        ]
        mock_tool.check.return_value = matches
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Text with many errors",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"max_issues": 5})

        # Should only report 5 issues
        assert len(result.issues) == 5

    @patch('language_tool_python.LanguageTool')
    def test_max_issues_prioritizes_errors(self, mock_lt_class):
        """Test that max_issues prioritizes ERROR over WARNING over INFO."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        # Create mix of severities
        matches = [
            make_mock_match("Info 1", "TYPOGRAPHY", "INFO_1"),
            make_mock_match("Warning 1", "STYLE", "WARN_1"),
            make_mock_match("Error 1", "GRAMMAR", "ERROR_1"),
            make_mock_match("Info 2", "TYPOGRAPHY", "INFO_2"),
            make_mock_match("Error 2", "GRAMMAR", "ERROR_2"),
        ]
        mock_tool.check.return_value = matches
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"max_issues": 3})

        # Should report 2 errors + 1 warning
        assert len(result.issues) == 3
        error_count = sum(1 for iss in result.issues if iss.severity == IssueLevel.ERROR)
        assert error_count == 2


@skip_if_no_lt
class TestDialectSupport:
    """Tests for dialect switching."""

    @patch('language_tool_python.LanguageTool')
    def test_init_with_dialect(self, mock_lt_class):
        """Test initialization with specific dialect."""
        pytest.importorskip("language_tool_python")

        evaluator = GrammarEvaluator(dialect='es-MX')

        # Check that LanguageTool was initialized with es-MX
        mock_lt_class.assert_called_with('es-MX')
        assert evaluator.dialect == 'es-MX'

    @patch('language_tool_python.LanguageTool')
    def test_dialect_override_in_context(self, mock_lt_class):
        """Test overriding dialect via context."""
        pytest.importorskip("language_tool_python")

        evaluator = GrammarEvaluator(dialect='es')

        mock_tool = Mock()
        mock_tool.check.return_value = []
        mock_lt_class.return_value = mock_tool

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Texto correcto.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"dialect": "es-MX"})

        # Should have reinitialized with es-MX
        assert evaluator.dialect == "es-MX"


@skip_if_no_lt
class TestSeverityMapping:
    """Tests for severity mapping from categories."""

    @patch('language_tool_python.LanguageTool')
    def test_severity_mapping(self, mock_lt_class):
        """Test that categories map to correct severities."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        matches = [
            make_mock_match("Grammar", "GRAMMAR", "G1"),      # → ERROR
            make_mock_match("Typo", "TYPOS", "T1"),           # → ERROR
            make_mock_match("Style", "STYLE", "S1"),          # → WARNING
            make_mock_match("Punct", "PUNCTUATION", "P1"),    # → WARNING
            make_mock_match("Typo", "TYPOGRAPHY", "TY1"),     # → INFO
            make_mock_match("Redund", "REDUNDANCY", "R1"),    # → INFO
        ]
        mock_tool.check.return_value = matches
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        # Check severities
        assert len(result.issues) == 6
        severities = [iss.severity for iss in result.issues]

        # Count by severity
        errors = sum(1 for s in severities if s == IssueLevel.ERROR)
        warnings = sum(1 for s in severities if s == IssueLevel.WARNING)
        infos = sum(1 for s in severities if s == IssueLevel.INFO)

        assert errors == 2  # GRAMMAR, TYPOS
        assert warnings == 2  # STYLE, PUNCTUATION
        assert infos == 2  # TYPOGRAPHY, REDUNDANCY


@skip_if_no_lt
class TestScoreCalculation:
    """Tests for score calculation."""

    @patch('language_tool_python.LanguageTool')
    def test_perfect_score_no_issues(self, mock_lt_class):
        """Test perfect score with no issues."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        mock_tool.check.return_value = []
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Texto perfecto.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert result.score == 1.0

    @patch('language_tool_python.LanguageTool')
    def test_score_penalty_for_errors(self, mock_lt_class):
        """Test score decreases with errors."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        # Create one error (5% penalty)
        mock_tool.check.return_value = [
            make_mock_match("Error", "GRAMMAR", "E1")
        ]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert result.score == 0.95  # 1.0 - 0.05


@skip_if_no_lt
class TestCharacterPositions:
    """Tests for character position tracking."""

    @patch('language_tool_python.LanguageTool')
    def test_character_positions_in_issues(self, mock_lt_class):
        """Test that character positions are tracked."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        match = make_mock_match(
            message="Error at position 10",
            category="GRAMMAR",
            rule_id="TEST",
            offset=10,
            length=5
        )
        mock_tool.check.return_value = [match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="0123456789ERROR12345",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 1
        assert "char 10" in result.issues[0].location


@skip_if_no_lt
class TestSuggestions:
    """Tests for replacement suggestions."""

    @patch('language_tool_python.LanguageTool')
    def test_suggestions_included(self, mock_lt_class):
        """Test that replacement suggestions are included."""
        pytest.importorskip("language_tool_python")

        mock_tool = Mock()
        match = make_mock_match(
            message="Error",
            category="GRAMMAR",
            rule_id="TEST",
            replacements=["corrección", "arreglo", "solución"]
        )
        mock_tool.check.return_value = [match]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Test",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 1
        assert result.issues[0].suggestion is not None
        assert "corrección" in result.issues[0].suggestion
