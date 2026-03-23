"""
Tests for the length evaluator.
"""

import pytest
from datetime import datetime

from src.models import Chunk, ChunkMetadata, ChunkStatus, IssueLevel
from src.evaluators.length_eval import LengthEvaluator


@pytest.fixture
def evaluator():
    """Create a length evaluator instance."""
    return LengthEvaluator()


@pytest.fixture
def base_chunk():
    """Create a basic chunk for testing."""
    return Chunk(
        id="test_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="This is a test sentence with exactly ten words here.",
        translated_text=None,  # Will be set in tests
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=10,
        ),
        status=ChunkStatus.PENDING,
    )


def test_good_translation_passes(evaluator, base_chunk):
    """Test that a translation with expected length passes."""
    # Spanish is typically 1.1-1.3x longer
    # Source: 10 words
    # Translation: 12 words (1.2x ratio - perfect)
    base_chunk.translated_text = "Esta es una oración de prueba con un total de doce palabras."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.eval_name == "length"
    assert result.metadata["ratio"] == pytest.approx(1.2, rel=0.1)


def test_too_short_translation_error(evaluator, base_chunk):
    """Test that a suspiciously short translation raises an error."""
    # Source: 10 words
    # Translation: 3 words (0.3x ratio - too short, < 0.5 threshold)
    base_chunk.translated_text = "Muy muy corto."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    # Check that message contains the ratio (could be 27%, 30%, or 30.0%)
    assert any(x in result.issues[0].message for x in ["27%", "30%", "30.0%"])
    assert result.score < 1.0


def test_too_long_translation_error(evaluator, base_chunk):
    """Test that an excessively long translation raises an error."""
    # Source: 10 words
    # Translation: 21+ words (2.1x ratio - too long, > 2.0 threshold)
    base_chunk.translated_text = (
        "Esta es una oración de prueba muy muy muy larga con muchas "
        "palabras adicionales que no deberían estar aquí en absoluto porque es demasiado largo."
    )

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    assert result.score < 1.0


def test_slightly_short_translation_warning(evaluator, base_chunk):
    """Test that a slightly short translation raises a warning."""
    # Source: 10 words
    # Translation: 8 words (0.8x ratio - below expected 1.1, but above error threshold 0.5)
    base_chunk.translated_text = "Esta es una prueba con ocho palabras totales."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True  # Warnings don't fail
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.WARNING
    assert result.score > 0.0 and result.score < 1.0


def test_slightly_long_translation_warning(evaluator, base_chunk):
    """Test that a slightly long translation raises a warning."""
    # Source: 10 words
    # Translation: 15 words (1.5x ratio - above expected 1.3, but below error threshold 2.0)
    base_chunk.translated_text = (
        "Esta es una oración de prueba que tiene un total de quince palabras en español."
    )

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True  # Warnings don't fail
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.WARNING
    assert result.score > 0.0 and result.score < 1.0


def test_empty_translation_error(evaluator, base_chunk):
    """Test that an empty translation raises an error."""
    base_chunk.translated_text = ""

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    assert result.metadata["ratio"] == 0.0


def test_no_translation_raises_exception(evaluator, base_chunk):
    """Test that a chunk without translation raises ValueError."""
    base_chunk.translated_text = None

    with pytest.raises(ValueError, match="has no translation"):
        evaluator.evaluate(base_chunk, {})


def test_count_by_characters(evaluator, base_chunk):
    """Test evaluation using character count instead of words."""
    # Source: "Test" (4 chars)
    base_chunk.source_text = "Test"
    # Translation: "Prueba" (6 chars, 1.5x ratio)
    base_chunk.translated_text = "Prueba"

    context = {"length_config": {"count_by": "chars"}}
    result = evaluator.evaluate(base_chunk, context)

    assert result.metadata["unit"] == "characters"
    assert result.metadata["source_count"] == 4
    assert result.metadata["target_count"] == 6
    assert result.metadata["ratio"] == 1.5


def test_custom_thresholds(evaluator, base_chunk):
    """Test evaluation with custom ratio thresholds."""
    # Source: 10 words
    # Translation: 15 words (1.5x ratio)
    base_chunk.translated_text = (
        "Esta es una oración de prueba que tiene un total de quince palabras."
    )

    # Set custom thresholds where 1.5x is acceptable
    context = {
        "length_config": {
            "min_ratio": 0.8,
            "max_ratio": 2.5,
            "expected_min": 1.0,
            "expected_max": 2.0,
        }
    }

    result = evaluator.evaluate(base_chunk, context)

    # Should pass with no issues since 1.5 is within expected range
    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0


def test_whitespace_handling(evaluator, base_chunk):
    """Test that extra whitespace doesn't affect word count."""
    base_chunk.source_text = "Test   with    extra     spaces"  # 4 words
    base_chunk.translated_text = "Prueba  con  espacios  adicionales  ahora"  # 5 words

    result = evaluator.evaluate(base_chunk, {})

    assert result.metadata["source_count"] == 4
    assert result.metadata["target_count"] == 5
    assert result.metadata["ratio"] == 1.25


def test_multiline_text(evaluator, base_chunk):
    """Test handling of text with multiple lines."""
    base_chunk.source_text = """This is the first line.
This is the second line.
This is the third line."""  # 15 words

    base_chunk.translated_text = """Esta es la primera línea.
Esta es la segunda línea.
Esta es la tercera línea."""  # 15 words

    result = evaluator.evaluate(base_chunk, {})

    assert result.metadata["source_count"] == 15
    assert result.metadata["target_count"] == 15
    assert result.passed is True  # 1.0 ratio might give warning, but within bounds


def test_metadata_includes_thresholds(evaluator, base_chunk):
    """Test that result metadata includes threshold configuration."""
    base_chunk.translated_text = "Traducción de prueba perfecta."

    result = evaluator.evaluate(base_chunk, {})

    assert "thresholds" in result.metadata
    assert "min_ratio" in result.metadata["thresholds"]
    assert "max_ratio" in result.metadata["thresholds"]
    assert "expected_min" in result.metadata["thresholds"]
    assert "expected_max" in result.metadata["thresholds"]


def test_score_calculation_perfect(evaluator, base_chunk):
    """Test score is 1.0 for perfect ratio."""
    # 1.2x ratio is within expected range (1.1-1.3)
    # Source has 10 words, so translation should have 12 words
    base_chunk.translated_text = "Esta es una prueba con doce palabras en total que están aquí."

    result = evaluator.evaluate(base_chunk, {})

    assert result.score == 1.0


def test_score_calculation_decreases_with_deviation(evaluator, base_chunk):
    """Test that score decreases as ratio deviates from expected."""
    # Source: 10 words

    # Test 1: Slightly short (0.9x) - small deviation
    base_chunk.translated_text = "Esta es una prueba con nueve palabras."
    result1 = evaluator.evaluate(base_chunk, {})

    # Test 2: Much shorter (0.6x) - large deviation
    base_chunk.translated_text = "Prueba con seis palabras."
    result2 = evaluator.evaluate(base_chunk, {})

    # Larger deviation should have lower score
    assert result2.score < result1.score
    assert result1.score < 1.0


def test_count_words_method(evaluator):
    """Test the _count_words helper method."""
    assert evaluator._count_words("one two three") == 3
    assert evaluator._count_words("") == 0
    assert evaluator._count_words("single") == 1
    assert evaluator._count_words("  extra   spaces  ") == 2


def test_count_chars_method(evaluator):
    """Test the _count_chars helper method."""
    assert evaluator._count_chars("test") == 4
    assert evaluator._count_chars("test test") == 8  # No space
    assert evaluator._count_chars("  test  ") == 4  # Whitespace removed
    assert evaluator._count_chars("") == 0


def test_calculate_ratio_method(evaluator):
    """Test the _calculate_ratio helper method."""
    assert evaluator._calculate_ratio(10, 12) == 1.2
    assert evaluator._calculate_ratio(10, 5) == 0.5
    assert evaluator._calculate_ratio(0, 10) == 0.0  # Avoid division by zero
    assert evaluator._calculate_ratio(10, 0) == 0.0


def test_issue_message_contains_useful_info(evaluator, base_chunk):
    """Test that issue messages contain actionable information."""
    base_chunk.translated_text = "Corto."  # Very short

    result = evaluator.evaluate(base_chunk, {})

    assert len(result.issues) == 1
    issue = result.issues[0]

    # Should contain counts, ratio, and suggestion
    assert any(char.isdigit() for char in issue.message)  # Has numbers
    assert issue.suggestion is not None
    assert len(issue.suggestion) > 0
    assert issue.location is not None


def test_evaluator_name_and_version(evaluator):
    """Test that evaluator has correct name and version."""
    assert evaluator.name == "length"
    assert evaluator.version == "1.0.0"
    assert evaluator.description is not None


def test_real_world_example(evaluator):
    """Test with a realistic translation example."""
    chunk = Chunk(
        id="don_quixote_ch01_001",
        chapter_id="chapter_01",
        position=1,
        source_text="""In a village of La Mancha, the name of which I have no desire to call to mind,
there lived not long since one of those gentlemen that keep a lance in the lance-rack,
an old buckler, a lean hack, and a greyhound for coursing.""",
        translated_text="""En un lugar de la Mancha, de cuyo nombre no quiero acordarme,
no ha mucho tiempo que vivía un hidalgo de los de lanza en astillero,
adarga antigua, rocín flaco y galgo corredor.""",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=250,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=42,
        ),
    )

    result = evaluator.evaluate(chunk, {})

    # Spanish version should be slightly shorter but within acceptable range
    assert result.passed is True or (result.issues and result.issues[0].severity == IssueLevel.WARNING)
    assert result.metadata["source_count"] > 0
    assert result.metadata["target_count"] > 0
