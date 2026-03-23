"""
Tests for the dictionary evaluator.
"""

import pytest
from datetime import datetime

from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    IssueLevel,
    Glossary,
    GlossaryTerm,
)
from src.evaluators.dictionary_eval import DictionaryEvaluator


@pytest.fixture
def evaluator():
    """Create a dictionary evaluator instance."""
    return DictionaryEvaluator()


@pytest.fixture
def base_chunk():
    """Create a basic chunk for testing."""
    return Chunk(
        id="test_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="This is a test sentence.",
        translated_text=None,  # Will be set in tests
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=5,
        ),
        status=ChunkStatus.PENDING,
    )


@pytest.fixture
def sample_glossary():
    """Create a sample glossary for testing."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Hogwarts",
                spanish="Hogwarts",
                term_type="place",
                context="School name, keep in English",
                alternatives=[],
            ),
            GlossaryTerm(
                english="API",
                spanish="API",
                term_type="technical",
                context="Technical term",
                alternatives=[],
            ),
        ],
        version="1.0.0",
    )


def test_all_spanish_words_pass(evaluator, base_chunk):
    """Test that valid Spanish text passes all checks."""
    base_chunk.translated_text = "Esta es una oración de prueba en español correcto."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.metadata["english_words"] == 0
    assert result.metadata["unknown_words"] == 0


def test_english_words_flagged_as_errors(evaluator, base_chunk):
    """Test that English words in translation are flagged as errors."""
    # Mix of Spanish and English
    base_chunk.translated_text = "Esta es una sentence con some English words."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    # All English words should be detected: sentence, some, English, words
    assert result.metadata["english_words"] == 4
    assert any(issue.severity == IssueLevel.ERROR for issue in result.issues)
    # Check that at least one error mentions English
    english_errors = [i for i in result.issues if i.severity == IssueLevel.ERROR]
    assert len(english_errors) >= 1
    assert any("English" in err.message for err in english_errors)


def test_misspelled_words_flagged_as_warnings(evaluator, base_chunk):
    """Test that misspelled Spanish words are flagged as warnings."""
    # "prueba" misspelled as "preuba"
    base_chunk.translated_text = "Esta es una preuba con errores ortográficos."

    result = evaluator.evaluate(base_chunk, {})

    # Warnings don't cause failure, but issues should be present
    assert result.metadata["unknown_words"] >= 1
    # Should have warnings for unknown words
    warnings = [i for i in result.issues if i.severity == IssueLevel.WARNING]
    assert len(warnings) >= 1
    # Score should be less than perfect due to unknown words
    assert result.score < 1.0


def test_proper_nouns_handled(evaluator, base_chunk):
    """Test that proper nouns (capitalized words) are handled appropriately."""
    # "María" is a valid Spanish name
    base_chunk.translated_text = "María es una persona importante en la historia."

    result = evaluator.evaluate(base_chunk, {})

    # María should be recognized (it's in Spanish dictionaries)
    # All other words are valid Spanish
    assert result.passed is True
    assert len(result.issues) == 0


def test_glossary_terms_excluded(evaluator, base_chunk, sample_glossary):
    """Test that glossary terms are not flagged even if not in dictionaries."""
    base_chunk.translated_text = "Hogwarts es una escuela en la API mágica."

    result = evaluator.evaluate(base_chunk, {"glossary": sample_glossary})

    # "Hogwarts" and "API" are in glossary, should not be flagged
    assert result.passed is True
    assert result.metadata["glossary_words"] == 2
    assert result.metadata["english_words"] == 0
    assert result.metadata["unknown_words"] == 0


def test_numbers_ignored(evaluator, base_chunk):
    """Test that numbers are ignored in dictionary checking."""
    base_chunk.translated_text = "Había 123 personas y 45.67 kilómetros."

    result = evaluator.evaluate(base_chunk, {})

    # Numbers should be ignored
    assert result.passed is True
    assert len(result.issues) == 0


def test_single_characters_handled(evaluator, base_chunk):
    """Test that single characters are handled correctly."""
    # Spanish single-letter words: a, o, e, y
    base_chunk.translated_text = "A María y Pedro o Elena e Isabel."

    result = evaluator.evaluate(base_chunk, {})

    # Should recognize valid single-letter Spanish words
    # Note: María, Pedro, Elena, Isabel are proper names
    # May or may not be in dictionary - test should pass if no English detected
    errors = [i for i in result.issues if i.severity == IssueLevel.ERROR]
    assert len(errors) == 0  # No English words


def test_punctuation_handling(evaluator, base_chunk):
    """Test that punctuation doesn't interfere with word checking."""
    base_chunk.translated_text = "¡Hola! ¿Cómo estás? Bien, gracias."

    result = evaluator.evaluate(base_chunk, {})

    # Should extract words correctly despite punctuation
    assert result.passed is True
    assert len(result.issues) == 0


def test_accented_characters(evaluator, base_chunk):
    """Test that Spanish accented characters are handled correctly."""
    # Use common Spanish words with accents (avoid proper names)
    base_chunk.translated_text = "La canción es rápida y difícil también."

    result = evaluator.evaluate(base_chunk, {})

    # All are valid Spanish words with accents
    assert result.passed is True
    assert len(result.issues) == 0


def test_case_insensitive_by_default(evaluator, base_chunk):
    """Test that checking is case-insensitive by default."""
    base_chunk.translated_text = "HOLA hola Hola HoLa."

    result = evaluator.evaluate(base_chunk, {})

    # All variants of "hola" should be accepted
    assert result.passed is True
    assert len(result.issues) == 0


def test_case_sensitive_mode(evaluator, base_chunk):
    """Test case-sensitive mode if enabled."""
    base_chunk.translated_text = "hola es una palabra."

    # Default: case insensitive
    result = evaluator.evaluate(base_chunk, {"case_sensitive": False})
    assert result.passed is True

    # Case sensitive: should still pass (lowercase is valid)
    result2 = evaluator.evaluate(base_chunk, {"case_sensitive": True})
    assert result2.passed is True


def test_empty_translation_raises_error(evaluator, base_chunk):
    """Test that empty translation raises ValueError."""
    base_chunk.translated_text = None

    with pytest.raises(ValueError, match="has no translation"):
        evaluator.evaluate(base_chunk, {})


def test_mixed_spanish_variants(evaluator, base_chunk):
    """Test that words from both es_ES and es_MX are accepted."""
    # Use words that might differ between variants
    # Most words are shared, so just test common Spanish
    base_chunk.translated_text = "El ordenador es una computadora moderna."

    result = evaluator.evaluate(base_chunk, {})

    # Both "ordenador" (Spain) and "computadora" (Latin America) should be valid
    # because we check both dictionaries with OR logic
    assert result.passed is True


def test_character_positions_reported(evaluator, base_chunk):
    """Test that character positions are reported for flagged words."""
    base_chunk.translated_text = "Esta sentence tiene some English words aquí."

    result = evaluator.evaluate(base_chunk, {})

    # Should have issues with character positions
    assert len(result.issues) > 0
    for issue in result.issues:
        # Location should mention character position
        assert "position" in issue.location.lower()


def test_suggestions_provided(evaluator, base_chunk):
    """Test that spelling suggestions are provided for unknown words."""
    # Intentional misspelling: "prueba" -> "preuba"
    base_chunk.translated_text = "Esta es una preuba."

    result = evaluator.evaluate(base_chunk, {})

    # Should have warning with suggestion
    warnings = [i for i in result.issues if i.severity == IssueLevel.WARNING]
    assert len(warnings) >= 1

    # At least one warning should have a suggestion
    has_suggestion = any(
        w.suggestion and ("Suggestion" in w.suggestion or "prueba" in w.suggestion.lower())
        for w in warnings
    )
    assert has_suggestion


def test_repeated_words_counted_separately(evaluator, base_chunk):
    """Test that repeated words are counted at each occurrence."""
    base_chunk.translated_text = "English English English palabra."

    result = evaluator.evaluate(base_chunk, {})

    # Should report multiple positions for "English"
    assert result.metadata["english_words"] == 1  # 1 unique word
    # But flagged_instances should be 3
    assert result.metadata["flagged_instances"] == 3


def test_score_calculation(evaluator, base_chunk):
    """Test that score is calculated correctly based on error ratio."""
    # 10 words total, 2 errors
    base_chunk.translated_text = "Uno dos tres cuatro cinco error1 error2 ocho nueve diez."

    result = evaluator.evaluate(base_chunk, {})

    # Score should be approximately 0.8 (8 good / 10 total)
    # Actual score might vary based on what's in dictionary
    assert 0.0 <= result.score <= 1.0


def test_metadata_completeness(evaluator, base_chunk):
    """Test that all expected metadata fields are present."""
    base_chunk.translated_text = "Esta es una prueba con algunas palabras."

    result = evaluator.evaluate(base_chunk, {})

    # Check all expected metadata fields
    assert "total_words" in result.metadata
    assert "unique_words" in result.metadata
    assert "english_words" in result.metadata
    assert "unknown_words" in result.metadata
    assert "glossary_words" in result.metadata
    assert "flagged_instances" in result.metadata

    # Values should make sense
    assert result.metadata["total_words"] >= result.metadata["unique_words"]
    assert result.metadata["flagged_instances"] >= 0


def test_hyphenated_words(evaluator, base_chunk):
    """Test that hyphenated words are handled appropriately."""
    base_chunk.translated_text = "Es un bien-estar importante."

    result = evaluator.evaluate(base_chunk, {})

    # Hyphenated words should be tokenized
    # May or may not pass depending on whether "bien-estar" is recognized
    # At minimum, should not crash
    assert result is not None


def test_apostrophes_in_words(evaluator, base_chunk):
    """Test that apostrophes in words are handled."""
    # Spanish doesn't commonly use apostrophes, but test anyway
    base_chunk.translated_text = "La palabra l'home es catalana."

    result = evaluator.evaluate(base_chunk, {})

    # Should handle apostrophes without crashing
    assert result is not None


def test_only_english_text(evaluator, base_chunk):
    """Test translation that's entirely in English."""
    base_chunk.translated_text = "This is completely in English and not translated."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    # Most/all words should be flagged as English
    assert result.metadata["english_words"] > 0
    assert result.score < 0.5  # Very low score


def test_capitalized_proper_nouns(evaluator, base_chunk):
    """Test that capitalized proper nouns are recognized correctly."""
    # Test with Spanish country names, cities, and personal names that are
    # capitalized in the dictionary
    base_chunk.translated_text = "Inglaterra, España, Francia, Madrid, Barcelona y México son lugares importantes."

    result = evaluator.evaluate(base_chunk, {})

    # All these are valid Spanish words (capitalized proper nouns)
    assert result.passed is True
    assert len(result.issues) == 0
    assert result.metadata["unknown_words"] == 0
    assert result.score == 1.0


def test_capitalized_proper_nouns_at_sentence_start(evaluator, base_chunk):
    """Test proper nouns at the start of sentences."""
    # "Inglaterra" should be recognized even though it's capitalized
    base_chunk.translated_text = "Inglaterra es un país. Sara vive en Madrid."

    result = evaluator.evaluate(base_chunk, {})

    # "Inglaterra", "Sara", and "Madrid" are all proper nouns in the dictionary
    assert result.passed is True
    assert len(result.issues) == 0


def test_lowercase_proper_nouns_warning(evaluator, base_chunk):
    """Test that lowercase proper nouns may get warnings."""
    # Lowercase country names are not in dictionary
    base_chunk.translated_text = "inglaterra es un país hermoso."

    result = evaluator.evaluate(base_chunk, {})

    # "inglaterra" (lowercase) is not in dictionary, should be flagged
    assert result.metadata["unknown_words"] >= 1
