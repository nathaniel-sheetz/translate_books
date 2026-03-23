"""
Tests for the paragraph evaluator.
"""

import pytest
from datetime import datetime

from src.models import Chunk, ChunkMetadata, ChunkStatus, IssueLevel
from src.evaluators.paragraph_eval import ParagraphEvaluator


@pytest.fixture
def evaluator():
    """Create a paragraph evaluator instance."""
    return ParagraphEvaluator()


@pytest.fixture
def base_chunk():
    """Create a basic chunk for testing."""
    return Chunk(
        id="test_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="Default text.",  # Will be overwritten in tests
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


def test_matching_paragraph_count_passes(evaluator, base_chunk):
    """Test that matching paragraph counts pass."""
    base_chunk.source_text = """First paragraph.

Second paragraph.

Third paragraph."""

    base_chunk.translated_text = """Primer párrafo.

Segundo párrafo.

Tercer párrafo."""

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.metadata["source_paragraphs"] == 3
    assert result.metadata["translation_paragraphs"] == 3
    assert result.metadata["match"] is True


def test_fewer_paragraphs_error(evaluator, base_chunk):
    """Test that fewer paragraphs in translation raises error."""
    base_chunk.source_text = """First paragraph.

Second paragraph.

Third paragraph."""

    # Translation has only 2 paragraphs (merged)
    base_chunk.translated_text = """Primer párrafo.

Segundo y tercer párrafo combinados."""

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    assert "merged" in result.issues[0].message.lower()
    assert "3" in result.issues[0].message  # source count
    assert "2" in result.issues[0].message  # translation count
    assert result.score < 1.0


def test_more_paragraphs_error(evaluator, base_chunk):
    """Test that more paragraphs in translation raises error."""
    base_chunk.source_text = """First paragraph.

Second paragraph."""

    # Translation has 3 paragraphs (split)
    base_chunk.translated_text = """Primer párrafo.

Segunda parte uno.

Segunda parte dos."""

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    assert "split" in result.issues[0].message.lower() or "extra" in result.issues[0].message.lower()
    assert result.score < 1.0


def test_single_paragraph_matches(evaluator, base_chunk):
    """Test that single paragraph works correctly."""
    base_chunk.source_text = "This is a single paragraph without breaks."
    base_chunk.translated_text = "Este es un solo párrafo sin saltos."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.metadata["source_paragraphs"] == 1
    assert result.metadata["translation_paragraphs"] == 1


def test_no_translation_raises_exception(evaluator, base_chunk):
    """Test that chunk without translation raises ValueError."""
    base_chunk.source_text = "Some text."
    base_chunk.translated_text = None

    with pytest.raises(ValueError, match="has no translation"):
        evaluator.evaluate(base_chunk, {})


def test_windows_newlines(evaluator, base_chunk):
    """Test handling of Windows-style \r\n newlines."""
    base_chunk.source_text = "First paragraph.\r\n\r\nSecond paragraph.\r\n\r\nThird paragraph."
    base_chunk.translated_text = "Primer párrafo.\r\n\r\nSegundo párrafo.\r\n\r\nTercer párrafo."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 3
    assert result.metadata["translation_paragraphs"] == 3


def test_mixed_newlines(evaluator, base_chunk):
    """Test handling of mixed newline styles."""
    # Source has Unix newlines
    base_chunk.source_text = "First.\n\nSecond.\n\nThird."
    # Translation has Windows newlines
    base_chunk.translated_text = "Primero.\r\n\r\nSegundo.\r\n\r\nTercero."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 3
    assert result.metadata["translation_paragraphs"] == 3


def test_multiple_blank_lines_between_paragraphs(evaluator, base_chunk):
    """Test that multiple blank lines are treated as paragraph separator."""
    base_chunk.source_text = "First.\n\n\n\nSecond."  # 3 blank lines
    base_chunk.translated_text = "Primero.\n\nSegundo."  # 1 blank line

    result = evaluator.evaluate(base_chunk, {})

    # Should still count as 2 paragraphs in both
    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 2
    assert result.metadata["translation_paragraphs"] == 2


def test_whitespace_only_paragraphs_ignored(evaluator, base_chunk):
    """Test that whitespace-only paragraphs are not counted."""
    base_chunk.source_text = "First.\n\n   \n\nSecond."  # Middle "paragraph" is just spaces
    base_chunk.translated_text = "Primero.\n\nSegundo."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 2
    assert result.metadata["translation_paragraphs"] == 2


def test_leading_and_trailing_whitespace(evaluator, base_chunk):
    """Test that leading/trailing whitespace doesn't affect count."""
    base_chunk.source_text = "\n\nFirst.\n\nSecond.\n\n"
    base_chunk.translated_text = "Primero.\n\nSegundo."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 2
    assert result.metadata["translation_paragraphs"] == 2


def test_empty_text_zero_paragraphs(evaluator, base_chunk):
    """Test that empty text has zero paragraphs."""
    base_chunk.source_text = ""
    base_chunk.translated_text = ""

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 0
    assert result.metadata["translation_paragraphs"] == 0


def test_whitespace_only_text_zero_paragraphs(evaluator, base_chunk):
    """Test that whitespace-only text has zero paragraphs."""
    base_chunk.source_text = "   \n\n  \n  "
    base_chunk.translated_text = "\n\n\n"

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 0
    assert result.metadata["translation_paragraphs"] == 0


def test_allow_mismatch_configuration(evaluator, base_chunk):
    """Test that allow_mismatch configuration permits differences."""
    base_chunk.source_text = "First.\n\nSecond.\n\nThird."
    base_chunk.translated_text = "Primero.\n\nSegundo."  # Missing one paragraph

    # Without allow_mismatch, should fail
    result_strict = evaluator.evaluate(base_chunk, {})
    assert result_strict.passed is False

    # With allow_mismatch and threshold=1, should warn but pass
    context = {
        "paragraph_config": {
            "allow_mismatch": True,
            "mismatch_threshold": 1
        }
    }
    result_lenient = evaluator.evaluate(base_chunk, context)

    assert result_lenient.passed is True  # Warnings don't fail
    assert len(result_lenient.issues) == 1
    assert result_lenient.issues[0].severity == IssueLevel.WARNING


def test_mismatch_exceeds_threshold(evaluator, base_chunk):
    """Test that mismatches exceeding threshold still error."""
    base_chunk.source_text = "One.\n\nTwo.\n\nThree.\n\nFour.\n\nFive."
    base_chunk.translated_text = "Uno.\n\nDos."  # 3 paragraphs missing

    context = {
        "paragraph_config": {
            "allow_mismatch": True,
            "mismatch_threshold": 1  # Allow 1, but we have 3 difference
        }
    }

    result = evaluator.evaluate(base_chunk, context)

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR


def test_score_calculation_perfect(evaluator, base_chunk):
    """Test score is 1.0 for perfect match."""
    base_chunk.source_text = "One.\n\nTwo."
    base_chunk.translated_text = "Uno.\n\nDos."

    result = evaluator.evaluate(base_chunk, {})

    assert result.score == 1.0


def test_score_calculation_decreases(evaluator, base_chunk):
    """Test that score decreases with paragraph differences."""
    base_chunk.source_text = "One.\n\nTwo.\n\nThree.\n\nFour."  # 4 paragraphs

    # Test with 3 paragraphs (1 missing)
    base_chunk.translated_text = "Uno.\n\nDos.\n\nTres."
    result1 = evaluator.evaluate(base_chunk, {})

    # Test with 2 paragraphs (2 missing)
    base_chunk.translated_text = "Uno.\n\nDos."
    result2 = evaluator.evaluate(base_chunk, {})

    # More missing paragraphs should have lower score
    assert result2.score < result1.score
    assert result1.score < 1.0


def test_normalize_newlines_method(evaluator):
    """Test the _normalize_newlines helper method."""
    # Windows newlines
    assert evaluator._normalize_newlines("test\r\nline") == "test\nline"

    # Old Mac newlines
    assert evaluator._normalize_newlines("test\rline") == "test\nline"

    # Unix newlines (unchanged)
    assert evaluator._normalize_newlines("test\nline") == "test\nline"

    # Mixed
    assert evaluator._normalize_newlines("one\r\ntwo\rthree\nfour") == "one\ntwo\nthree\nfour"


def test_count_paragraphs_method(evaluator):
    """Test the _count_paragraphs helper method."""
    assert evaluator._count_paragraphs("Single paragraph") == 1
    assert evaluator._count_paragraphs("First.\n\nSecond.") == 2
    assert evaluator._count_paragraphs("One.\n\nTwo.\n\nThree.") == 3
    assert evaluator._count_paragraphs("") == 0
    assert evaluator._count_paragraphs("   \n\n   ") == 0
    assert evaluator._count_paragraphs("First.\n\n\n\nSecond.") == 2  # Multiple blank lines


def test_metadata_includes_all_fields(evaluator, base_chunk):
    """Test that result metadata includes all expected fields."""
    base_chunk.source_text = "One.\n\nTwo."
    base_chunk.translated_text = "Uno."

    result = evaluator.evaluate(base_chunk, {})

    assert "source_paragraphs" in result.metadata
    assert "translation_paragraphs" in result.metadata
    assert "difference" in result.metadata
    assert "match" in result.metadata
    assert result.metadata["difference"] == 1
    assert result.metadata["match"] is False


def test_issue_message_contains_useful_info(evaluator, base_chunk):
    """Test that issue messages contain actionable information."""
    base_chunk.source_text = "One.\n\nTwo.\n\nThree."
    base_chunk.translated_text = "Uno."

    result = evaluator.evaluate(base_chunk, {})

    assert len(result.issues) == 1
    issue = result.issues[0]

    # Should contain paragraph counts
    assert "3" in issue.message  # source count
    assert "1" in issue.message  # translation count
    assert issue.suggestion is not None
    assert len(issue.suggestion) > 0
    assert issue.location is not None


def test_evaluator_name_and_version(evaluator):
    """Test that evaluator has correct name and version."""
    assert evaluator.name == "paragraph"
    assert evaluator.version == "1.0.0"
    assert evaluator.description is not None


def test_real_world_example(evaluator):
    """Test with a realistic translation example."""
    chunk = Chunk(
        id="don_quixote_ch01_001",
        chapter_id="chapter_01",
        position=1,
        source_text="""In a village of La Mancha, the name of which I have no desire to call to mind, there lived not long since one of those gentlemen that keep a lance in the lance-rack, an old buckler, a lean hack, and a greyhound for coursing.

An olla of rather more beef than mutton, a salad on most nights, scraps on Saturdays, lentils on Fridays, and a pigeon or so extra on Sundays, made away with three-quarters of his income.

The rest of it went in a doublet of fine cloth and velvet breeches and shoes to match for holidays, while on week-days he made a brave figure in his best homespun.""",
        translated_text="""En un lugar de la Mancha, de cuyo nombre no quiero acordarme, no ha mucho tiempo que vivía un hidalgo de los de lanza en astillero, adarga antigua, rocín flaco y galgo corredor.

Una olla de algo más vaca que carnero, salpicón las más noches, duelos y quebrantos los sábados, lentejas los viernes, algún palomino de añadidura los domingos, consumían las tres partes de su hacienda.

El resto della concluían sayo de velarte, calzas de velludo para las fiestas con sus pantuflos de lo mismo, y los días de entre semana se honraba con su vellorí de lo más fino.""",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=500,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=3,
            word_count=100,
        ),
    )

    result = evaluator.evaluate(chunk, {})

    # Should pass - both have 3 paragraphs
    assert result.passed is True
    assert result.metadata["source_paragraphs"] == 3
    assert result.metadata["translation_paragraphs"] == 3
    assert result.score == 1.0
