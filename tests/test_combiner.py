"""Tests for combiner module.

Chunk overlap is disabled (the overlap/combine de-dup path is known-broken; see
docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md #20). Chunks are stitched by plain
concatenation, and any chunk that still carries nonzero overlap metadata is
rejected. These tests cover both behaviors.
"""

import pytest
from datetime import datetime

from src.combiner import (
    combine_chunks,
    validate_chunk_completeness,
)
from src.models import Chunk, ChunkMetadata, ChunkStatus


def create_test_chunk(
    position: int,
    chapter_id: str = "chapter_01",
    source_text: str = "Source text",
    translated_text: str = "Translated text",
    overlap_start: int = 0,
    overlap_end: int = 0
) -> Chunk:
    """Helper to create test chunks."""
    return Chunk(
        id=f"{chapter_id}_chunk_{position:03d}",
        chapter_id=chapter_id,
        position=position,
        source_text=source_text,
        translated_text=translated_text,
        metadata=ChunkMetadata(
            char_start=position * 100,
            char_end=(position + 1) * 100,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
            paragraph_count=1,
            word_count=len(source_text.split())
        ),
        status=ChunkStatus.TRANSLATED,
        created_at=datetime.now()
    )


class TestValidateChunkCompleteness:
    """Tests for validate_chunk_completeness function."""

    def test_valid_chunks(self):
        """Test with valid, complete chunk set."""
        chunks = [
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text="Translation 2"),
            create_test_chunk(2, translated_text="Translation 3"),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is True
        assert len(errors) == 0

    def test_valid_chunks_unsorted(self):
        """Test with valid chunks in wrong order - should still validate."""
        chunks = [
            create_test_chunk(2, translated_text="Translation 3"),
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text="Translation 2"),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is True
        assert len(errors) == 0

    def test_empty_chunk_list(self):
        """Test with empty list."""
        is_valid, errors = validate_chunk_completeness([])

        assert is_valid is False
        assert len(errors) == 1
        assert "No chunks provided" in errors[0]

    def test_missing_chunk_in_sequence(self):
        """Test with gap in sequence (0, 1, 3 - missing 2)."""
        chunks = [
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text="Translation 2"),
            create_test_chunk(3, translated_text="Translation 4"),  # Missing position 2
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Missing chunk positions" in e for e in errors)
        assert any("2" in e for e in errors)

    def test_untranslated_chunk(self):
        """Test with chunk missing translation."""
        chunks = [
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text=None),  # No translation
            create_test_chunk(2, translated_text="Translation 3"),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Untranslated chunks" in e for e in errors)

    def test_empty_translated_text(self):
        """Test with empty translated_text."""
        chunks = [
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text=""),  # Empty
            create_test_chunk(2, translated_text="Translation 3"),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Untranslated" in e for e in errors)

    def test_whitespace_only_translation(self):
        """Test with whitespace-only translation."""
        chunks = [
            create_test_chunk(0, translated_text="Translation 1"),
            create_test_chunk(1, translated_text="   \n  "),  # Whitespace only
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Untranslated" in e for e in errors)

    def test_mismatched_chapter_ids(self):
        """Test with chunks from different chapters."""
        chunks = [
            create_test_chunk(0, chapter_id="chapter_01", translated_text="Trans 1"),
            create_test_chunk(1, chapter_id="chapter_02", translated_text="Trans 2"),
            create_test_chunk(2, chapter_id="chapter_01", translated_text="Trans 3"),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Multiple chapter IDs" in e for e in errors)

    def test_single_chunk_valid(self):
        """Test with single chunk - should be valid."""
        chunks = [create_test_chunk(0, translated_text="Translation")]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is True
        assert len(errors) == 0

    def test_overlap_start_rejected(self):
        """A chunk with nonzero overlap_start is rejected (overlap disabled)."""
        chunks = [
            create_test_chunk(0, translated_text="First", overlap_end=5),
            create_test_chunk(1, translated_text="Second", overlap_start=5),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Overlap/combine de-dup is disabled" in e for e in errors)
        assert any("chapter_01_chunk_001" in e for e in errors)
        assert any("--overlap-paragraphs 0" in e for e in errors)

    def test_overlap_end_only_rejected(self):
        """overlap_end alone is also enough to reject (overlap was created)."""
        chunks = [
            create_test_chunk(0, translated_text="First", overlap_end=4),
            create_test_chunk(1, translated_text="Second", overlap_start=0),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is False
        assert any("Overlap/combine de-dup is disabled" in e for e in errors)

    def test_zero_overlap_not_rejected(self):
        """The common, supported case: zero overlap validates cleanly."""
        chunks = [
            create_test_chunk(0, translated_text="First", overlap_start=0, overlap_end=0),
            create_test_chunk(1, translated_text="Second", overlap_start=0, overlap_end=0),
        ]

        is_valid, errors = validate_chunk_completeness(chunks)

        assert is_valid is True
        assert len(errors) == 0


class TestCombineChunks:
    """Tests for main combine_chunks function (plain concatenation)."""

    def test_single_chunk(self):
        """Test combining single chunk."""
        chunks = [
            create_test_chunk(0, translated_text="Single chunk translation")
        ]

        result = combine_chunks(chunks)

        assert result == "Single chunk translation"

    def test_two_chunks_concatenate(self):
        """Two zero-overlap chunks concatenate with a paragraph break."""
        chunks = [
            create_test_chunk(0, translated_text="First chunk"),
            create_test_chunk(1, translated_text="Second chunk"),
        ]

        result = combine_chunks(chunks)

        assert result == "First chunk\n\nSecond chunk"

    def test_three_chunks_concatenate(self):
        """Three zero-overlap chunks concatenate in order."""
        chunks = [
            create_test_chunk(0, translated_text="Chunk one"),
            create_test_chunk(1, translated_text="Chunk two"),
            create_test_chunk(2, translated_text="Chunk three"),
        ]

        result = combine_chunks(chunks)

        assert result == "Chunk one\n\nChunk two\n\nChunk three"

    def test_unsorted_chunks(self):
        """Test that function sorts chunks correctly before concatenating."""
        chunks = [
            create_test_chunk(2, translated_text="Third"),
            create_test_chunk(0, translated_text="First"),
            create_test_chunk(1, translated_text="Second"),
        ]

        result = combine_chunks(chunks)

        # Should combine in correct order: 0, 1, 2.
        assert result == "First\n\nSecond\n\nThird"

    def test_overlap_start_raises(self):
        """A chunk carrying overlap_start makes combine_chunks raise (blocked)."""
        chunks = [
            create_test_chunk(0, translated_text="First chunk shared text", overlap_end=11),
            create_test_chunk(1, translated_text="shared text second chunk", overlap_start=11),
        ]

        with pytest.raises(ValueError) as exc_info:
            combine_chunks(chunks)

        msg = str(exc_info.value)
        assert "Overlap/combine de-dup is disabled" in msg
        assert "--overlap-paragraphs 0 --min-overlap-words 0" in msg

    def test_overlap_end_raises(self):
        """overlap_end alone also makes combine_chunks raise."""
        chunks = [
            create_test_chunk(0, translated_text="Chunk one overlap", overlap_end=7),
            create_test_chunk(1, translated_text="overlap chunk two", overlap_start=0),
        ]

        with pytest.raises(ValueError) as exc_info:
            combine_chunks(chunks)

        assert "Overlap/combine de-dup is disabled" in str(exc_info.value)

    def test_validation_failure_raises_error(self):
        """Test that validation errors raise ValueError."""
        chunks = [
            create_test_chunk(0, translated_text="First"),
            create_test_chunk(2, translated_text="Third"),  # Missing position 1
        ]

        with pytest.raises(ValueError) as exc_info:
            combine_chunks(chunks)

        assert "validation failed" in str(exc_info.value).lower()
        assert "missing" in str(exc_info.value).lower()

    def test_untranslated_chunk_raises_error(self):
        """Test that untranslated chunks raise error."""
        chunks = [
            create_test_chunk(0, translated_text="First"),
            create_test_chunk(1, translated_text=None),  # Not translated
        ]

        with pytest.raises(ValueError) as exc_info:
            combine_chunks(chunks)

        assert "untranslated" in str(exc_info.value).lower()


class TestIntegration:
    """Integration tests with realistic, zero-overlap scenarios."""

    def test_realistic_paragraph_combination(self):
        """Combining disjoint multi-paragraph chunks preserves structure with no
        duplicates (chunks no longer share an overlap)."""
        chunks = [
            create_test_chunk(
                0,
                translated_text="Párrafo uno.\n\nPárrafo dos.\n\nPárrafo tres.",
            ),
            create_test_chunk(
                1,
                translated_text="Párrafo cuatro.\n\nPárrafo cinco.",
            ),
            create_test_chunk(
                2,
                translated_text="Párrafo seis.",
            ),
        ]

        result = combine_chunks(chunks)

        assert "Párrafo uno" in result
        assert "Párrafo seis" in result
        # Nothing is shared between chunks, so every paragraph appears exactly once.
        for n in ("uno", "dos", "tres", "cuatro", "cinco", "seis"):
            assert result.count(f"Párrafo {n}") == 1

    def test_chunk_and_combine_roundtrip(self):
        """Chunking and combining preserves content structure (zero overlap)."""
        chunks = [
            create_test_chunk(
                0,
                source_text="Para 1\n\nPara 2",
                translated_text="Párrafo 1\n\nPárrafo 2",
            ),
            create_test_chunk(
                1,
                source_text="Para 3\n\nPara 4",
                translated_text="Párrafo 3\n\nPárrafo 4",
            ),
        ]

        result = combine_chunks(chunks)

        # Should have all paragraphs, no duplicates.
        assert result.count("Párrafo 1") == 1
        assert result.count("Párrafo 2") == 1
        assert result.count("Párrafo 3") == 1
        assert result.count("Párrafo 4") == 1
        assert result == "Párrafo 1\n\nPárrafo 2\n\nPárrafo 3\n\nPárrafo 4"

    def test_paragraph_break_preserved_at_zero_overlap_boundary(self):
        """Two paragraph-aligned chunks must remain distinct paragraphs after
        combine_chunks (regression for the ``among-the-farmyard-people/chapter_04``
        'Día tras día' boundary where the inter-chunk \\n\\n was being silently
        dropped).
        """
        chunks = [
            create_test_chunk(
                0,
                translated_text=(
                    "Párrafo A.\n\n"
                    "Párrafo B que es el último del primer trozo."
                ),
            ),
            create_test_chunk(
                1,
                translated_text=(
                    "Párrafo C que abre el segundo trozo.\n\n"
                    "Párrafo D."
                ),
            ),
        ]

        result = combine_chunks(chunks)
        paragraphs = result.split("\n\n")

        assert paragraphs == [
            "Párrafo A.",
            "Párrafo B que es el último del primer trozo.",
            "Párrafo C que abre el segundo trozo.",
            "Párrafo D.",
        ]

    def test_paragraph_break_preserved_with_trailing_newline(self):
        """A chunk whose translation ends with a stray trailing newline
        (a common LLM artifact — chapter_04_chunk_000 ends with
        ``"…impaciencia.\\n"``) must still produce a clean paragraph break
        at the chunk boundary, not three newlines.
        """
        chunks = [
            create_test_chunk(0, translated_text="Final del primer trozo.\n"),
            create_test_chunk(1, translated_text="Inicio del segundo trozo."),
        ]

        result = combine_chunks(chunks)

        assert result == "Final del primer trozo.\n\nInicio del segundo trozo."

    def test_paragraph_break_preserved_when_both_sides_dirty(self):
        """A trailing \\n on chunk_0 AND a leading \\n on chunk_1 must still
        produce exactly one \\n\\n paragraph break.
        """
        chunks = [
            create_test_chunk(0, translated_text="Párrafo uno.\n"),
            create_test_chunk(1, translated_text="\nPárrafo dos."),
        ]

        result = combine_chunks(chunks)

        assert result == "Párrafo uno.\n\nPárrafo dos."

    def test_crlf_trailing_newline_normalized(self):
        """A chunk ending with Windows-style \\r\\n must produce a clean
        paragraph break, not a stray \\r before the blank line.
        """
        chunks = [
            create_test_chunk(0, translated_text="Primer párrafo.\r\n"),
            create_test_chunk(1, translated_text="Segundo párrafo."),
        ]

        result = combine_chunks(chunks)

        assert "\r" not in result
        assert result == "Primer párrafo.\n\nSegundo párrafo."


class TestEdgeCases:
    """Additional edge case tests (all zero-overlap)."""

    def test_many_chunks_concatenate_in_order(self):
        """Many uniformly sized chunks concatenate in position order."""
        chunks = [
            create_test_chunk(i, translated_text=f"Chunk {i} text")
            for i in range(5)
        ]

        result = combine_chunks(chunks)

        assert result == "\n\n".join(f"Chunk {i} text" for i in range(5))

    def test_any_overlap_in_a_long_set_blocks_combine(self):
        """A single overlapping chunk anywhere in the set blocks the whole combine."""
        chunks = [
            create_test_chunk(0, translated_text="A"),
            create_test_chunk(1, translated_text="B"),
            create_test_chunk(2, translated_text="C", overlap_start=1),  # the offender
            create_test_chunk(3, translated_text="D"),
        ]

        with pytest.raises(ValueError) as exc_info:
            combine_chunks(chunks)

        assert "chapter_01_chunk_002" in str(exc_info.value)
