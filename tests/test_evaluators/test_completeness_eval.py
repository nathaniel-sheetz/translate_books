"""Tests for completeness evaluator."""

import pytest
from datetime import datetime

from src.evaluators.completeness_eval import CompletenessEvaluator
from src.models import Chunk, ChunkMetadata, ChunkStatus, IssueLevel


# Test fixtures
def create_test_chunk(source_text: str, translated_text: str, chunk_id: str = "test_chunk") -> Chunk:
    """Helper to create test chunks."""
    return Chunk(
        id=chunk_id,
        chapter_id="chapter_01",
        position=1,
        source_text=source_text,
        translated_text=translated_text,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source_text),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=len(source_text.split())
        ),
        status=ChunkStatus.TRANSLATED,
        created_at=datetime.now()
    )


class TestCompletenessEvaluator:
    """Tests for CompletenessEvaluator class."""

    def test_complete_translation_passes(self):
        """Test that a complete translation passes all checks."""
        chunk = create_test_chunk(
            source_text="This is a complete sentence.",
            translated_text="Esta es una oración completa."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is True
        assert len(result.issues) == 0
        assert result.score == 1.0
        assert result.metadata["empty"] is False
        assert result.metadata["has_placeholders"] is False
        assert result.metadata["appears_truncated"] is False

    def test_empty_translation_fails(self):
        """Test that empty translation is detected as error."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text=""
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR
        assert "empty" in result.issues[0].message.lower()
        assert result.score == 0.0
        assert result.metadata["empty"] is True

    def test_whitespace_only_translation_fails(self):
        """Test that whitespace-only translation is detected as empty."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="   \n\t  \n   "
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert len(result.issues) == 1
        assert "empty" in result.issues[0].message.lower()
        assert result.metadata["empty"] is True

    def test_none_translation_raises_error(self):
        """Test that None translation raises ValueError."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="placeholder"
        )
        chunk.translated_text = None

        evaluator = CompletenessEvaluator()

        with pytest.raises(ValueError, match="has no translation"):
            evaluator.evaluate(chunk, {})


class TestPlaceholderDetection:
    """Tests for placeholder text detection."""

    def test_todo_placeholder_detected(self):
        """Test TODO placeholder is detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="TODO: Translate this text."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.ERROR
        assert "placeholder" in result.issues[0].message.lower()
        assert "TODO" in result.issues[0].message
        assert result.metadata["has_placeholders"] is True

    def test_fixme_placeholder_detected(self):
        """Test FIXME placeholder is detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="FIXME: Need better translation"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert any("FIXME" in issue.message for issue in result.issues)

    def test_xxx_placeholder_detected(self):
        """Test XXX placeholder is detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="XXX incomplete"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert any("XXX" in issue.message for issue in result.issues)

    def test_bracket_placeholders_detected(self):
        """Test bracket-style placeholders are detected."""
        test_cases = [
            "[TRANSLATION HERE]",
            "[INSERT TRANSLATION]",
            "[MISSING TRANSLATION]",
            "[TBD]",
            "[PLACEHOLDER]",
        ]

        for placeholder in test_cases:
            chunk = create_test_chunk(
                source_text="This is some text.",
                translated_text=f"Text {placeholder} more text."
            )

            evaluator = CompletenessEvaluator()
            result = evaluator.evaluate(chunk, {})

            assert result.passed is False, f"Failed to detect: {placeholder}"
            assert result.metadata["has_placeholders"] is True

    def test_angle_bracket_placeholders_detected(self):
        """Test angle bracket placeholders are detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="Text <TRANSLATION NEEDED> more text."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert result.metadata["has_placeholders"] is True

    def test_triple_angle_placeholders_detected(self):
        """Test triple angle bracket placeholders are detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="Text <<<INCOMPLETE>>> more text."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert result.metadata["has_placeholders"] is True

    def test_curly_brace_placeholders_detected(self):
        """Test curly brace placeholders are detected."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="Text {{TRANSLATION}} more text."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert result.metadata["has_placeholders"] is True

    def test_multiple_placeholders_detected(self):
        """Test multiple placeholders are all detected."""
        chunk = create_test_chunk(
            source_text="This is some text with multiple sections.",
            translated_text="TODO: First part [TRANSLATION HERE] second part."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert len(result.issues) == 2  # Two placeholders detected
        assert result.metadata["has_placeholders"] is True

    def test_custom_placeholder_patterns(self):
        """Test custom placeholder patterns can be added."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="Text CUSTOM_PLACEHOLDER more text."
        )

        context = {
            "completeness_config": {
                "custom_placeholders": [r'CUSTOM_PLACEHOLDER']
            }
        }

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, context)

        assert result.passed is False
        assert any("CUSTOM_PLACEHOLDER" in issue.message for issue in result.issues)

    def test_case_insensitive_placeholder_detection(self):
        """Test placeholder detection is case-insensitive."""
        chunk = create_test_chunk(
            source_text="This is some text.",
            translated_text="todo: lowercase placeholder"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert any("todo" in issue.message.lower() for issue in result.issues)


class TestTruncationDetection:
    """Tests for truncation detection."""

    def test_proper_ending_punctuation_passes(self):
        """Test that text with proper ending punctuation passes."""
        proper_endings = [
            "Esta es una oración completa.",
            "¿Es esto una pregunta?",
            "¡Qué maravilloso!",
            'Ella dijo: "Hola".',
            "Puntos suspensivos...",
            "Texto con cierre»",
            "Texto con paréntesis (final).",
            "Texto con corchete [final].",
            "Em-dash final—",
        ]

        evaluator = CompletenessEvaluator()

        for text in proper_endings:
            chunk = create_test_chunk(
                source_text="Some source text.",
                translated_text=text
            )

            result = evaluator.evaluate(chunk, {})

            assert result.metadata["appears_truncated"] is False, f"False positive for: {text}"

    def test_no_ending_punctuation_warns(self):
        """Test that text without ending punctuation triggers warning."""
        chunk = create_test_chunk(
            source_text="This is complete text.",
            translated_text="Esta es una oración sin punto final"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.WARNING
        assert "truncated" in result.issues[0].message.lower()
        assert result.metadata["appears_truncated"] is True

    def test_truncation_provides_context(self):
        """Test that truncation warning provides context."""
        chunk = create_test_chunk(
            source_text="This is complete text.",
            translated_text="Esta es una oración muy larga que termina sin puntuación adecuada"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 1
        # Should show last part of text in location
        assert "termina sin puntuación" in result.issues[0].location


class TestSpecialMarkers:
    """Tests for special marker preservation."""

    def test_horizontal_rule_preserved(self):
        """Test that horizontal rules are preserved."""
        chunk = create_test_chunk(
            source_text="Text before\n\n---\n\nText after",
            translated_text="Texto antes.\n\n---\n\nTexto después."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0
        assert result.metadata["marker_issues_count"] == 0

    def test_horizontal_rule_missing_warns(self):
        """Test that missing horizontal rules trigger warning."""
        chunk = create_test_chunk(
            source_text="Text before\n\n---\n\nText after",
            translated_text="Texto antes\n\nTexto después"  # Missing ---
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) >= 1
        assert any("marker" in issue.message.lower() for issue in result.issues)
        assert result.metadata["marker_issues_count"] >= 1

    def test_star_divider_preserved(self):
        """Test that star dividers are preserved."""
        chunk = create_test_chunk(
            source_text="Text before\n\n* * *\n\nText after",
            translated_text="Texto antes.\n\n* * *\n\nTexto después."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0

    def test_star_divider_missing_warns(self):
        """Test that missing star dividers trigger warning."""
        chunk = create_test_chunk(
            source_text="Text before\n\n* * *\n\nText after",
            translated_text="Texto antes\n\nTexto después"  # Missing * * *
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) >= 1
        assert any("* * *" in issue.message for issue in result.issues)

    def test_markdown_headers_preserved(self):
        """Test that markdown headers are preserved."""
        chunk = create_test_chunk(
            source_text="# Chapter One\n\nText here",
            translated_text="# Capítulo Uno\n\nTexto aquí."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0

    def test_numbered_list_preserved(self):
        """Test that numbered lists are preserved."""
        chunk = create_test_chunk(
            source_text="1. First item\n2. Second item",
            translated_text="1. Primer elemento.\n2. Segundo elemento."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0

    def test_bullet_list_preserved(self):
        """Test that bullet lists are preserved."""
        chunk = create_test_chunk(
            source_text="- First item\n- Second item",
            translated_text="- Primer elemento.\n- Segundo elemento."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0

    def test_multiple_markers_preserved(self):
        """Test that multiple markers are all checked."""
        chunk = create_test_chunk(
            source_text="---\n\n# Header\n\n* * *\n\n- List item",
            translated_text="---\n\n# Encabezado\n\n* * *\n\n- Elemento de lista."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert len(result.issues) == 0

    def test_multiple_markers_some_missing(self):
        """Test that some missing markers are detected."""
        chunk = create_test_chunk(
            source_text="---\n\n# Header\n\n* * *\n\nText",
            translated_text="# Encabezado\n\nTexto"  # Missing --- and * * *
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        # Should have at least 2 warnings (one for each missing marker type)
        marker_issues = [i for i in result.issues if "marker" in i.message.lower()]
        assert len(marker_issues) >= 2

    def test_marker_checking_can_be_disabled(self):
        """Test that marker checking can be disabled."""
        chunk = create_test_chunk(
            source_text="---\n\nText",
            translated_text="Texto"  # Missing ---
        )

        context = {
            "completeness_config": {
                "check_markers": False
            }
        }

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, context)

        # No marker issues because checking is disabled
        marker_issues = [i for i in result.issues if "marker" in i.message.lower()]
        assert len(marker_issues) == 0

    def test_strict_markers_mode(self):
        """Test that strict mode treats missing markers as errors."""
        chunk = create_test_chunk(
            source_text="---\n\nText",
            translated_text="Texto"  # Missing ---
        )

        context = {
            "completeness_config": {
                "strict_markers": True
            }
        }

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, context)

        marker_issues = [i for i in result.issues if "marker" in i.message.lower()]
        assert len(marker_issues) >= 1
        assert marker_issues[0].severity == IssueLevel.ERROR

    def test_non_strict_markers_mode(self):
        """Test that non-strict mode treats missing markers as warnings."""
        chunk = create_test_chunk(
            source_text="---\n\nText",
            translated_text="Texto"  # Missing ---
        )

        context = {
            "completeness_config": {
                "strict_markers": False  # Default
            }
        }

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, context)

        marker_issues = [i for i in result.issues if "marker" in i.message.lower()]
        assert len(marker_issues) >= 1
        assert marker_issues[0].severity == IssueLevel.WARNING


class TestScoreCalculation:
    """Tests for score calculation."""

    def test_perfect_score_no_issues(self):
        """Test that perfect translation gets score of 1.0."""
        chunk = create_test_chunk(
            source_text="This is complete.",
            translated_text="Esto está completo."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.score == 1.0

    def test_score_decreases_with_errors(self):
        """Test that errors decrease score by 0.3 each."""
        chunk = create_test_chunk(
            source_text="This is text.",
            translated_text="TODO [TRANSLATION] more text."  # 2 placeholders = 2 errors
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        # 2 errors = -0.6, so score should be 0.4
        assert result.score == pytest.approx(0.4)

    def test_score_decreases_with_warnings(self):
        """Test that warnings decrease score by 0.1 each."""
        chunk = create_test_chunk(
            source_text="---\n\nText",
            translated_text="Texto sin puntuación"  # 1 truncation warning + 1 marker warning
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        # 2 warnings = -0.2, so score should be 0.8
        assert result.score == 0.8

    def test_score_mixed_issues(self):
        """Test score with mixed errors and warnings."""
        chunk = create_test_chunk(
            source_text="---\n\nText",
            translated_text="TODO: traducir"  # 1 error (TODO) + possibly warnings
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        # At least 1 error, score should be <= 0.7
        assert result.score <= 0.7

    def test_score_never_below_zero(self):
        """Test that score never goes below 0.0."""
        chunk = create_test_chunk(
            source_text="Text",
            translated_text="TODO FIXME XXX [TODO] [FIXME] <TODO>"  # Many errors
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.score >= 0.0
        assert result.score == 0.0  # Should be exactly 0.0 with many errors


class TestEvaluatorMetadata:
    """Tests for evaluator metadata."""

    def test_evaluator_name_and_version(self):
        """Test that evaluator has proper name and version."""
        evaluator = CompletenessEvaluator()

        assert evaluator.name == "completeness"
        assert evaluator.version == "1.0.0"
        assert evaluator.description

    def test_result_metadata_structure(self):
        """Test that result metadata has expected structure."""
        chunk = create_test_chunk(
            source_text="Text",
            translated_text="Texto."
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert "empty" in result.metadata
        assert "has_placeholders" in result.metadata
        assert "appears_truncated" in result.metadata
        assert "marker_issues_count" in result.metadata
        assert isinstance(result.metadata["empty"], bool)
        assert isinstance(result.metadata["has_placeholders"], bool)
        assert isinstance(result.metadata["appears_truncated"], bool)
        assert isinstance(result.metadata["marker_issues_count"], int)


class TestIntegration:
    """Integration tests with realistic scenarios."""

    def test_realistic_good_translation(self):
        """Test with realistic good translation."""
        chunk = create_test_chunk(
            source_text="""It is a truth universally acknowledged, that a single man in
possession of a good fortune, must be in want of a wife.

However little known the feelings or views of such a man may be on his
first entering a neighbourhood, this truth is so well fixed in the minds
of the surrounding families, that he is considered the rightful property
of some one or other of their daughters.""",
            translated_text="""Es una verdad universalmente reconocida que un hombre soltero en
posesión de una gran fortuna necesita una esposa.

Por poco que se conozcan los sentimientos o puntos de vista de un
hombre así al establecerse por primera vez en una vecindad, esta verdad
está tan arraigada en la mente de las familias circundantes, que lo
consideran propiedad legítima de una u otra de sus hijas."""
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is True
        assert len(result.issues) == 0
        assert result.score == 1.0

    def test_realistic_incomplete_translation(self):
        """Test with realistic incomplete translation."""
        chunk = create_test_chunk(
            source_text="The entire first chapter text here.",
            translated_text="TODO: Finish translating this chapter"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is False
        assert len(result.issues) >= 1
        assert result.metadata["has_placeholders"] is True

    def test_realistic_truncated_translation(self):
        """Test with realistic truncated translation."""
        chunk = create_test_chunk(
            source_text="This is a complete sentence with proper ending.",
            translated_text="Esta es una oración completa que de repente"
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        # Should warn about truncation (no proper ending)
        assert len(result.issues) >= 1
        assert result.metadata["appears_truncated"] is True

    def test_realistic_with_section_breaks(self):
        """Test with realistic text containing section breaks."""
        chunk = create_test_chunk(
            source_text="""Chapter the First

---

It is a truth universally acknowledged...""",
            translated_text="""Capítulo Primero

---

Es una verdad universalmente reconocida..."""
        )

        evaluator = CompletenessEvaluator()
        result = evaluator.evaluate(chunk, {})

        assert result.passed is True
        assert len(result.issues) == 0
