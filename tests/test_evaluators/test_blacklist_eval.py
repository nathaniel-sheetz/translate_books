"""
Tests for BlacklistEvaluator

Tests the blacklist evaluator's ability to detect forbidden words and phrases
with various matching options, severity levels, and variations.
"""

import pytest
from pathlib import Path
from src.models import Chunk, ChunkMetadata, BlacklistEntry, Blacklist, IssueLevel
from src.evaluators.blacklist_eval import BlacklistEvaluator


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


class TestBlacklistEvaluatorBasic:
    """Basic functionality tests."""

    def test_no_blacklist_provided(self):
        """Test evaluation when no blacklist is provided."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {})

        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0
        assert result.metadata["blacklist_entries_checked"] == 0

    def test_empty_blacklist(self):
        """Test evaluation with empty blacklist."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba con coger y zumo",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0

    def test_no_translation_text(self):
        """Test evaluation when no translation provided."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="coger", reason="Test")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0

    def test_no_matches_clean_text(self):
        """Test text without any blacklisted words."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él tomó el libro y lo llevó consigo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="coger", variations=["coger", "cogió"], reason="Test"),
            BlacklistEntry(term="zumo", variations=["zumo", "zumos"], reason="Test")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0


class TestBlacklistMatching:
    """Tests for matching logic."""

    def test_exact_term_match_no_variations(self):
        """Test matching base term when no variations provided."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Esto es un zumo de naranja.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="zumo", variations=[], reason="Use 'jugo'", severity="error")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 1
        assert "zumo" in result.issues[0].message
        assert result.issues[0].severity == IssueLevel.ERROR

    def test_variation_match(self):
        """Test matching a variation from the variations list."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él cogió el libro.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="coger",
                variations=["coger", "cogió", "coge"],
                reason="Offensive in Latin America",
                severity="warning"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        # Warnings don't fail evaluation, but issues should be found
        assert len(result.issues) == 1
        assert "coger" in result.issues[0].message  # Reports base term
        assert "cogió" in result.issues[0].message  # Shows matched word
        assert result.issues[0].severity == IssueLevel.WARNING

    def test_multiple_variation_matches(self):
        """Test finding multiple different variations in same text."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él coge el libro y lo cogió ayer. Si cogería más libros sería mejor.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="coger",
                variations=["coger", "coge", "cogió", "cogería"],
                reason="Offensive",
                severity="warning"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        # Warnings don't fail evaluation
        assert len(result.issues) == 3  # coge, cogió, cogería
        assert result.issues[0].severity == IssueLevel.WARNING

    def test_multiple_occurrences_same_word(self):
        """Test finding multiple occurrences of the same word."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Zumo de naranja, zumo de manzana, y más zumo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="zumo", variations=["zumo"], reason="Use 'jugo'")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 3  # Three occurrences of "zumo"


class TestCaseSensitivity:
    """Tests for case-sensitive and case-insensitive matching."""

    def test_case_insensitive_matching(self):
        """Test case-insensitive matching (default)."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="ZUMO de naranja y Zumo de manzana.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="zumo",
                variations=["zumo"],
                reason="Use 'jugo'",
                case_sensitive=False
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 2  # Both ZUMO and Zumo found

    def test_case_sensitive_matching(self):
        """Test case-sensitive matching."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="ZUMO de naranja y zumo de manzana.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="zumo",
                variations=["zumo"],
                reason="Use 'jugo'",
                case_sensitive=True
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 1  # Only lowercase "zumo" found


class TestWordBoundaries:
    """Tests for whole-word matching."""

    def test_whole_word_matching_avoids_partial_match(self):
        """Test that whole_word prevents matching inside other words."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él recoge el libro y lo recogió ayer.",  # "recoger" should NOT match "coger"
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="coger",
                variations=["coger", "coge", "cogió"],
                reason="Offensive",
                whole_word=True
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.passed
        assert len(result.issues) == 0  # "recoge" and "recogió" should NOT match

    def test_whole_word_false_matches_partial(self):
        """Test that whole_word=False allows partial matches."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él recoge el libro.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="coge",
                variations=["coge"],
                reason="Test",
                whole_word=False
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 1  # "coge" found inside "recoge"


class TestPhraseMatching:
    """Tests for multi-word phrase matching."""

    def test_multi_word_phrase_match(self):
        """Test matching multi-word phrases."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Eso es una tontería absoluta.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="tontería absoluta",
                variations=["tontería absoluta"],
                reason="Too informal",
                severity="warning"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        # Warnings don't fail evaluation
        assert len(result.issues) == 1
        assert "tontería absoluta" in result.issues[0].message


class TestCharacterPositions:
    """Tests for character position tracking."""

    def test_character_positions_tracked(self):
        """Test that character positions are correctly tracked."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="12345 zumo 12345",  # "zumo" starts at position 6
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="zumo", variations=["zumo"], reason="Test")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 1
        assert "position 6" in result.issues[0].message or "char 6" in result.issues[0].location


class TestSeverityLevels:
    """Tests for different severity levels."""

    def test_error_severity(self):
        """Test error severity."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Es un bastardo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="bastardo",
                variations=["bastardo"],
                reason="Offensive",
                severity="error"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert result.issues[0].severity == IssueLevel.ERROR

    def test_warning_severity(self):
        """Test warning severity."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Ese tío es raro.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="tío",
                variations=["tío"],
                reason="Informal",
                severity="warning"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        # Warnings don't fail evaluation
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.WARNING

    def test_info_severity(self):
        """Test info severity."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Esto es informal.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="informal",
                variations=["informal"],
                reason="Style preference",
                severity="info"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        # Info doesn't fail evaluation
        assert len(result.issues) == 1
        assert result.issues[0].severity == IssueLevel.INFO
        assert result.score > 0.5  # Info has low penalty


class TestAlternatives:
    """Tests for suggested alternatives."""

    def test_alternatives_in_suggestions(self):
        """Test that alternatives appear in suggestions."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él cogió el libro.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="coger",
                variations=["cogió"],
                reason="Offensive",
                alternatives=["tomar", "agarrar"]
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert not result.passed
        assert len(result.issues) == 1
        assert result.issues[0].suggestion is not None
        assert "tomar" in result.issues[0].suggestion
        assert "agarrar" in result.issues[0].suggestion


class TestScoreCalculation:
    """Tests for score calculation logic."""

    def test_score_perfect_with_no_issues(self):
        """Test perfect score when no issues found."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Texto limpio sin problemas.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="coger", variations=["coger"], reason="Test")
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.score == 1.0

    def test_score_penalty_for_errors(self):
        """Test score decreases with errors."""
        evaluator = BlacklistEvaluator()
        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Es un bastardo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist = Blacklist(entries=[
            BlacklistEntry(
                term="bastardo",
                variations=["bastardo"],
                reason="Offensive",
                severity="error"
            )
        ])

        result = evaluator.evaluate(chunk, {"blacklist": blacklist})

        assert result.score < 1.0
        assert result.score >= 0.0

    def test_score_lower_penalty_for_warnings(self):
        """Test warnings have lower penalty than errors."""
        evaluator = BlacklistEvaluator()

        # Chunk with error
        chunk_error = Chunk(
            id="test",
            source_text="Test",
            translated_text="Es un bastardo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist_error = Blacklist(entries=[
            BlacklistEntry(term="bastardo", variations=["bastardo"], reason="Test", severity="error")
        ])
        result_error = evaluator.evaluate(chunk_error, {"blacklist": blacklist_error})

        # Chunk with warning
        chunk_warning = Chunk(
            id="test",
            source_text="Test",
            translated_text="Ese tío es raro.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )
        blacklist_warning = Blacklist(entries=[
            BlacklistEntry(term="tío", variations=["tío"], reason="Test", severity="warning")
        ])
        result_warning = evaluator.evaluate(chunk_warning, {"blacklist": blacklist_warning})

        # Warning should have better score than error
        assert result_warning.score > result_error.score


class TestFixtureIntegration:
    """Integration tests using fixture files."""

    def test_with_sample_blacklist_fixture(self):
        """Test using the sample blacklist fixture."""
        evaluator = BlacklistEvaluator()

        # Load fixture
        fixture_path = Path(__file__).parent.parent / "fixtures" / "blacklist_sample.json"

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Él cogió el libro y lo llevó consigo.",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        result = evaluator.evaluate(chunk, {"blacklist_path": fixture_path})

        # Should find "cogió" (warning severity in sample blacklist)
        assert len(result.issues) > 0  # Should find "cogió"

    def test_with_violations_fixture(self):
        """Test using chunk with known violations."""
        from src.utils.file_io import load_chunk

        evaluator = BlacklistEvaluator()

        fixture_path = Path(__file__).parent.parent / "fixtures" / "chunk_blacklist_violations.json"
        blacklist_path = Path(__file__).parent.parent / "fixtures" / "blacklist_sample.json"

        chunk = load_chunk(fixture_path)

        result = evaluator.evaluate(chunk, {"blacklist_path": blacklist_path})

        # Should find: cogió (warning), bastardo (error), zumo (error)
        assert not result.passed  # Should fail due to ERROR-level issues
        assert len(result.issues) == 3

        # Check we have both errors and warnings
        severities = [issue.severity for issue in result.issues]
        assert IssueLevel.ERROR in severities
        assert IssueLevel.WARNING in severities

    def test_with_clean_fixture(self):
        """Test using clean chunk without violations."""
        from src.utils.file_io import load_chunk

        evaluator = BlacklistEvaluator()

        fixture_path = Path(__file__).parent.parent / "fixtures" / "chunk_blacklist_clean.json"
        blacklist_path = Path(__file__).parent.parent / "fixtures" / "blacklist_sample.json"

        chunk = load_chunk(fixture_path)

        result = evaluator.evaluate(chunk, {"blacklist_path": blacklist_path})

        # Should pass - uses "tomó" instead of "cogió", "canalla" instead of "bastardo", "jugo" instead of "zumo"
        assert result.passed
        assert len(result.issues) == 0
        assert result.score == 1.0


class TestFileIOErrors:
    """Tests for file I/O error handling."""

    def test_missing_blacklist_file_raises_error(self):
        """Test that missing blacklist file raises FileNotFoundError."""
        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Path to non-existent file
        missing_path = Path("/tmp/nonexistent_blacklist_12345.json")

        with pytest.raises(FileNotFoundError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": missing_path})

        assert "Blacklist file not found" in str(exc_info.value)
        assert str(missing_path) in str(exc_info.value)

    def test_invalid_json_syntax_raises_error(self, tmp_path):
        """Test that invalid JSON syntax raises JSONDecodeError."""
        import json

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Create file with invalid JSON
        invalid_json_path = tmp_path / "invalid.json"
        invalid_json_path.write_text("{ invalid json syntax ]", encoding='utf-8')

        with pytest.raises(json.JSONDecodeError):
            evaluator.evaluate(chunk, {"blacklist_path": invalid_json_path})

    def test_empty_file_raises_error(self, tmp_path):
        """Test that empty file raises JSONDecodeError."""
        import json

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Create empty file
        empty_path = tmp_path / "empty.json"
        empty_path.write_text("", encoding='utf-8')

        with pytest.raises(json.JSONDecodeError):
            evaluator.evaluate(chunk, {"blacklist_path": empty_path})


class TestInvalidJSONStructure:
    """Tests for invalid JSON structure handling."""

    def test_missing_required_field_term(self, tmp_path):
        """Test that missing required 'term' field raises ValidationError."""
        from pydantic import ValidationError

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but missing required 'term' field
        invalid_path = tmp_path / "missing_term.json"
        invalid_path.write_text(
            '{"entries": [{"reason": "Test reason", "variations": ["test"]}], "version": "1.0"}',
            encoding='utf-8'
        )

        with pytest.raises(ValidationError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": invalid_path})

        assert "term" in str(exc_info.value).lower()

    def test_missing_required_field_reason(self, tmp_path):
        """Test that missing required 'reason' field raises ValidationError."""
        from pydantic import ValidationError

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but missing required 'reason' field
        invalid_path = tmp_path / "missing_reason.json"
        invalid_path.write_text(
            '{"entries": [{"term": "test", "variations": ["test"]}], "version": "1.0"}',
            encoding='utf-8'
        )

        with pytest.raises(ValidationError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": invalid_path})

        assert "reason" in str(exc_info.value).lower()

    def test_invalid_severity_value(self, tmp_path):
        """Test that invalid severity value raises ValidationError."""
        from pydantic import ValidationError

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but invalid severity value
        invalid_path = tmp_path / "invalid_severity.json"
        invalid_path.write_text(
            '{"entries": [{"term": "test", "variations": ["test"], "reason": "Test", "severity": "critical"}], "version": "1.0"}',
            encoding='utf-8'
        )

        with pytest.raises(ValidationError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": invalid_path})

        error_msg = str(exc_info.value).lower()
        assert "severity" in error_msg

    def test_empty_term_raises_error(self, tmp_path):
        """Test that empty term string raises ValidationError."""
        from pydantic import ValidationError

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but empty term (violates min_length=1)
        invalid_path = tmp_path / "empty_term.json"
        invalid_path.write_text(
            '{"entries": [{"term": "", "variations": ["test"], "reason": "Test"}], "version": "1.0"}',
            encoding='utf-8'
        )

        with pytest.raises(ValidationError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": invalid_path})

        error_msg = str(exc_info.value).lower()
        assert "term" in error_msg

    def test_wrong_data_type_for_entries(self, tmp_path):
        """Test that wrong data type for entries raises ValidationError."""
        from pydantic import ValidationError

        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but entries is a string instead of list
        invalid_path = tmp_path / "wrong_type.json"
        invalid_path.write_text(
            '{"entries": "not a list", "version": "1.0"}',
            encoding='utf-8'
        )

        with pytest.raises(ValidationError) as exc_info:
            evaluator.evaluate(chunk, {"blacklist_path": invalid_path})

        error_msg = str(exc_info.value).lower()
        assert "entries" in error_msg or "list" in error_msg

    def test_valid_json_missing_entries_key(self, tmp_path):
        """Test that missing 'entries' key uses default empty list."""
        evaluator = BlacklistEvaluator()

        chunk = Chunk(
            id="test",
            source_text="Test",
            translated_text="Prueba con palabras prohibidas",
            chapter_id="test",
            position=0,
            metadata=make_metadata()
        )

        # Valid JSON but no entries key (should default to empty list)
        no_entries_path = tmp_path / "no_entries.json"
        no_entries_path.write_text('{"version": "1.0"}', encoding='utf-8')

        # Should not raise error - entries defaults to empty list
        result = evaluator.evaluate(chunk, {"blacklist_path": no_entries_path})

        assert result.passed
        assert len(result.issues) == 0
        assert result.metadata["blacklist_entries_checked"] == 0
