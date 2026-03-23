"""
Tests for Glossary Evaluator

Validates that glossary term compliance checking works correctly.
"""

import pytest
from datetime import datetime
from src.models import (
    Chunk, ChunkMetadata, ChunkStatus, Glossary, GlossaryTerm, GlossaryTermType, IssueLevel
)
from src.evaluators.glossary_eval import GlossaryEvaluator


# ===== FIXTURES =====

@pytest.fixture
def evaluator():
    """Create a GlossaryEvaluator instance"""
    return GlossaryEvaluator()


@pytest.fixture
def sample_glossary():
    """Create a sample glossary with diverse terms"""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Mr. Bennet",
                spanish="Sr. Bennet",
                type=GlossaryTermType.CHARACTER,
                alternatives=["señor Bennet"]
            ),
            GlossaryTerm(
                english="Elizabeth Bennet",
                spanish="Elizabeth Bennet",
                type=GlossaryTermType.CHARACTER,
                alternatives=[]
            ),
            GlossaryTerm(
                english="Netherfield",
                spanish="Netherfield",
                type=GlossaryTermType.PLACE,
                alternatives=[]
            ),
            GlossaryTerm(
                english="entailment",
                spanish="vinculación",
                type=GlossaryTermType.CONCEPT,
                alternatives=["mayorazgo", "vínculo sucesorio"]
            ),
        ],
        version="1.0.0",
        updated_at=datetime.now()
    )


@pytest.fixture
def basic_chunk():
    """Create a basic chunk for testing"""
    return Chunk(
        id="test_001",
        chapter_id="ch01",
        position=1,
        source_text="Test source text",
        translated_text="Test translation",
        status=ChunkStatus.TRANSLATED,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=10
        )
    )


# ===== A. BASIC FUNCTIONALITY TESTS (8 tests) =====

def test_correct_glossary_usage_passes(evaluator, sample_glossary, basic_chunk):
    """Test that correct glossary usage passes validation"""
    basic_chunk.source_text = "Mr. Bennet said that entailment was unfair."
    basic_chunk.translated_text = "Sr. Bennet dijo que la vinculación era injusta."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0


def test_missing_translation_error(evaluator, sample_glossary, basic_chunk):
    """Test that missing glossary term translation creates ERROR"""
    basic_chunk.source_text = "Mr. Bennet went to Netherfield."
    basic_chunk.translated_text = "El señor fue a la casa."  # Missing "Netherfield"

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    assert len(result.issues) >= 1

    # Check for Netherfield missing error
    netherfield_errors = [
        issue for issue in result.issues
        if "Netherfield" in issue.message and issue.severity == IssueLevel.ERROR
    ]
    assert len(netherfield_errors) == 1
    assert result.score < 1.0


def test_wrong_translation_error(evaluator, sample_glossary, basic_chunk):
    """Test that wrong glossary translation creates ERROR"""
    basic_chunk.source_text = "Mr. Bennet said hello."
    basic_chunk.translated_text = "Señor Bennett dijo hola."  # Wrong: "Bennett" not "Bennet"

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    # Should have error for missing correct translation
    errors = [issue for issue in result.issues if issue.severity == IssueLevel.ERROR]
    assert len(errors) >= 1


def test_correct_primary_translation(evaluator, sample_glossary, basic_chunk):
    """Test that using primary translation passes"""
    basic_chunk.source_text = "The entailment troubled Mr. Bennet."
    basic_chunk.translated_text = "La vinculación preocupaba a Sr. Bennet."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert len([issue for issue in result.issues if issue.severity == IssueLevel.ERROR]) == 0


def test_correct_alternative_translation(evaluator, sample_glossary, basic_chunk):
    """Test that using valid alternative passes"""
    basic_chunk.source_text = "The entailment troubled Mr. Bennet."
    basic_chunk.translated_text = "El mayorazgo preocupaba a señor Bennet."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    errors = [issue for issue in result.issues if issue.severity == IssueLevel.ERROR]
    assert len(errors) == 0


def test_no_glossary_provided(evaluator, basic_chunk):
    """Test that evaluation works when no glossary provided"""
    basic_chunk.source_text = "Some text here."
    basic_chunk.translated_text = "Algún texto aquí."

    result = evaluator.evaluate(basic_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.metadata["glossary_terms_checked"] == 0


def test_empty_glossary(evaluator, basic_chunk):
    """Test that evaluation works with empty glossary"""
    empty_glossary = Glossary(terms=[], version="1.0.0", updated_at=datetime.now())
    basic_chunk.source_text = "Some text here."
    basic_chunk.translated_text = "Algún texto aquí."

    result = evaluator.evaluate(basic_chunk, {"glossary": empty_glossary})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0


def test_no_terms_in_text(evaluator, sample_glossary, basic_chunk):
    """Test that chunk with no glossary terms passes"""
    basic_chunk.source_text = "The weather was nice."
    basic_chunk.translated_text = "El clima estaba agradable."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.metadata["glossary_terms_in_source"] == 0


# ===== B. CONSISTENCY CHECKING TESTS (5 tests) =====

def test_consistent_primary_usage(evaluator, sample_glossary, basic_chunk):
    """Test that consistent use of primary translation passes"""
    basic_chunk.source_text = "Mr. Bennet told Mr. Bennet's wife that Mr. Bennet was tired."
    basic_chunk.translated_text = "Sr. Bennet le dijo a la esposa de Sr. Bennet que Sr. Bennet estaba cansado."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    # No consistency warnings
    warnings = [issue for issue in result.issues if issue.severity == IssueLevel.WARNING]
    assert len([w for w in warnings if "Inconsistent" in w.message]) == 0


def test_consistent_alternative_usage(evaluator, sample_glossary, basic_chunk):
    """Test that consistent use of alternative passes"""
    basic_chunk.source_text = "Mr. Bennet and Mr. Bennet's daughter."
    basic_chunk.translated_text = "señor Bennet y la hija de señor Bennet."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    # No consistency warnings
    warnings = [issue for issue in result.issues if issue.severity == IssueLevel.WARNING]
    consistency_warnings = [w for w in warnings if "Inconsistent" in w.message]
    assert len(consistency_warnings) == 0


def test_mixed_alternatives_warning(evaluator, sample_glossary, basic_chunk):
    """Test that mixing primary and alternative creates WARNING"""
    basic_chunk.source_text = "Mr. Bennet and Mr. Bennet's friend."
    basic_chunk.translated_text = "Sr. Bennet y el amigo de señor Bennet."  # Mixed!

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    # Should pass (technically correct) but with warning
    errors = [issue for issue in result.issues if issue.severity == IssueLevel.ERROR]
    assert len(errors) == 0

    # Should have consistency warning
    warnings = [issue for issue in result.issues if issue.severity == IssueLevel.WARNING]
    consistency_warnings = [w for w in warnings if "Inconsistent" in w.message]
    assert len(consistency_warnings) >= 1

    # Check warning mentions both variants
    warning_msg = consistency_warnings[0].message
    assert "Sr. Bennet" in warning_msg or "señor Bennet" in warning_msg


def test_multiple_terms_consistency(evaluator, sample_glossary, basic_chunk):
    """Test that multiple terms can each be consistent"""
    basic_chunk.source_text = "Mr. Bennet discussed entailment and entailment laws."
    basic_chunk.translated_text = "Sr. Bennet discutió la vinculación y las leyes de vinculación."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    # No consistency warnings
    warnings = [issue for issue in result.issues if issue.severity == IssueLevel.WARNING]
    consistency_warnings = [w for w in warnings if "Inconsistent" in w.message]
    assert len(consistency_warnings) == 0


def test_single_occurrence_always_consistent(evaluator, sample_glossary, basic_chunk):
    """Test that single occurrence can't be inconsistent"""
    basic_chunk.source_text = "Mr. Bennet arrived."
    basic_chunk.translated_text = "Sr. Bennet llegó."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    warnings = [issue for issue in result.issues if issue.severity == IssueLevel.WARNING]
    consistency_warnings = [w for w in warnings if "Inconsistent" in w.message]
    assert len(consistency_warnings) == 0


# ===== C. MULTI-WORD TERMS TESTS (4 tests) =====

def test_multiword_term_detection(evaluator, sample_glossary, basic_chunk):
    """Test that multi-word terms like 'Elizabeth Bennet' are detected"""
    basic_chunk.source_text = "Elizabeth Bennet was intelligent."
    basic_chunk.translated_text = "Elizabeth Bennet era inteligente."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert result.metadata["glossary_terms_in_source"] >= 1


def test_multiword_with_punctuation(evaluator, sample_glossary, basic_chunk):
    """Test multi-word terms work with punctuation"""
    basic_chunk.source_text = "Elizabeth Bennet, the eldest daughter, was wise."
    basic_chunk.translated_text = "Elizabeth Bennet, la hija mayor, era sabia."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True


def test_partial_name_not_matched(evaluator, sample_glossary, basic_chunk):
    """Test that partial match doesn't count for multi-word term"""
    # "Elizabeth" alone shouldn't match "Elizabeth Bennet" term
    basic_chunk.source_text = "Elizabeth was happy."
    basic_chunk.translated_text = "Elizabeth estaba feliz."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    # Should not find "Elizabeth Bennet" in source
    assert result.passed is True
    # Should show 0 terms found (since "Elizabeth" alone isn't in glossary)
    assert result.metadata["glossary_terms_in_source"] == 0


def test_multiple_multiword_terms(evaluator, sample_glossary, basic_chunk):
    """Test multiple multi-word character names"""
    basic_chunk.source_text = "Elizabeth Bennet met Mr. Bennet at Netherfield."
    basic_chunk.translated_text = "Elizabeth Bennet conoció a Sr. Bennet en Netherfield."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert result.metadata["glossary_terms_in_source"] == 3  # Elizabeth Bennet, Mr. Bennet, Netherfield


# ===== D. EDGE CASES TESTS (5 tests) =====

def test_case_sensitivity_characters(evaluator, sample_glossary, basic_chunk):
    """Test that character names are case-insensitive"""
    basic_chunk.source_text = "mr. bennet arrived."  # Lowercase
    basic_chunk.translated_text = "Sr. Bennet llegó."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    # Should still find "mr. bennet" matching "Mr. Bennet"
    assert result.metadata["glossary_terms_in_source"] >= 1
    assert result.passed is True


def test_case_sensitivity_concepts(evaluator, sample_glossary, basic_chunk):
    """Test that concepts are case-insensitive"""
    basic_chunk.source_text = "The ENTAILMENT was unfair."  # Uppercase
    basic_chunk.translated_text = "La vinculación era injusta."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    # Should find "ENTAILMENT" matching "entailment"
    assert result.metadata["glossary_terms_in_source"] >= 1
    assert result.passed is True


def test_term_with_possessive(evaluator, sample_glossary, basic_chunk):
    """Test handling of possessive forms"""
    basic_chunk.source_text = "Netherfield's garden was beautiful."
    basic_chunk.translated_text = "El jardín de Netherfield era hermoso."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    # Should recognize Netherfield even with possessive
    assert result.metadata["glossary_terms_in_source"] >= 1


def test_term_at_sentence_boundaries(evaluator, sample_glossary, basic_chunk):
    """Test terms at start and end of sentences"""
    basic_chunk.source_text = "Netherfield was grand. Mr. Bennet visited Netherfield."
    basic_chunk.translated_text = "Netherfield era grandioso. Sr. Bennet visitó Netherfield."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    # Both terms should be found
    assert result.metadata["glossary_terms_in_source"] == 2  # Mr. Bennet and Netherfield (2x)


def test_repeated_term_multiple_times(evaluator, sample_glossary, basic_chunk):
    """Test same term repeated many times"""
    basic_chunk.source_text = "Netherfield, Netherfield, Netherfield, Netherfield, Netherfield."
    basic_chunk.translated_text = "Netherfield, Netherfield, Netherfield, Netherfield, Netherfield."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is True
    assert result.metadata["glossary_terms_in_source"] == 1  # One unique term found


# ===== E. CHARACTER POSITIONS TESTS (3 tests) =====

def test_character_positions_reported(evaluator, sample_glossary, basic_chunk):
    """Test that issues include character positions"""
    basic_chunk.source_text = "Mr. Bennet went to Netherfield."
    basic_chunk.translated_text = "El señor fue a casa."  # Missing both terms!

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    # Check that errors mention positions
    for issue in result.issues:
        if issue.severity == IssueLevel.ERROR:
            # Location should contain position information
            assert issue.location is not None


def test_multiple_violations_all_reported(evaluator, sample_glossary, basic_chunk):
    """Test that all violations are reported, not just first"""
    basic_chunk.source_text = "Mr. Bennet, Elizabeth Bennet, and entailment."
    basic_chunk.translated_text = "El hombre, la mujer, y el tema."  # All wrong!

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    # Should have errors for all 3 missing terms
    errors = [issue for issue in result.issues if issue.severity == IssueLevel.ERROR]
    assert len(errors) >= 3


def test_position_accuracy(evaluator, basic_chunk):
    """Test that reported positions are accurate"""
    simple_glossary = Glossary(
        terms=[
            GlossaryTerm(
                english="test",
                spanish="prueba",
                type=GlossaryTermType.CONCEPT,
                alternatives=[]
            )
        ],
        version="1.0.0",
        updated_at=datetime.now()
    )

    basic_chunk.source_text = "The test is a test."  # "test" at positions 4 and 14
    basic_chunk.translated_text = "La prueba es una prueba."

    result = evaluator.evaluate(basic_chunk, {"glossary": simple_glossary})

    # Use internal method to verify position detection
    positions = evaluator._find_term_occurrences(
        basic_chunk.source_text,
        "test",
        GlossaryTermType.CONCEPT
    )
    assert 4 in positions  # "The test" (position 4)
    assert 14 in positions  # "a test" (position 14)


# ===== F. QUALITY SCORING TESTS (2 tests) =====

def test_quality_score_perfect(evaluator, sample_glossary, basic_chunk):
    """Test that perfect compliance gives score of 1.0"""
    basic_chunk.source_text = "Mr. Bennet discussed the entailment."
    basic_chunk.translated_text = "Sr. Bennet discutió la vinculación."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.score == 1.0


def test_quality_score_with_errors(evaluator, sample_glossary, basic_chunk):
    """Test that errors reduce quality score"""
    basic_chunk.source_text = "Mr. Bennet and Elizabeth Bennet discussed entailment."
    basic_chunk.translated_text = "El hombre y Elizabeth Bennet discutieron el tema."
    # Missing: Mr. Bennet (translated incorrectly) and entailment (wrong)

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.score < 1.0
    # 1 out of 3 terms correct = roughly 0.33 score
    assert result.score <= 0.5


# ===== G. REAL-WORLD EXAMPLE TESTS (1 test) =====

def test_pride_and_prejudice_excerpt(evaluator, basic_chunk):
    """Test with actual Pride and Prejudice text using real glossary"""
    pp_glossary = Glossary(
        terms=[
            GlossaryTerm(
                english="Mr. Bennet",
                spanish="Sr. Bennet",
                type=GlossaryTermType.CHARACTER,
                alternatives=["señor Bennet"]
            ),
            GlossaryTerm(
                english="Mrs. Bennet",
                spanish="Sra. Bennet",
                type=GlossaryTermType.CHARACTER,
                alternatives=["señora Bennet"]
            ),
            GlossaryTerm(
                english="Lizzy",
                spanish="Lizzy",
                type=GlossaryTermType.CHARACTER,
                alternatives=["Liza"]
            ),
            GlossaryTerm(
                english="Netherfield Park",
                spanish="Netherfield Park",
                type=GlossaryTermType.PLACE,
                alternatives=[]
            ),
        ],
        version="1.0.0",
        updated_at=datetime.now()
    )

    basic_chunk.source_text = (
        '"My dear Mr. Bennet," said his lady to him one day, '
        '"have you heard that Netherfield Park is let at last?"'
    )
    basic_chunk.translated_text = (
        '"Mi querido Sr. Bennet," le dijo su esposa un día, '
        '"¿has oído que Netherfield Park se ha alquilado por fin?"'
    )

    result = evaluator.evaluate(basic_chunk, {"glossary": pp_glossary})

    assert result.passed is True
    assert result.score == 1.0
    # Should find Mr. Bennet and Netherfield Park
    assert result.metadata["glossary_terms_in_source"] == 2


# ===== H. EMPTY/NULL INPUT TESTS (3 additional tests) =====

def test_empty_translation(evaluator, sample_glossary, basic_chunk):
    """Test handling of empty translation"""
    basic_chunk.source_text = "Mr. Bennet said hello."
    basic_chunk.translated_text = ""

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    assert len(result.issues) > 0
    assert result.score == 0.0


def test_empty_source(evaluator, sample_glossary, basic_chunk):
    """Test handling of empty source text"""
    basic_chunk.source_text = ""
    basic_chunk.translated_text = "Algún texto."

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    assert len(result.issues) > 0


def test_none_translation(evaluator, sample_glossary, basic_chunk):
    """Test handling of None translation"""
    basic_chunk.source_text = "Mr. Bennet said hello."
    basic_chunk.translated_text = None

    result = evaluator.evaluate(basic_chunk, {"glossary": sample_glossary})

    assert result.passed is False
    assert len(result.issues) > 0
    assert result.score == 0.0
