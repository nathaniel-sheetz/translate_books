"""Tests for combiner module."""

import pytest
from datetime import datetime

from src.combiner import (
    combine_chunks,
    validate_chunk_completeness,
    _remove_start_overlap
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


class TestRemoveStartOverlap:
    """Tests for _remove_start_overlap helper function."""

    def test_basic_removal(self):
        """Test removing characters from start."""
        text = "overlap text here"
        result = _remove_start_overlap(text, 8)
        assert result == "text here"

    def test_zero_overlap(self):
        """Test with zero overlap - should return full text."""
        text = "full text here"
        result = _remove_start_overlap(text, 0)
        assert result == text

    def test_negative_overlap(self):
        """Test with negative overlap - treated as zero."""
        text = "full text"
        result = _remove_start_overlap(text, -5)
        assert result == text

    def test_overlap_exceeds_length(self):
        """Test when overlap > text length - returns empty."""
        text = "short"
        result = _remove_start_overlap(text, 100)
        assert result == ""

    def test_overlap_equals_length(self):
        """Test when overlap equals text length."""
        text = "exact"
        result = _remove_start_overlap(text, len(text))
        assert result == ""


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


class TestCombineChunks:
    """Tests for main combine_chunks function."""

    def test_single_chunk(self):
        """Test combining single chunk."""
        chunks = [
            create_test_chunk(0, translated_text="Single chunk translation")
        ]

        result = combine_chunks(chunks)

        assert result == "Single chunk translation"

    def test_two_chunks_with_overlap(self):
        """Test combining 2 chunks with overlap."""
        # Chunk 1: "First chunk" + overlap "shared text"
        # Chunk 2: overlap "shared text" + "second chunk"
        # After overlap removal, non-overlap is " second chunk" (leading space
        # is a toy-data artifact from word-level overlap, not paragraph text).
        # combine_chunks inserts \n\n at every chunk boundary.

        chunks = [
            create_test_chunk(
                0,
                translated_text="First chunk shared text",
                overlap_start=0,  # First chunk has no overlap at start
                overlap_end=11  # "shared text" = 11 chars
            ),
            create_test_chunk(
                1,
                translated_text="shared text second chunk",
                overlap_start=11,  # "shared text" = 11 chars to remove
                overlap_end=0
            )
        ]

        result = combine_chunks(chunks)

        # combine_chunks normalizes the chunk boundary to a paragraph break.
        assert result == "First chunk shared text\n\n second chunk"

    def test_three_chunks_with_overlap(self):
        """Test combining 3 chunks with varying overlaps."""
        chunks = [
            create_test_chunk(
                0,
                translated_text="Chunk one overlap",
                overlap_start=0,
                overlap_end=7  # "overlap" = 7 chars
            ),
            create_test_chunk(
                1,
                translated_text="overlap chunk two ending",
                overlap_start=7,  # Remove "overlap"
                overlap_end=6  # "ending" = 6 chars
            ),
            create_test_chunk(
                2,
                translated_text="ending chunk three",
                overlap_start=6,  # Remove "ending"
                overlap_end=0
            )
        ]

        result = combine_chunks(chunks)

        # combine_chunks normalizes each chunk boundary to a paragraph break.
        assert result == "Chunk one overlap\n\n chunk two ending\n\n chunk three"

    def test_zero_overlap_combination(self):
        """Test combining chunks with no overlap."""
        chunks = [
            create_test_chunk(
                0,
                translated_text="First chunk",
                overlap_start=0,
                overlap_end=0
            ),
            create_test_chunk(
                1,
                translated_text="Second chunk",
                overlap_start=0,
                overlap_end=0
            )
        ]

        result = combine_chunks(chunks)

        # Chunk boundaries are always paragraph boundaries (the chunker
        # only splits on \n\n), so combine_chunks inserts the separator.
        assert result == "First chunk\n\nSecond chunk"

    def test_unsorted_chunks(self):
        """Test that function sorts chunks correctly."""
        chunks = [
            create_test_chunk(2, translated_text="Second Third", overlap_start=6),
            create_test_chunk(0, translated_text="First chunk", overlap_start=0),
            create_test_chunk(1, translated_text="chunk Second", overlap_start=5),
        ]

        result = combine_chunks(chunks)

        # Should combine in correct order: 0, 1, 2, with paragraph-break
        # separators inserted between chunks.
        assert result == "First chunk\n\n Second\n\n Third"

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
    """Integration tests with realistic scenarios."""

    def test_realistic_paragraph_combination(self):
        """Test combining chunks with paragraph structure."""
        # Simulate chunks with paragraph breaks
        chunks = [
            create_test_chunk(
                0,
                translated_text="Párrafo uno.\n\nPárrafo dos.\n\nPárrafo tres compartido.",
                overlap_start=0,
                overlap_end=25  # "Párrafo tres compartido." = 25 chars
            ),
            create_test_chunk(
                1,
                translated_text="Párrafo tres compartido.\n\nPárrafo cuatro.\n\nPárrafo cinco final.",
                overlap_start=25,
                overlap_end=22  # "Párrafo cinco final." = 22 chars (actually 21 + space)
            ),
            create_test_chunk(
                2,
                translated_text="Párrafo cinco final.\n\nPárrafo seis.",
                overlap_start=22,
                overlap_end=0
            )
        ]

        result = combine_chunks(chunks)

        # Verify structure is maintained
        assert "Párrafo uno" in result
        assert "Párrafo seis" in result
        # Verify overlap was handled (shouldn't have duplicates)
        assert result.count("Párrafo tres compartido") == 1
        assert result.count("Párrafo cinco final") == 1

    def test_chunk_and_combine_roundtrip(self):
        """Test that chunking and combining preserves content structure."""
        # This would ideally use actual chunker, but we'll simulate
        original_text = "Para 1\n\nPara 2\n\nPara 3\n\nPara 4"

        # Simulate 2 chunks with overlap
        chunks = [
            create_test_chunk(
                0,
                source_text="Para 1\n\nPara 2",
                translated_text="Párrafo 1\n\nPárrafo 2",
                overlap_start=0,
                overlap_end=11  # "Párrafo 2" + newlines
            ),
            create_test_chunk(
                1,
                source_text="Para 2\n\nPara 3\n\nPara 4",
                translated_text="Párrafo 2\n\nPárrafo 3\n\nPárrafo 4",
                overlap_start=11,  # len("Párrafo 2\n\n") == 11
                overlap_end=0
            )
        ]

        result = combine_chunks(chunks)

        # Should have all paragraphs, no duplicates
        assert result.count("Párrafo 1") == 1
        assert result.count("Párrafo 2") == 1
        assert result.count("Párrafo 3") == 1
        assert result.count("Párrafo 4") == 1

    def test_paragraph_break_preserved_at_zero_overlap_boundary(self):
        """Two paragraph-aligned chunks with overlap_start=0 must remain
        distinct paragraphs after combine_chunks (regression for the
        ``among-the-farmyard-people/chapter_04`` 'Día tras día' boundary
        where the inter-chunk \\n\\n was being silently dropped).
        """
        chunks = [
            create_test_chunk(
                0,
                translated_text=(
                    "Párrafo A.\n\n"
                    "Párrafo B que es el último del primer trozo."
                ),
                overlap_start=0,
                overlap_end=0,
            ),
            create_test_chunk(
                1,
                translated_text=(
                    "Párrafo C que abre el segundo trozo.\n\n"
                    "Párrafo D."
                ),
                overlap_start=0,
                overlap_end=0,
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
            create_test_chunk(
                0,
                translated_text="Final del primer trozo.\n",
                overlap_start=0,
                overlap_end=0,
            ),
            create_test_chunk(
                1,
                translated_text="Inicio del segundo trozo.",
                overlap_start=0,
                overlap_end=0,
            ),
        ]

        result = combine_chunks(chunks)

        assert result == (
            "Final del primer trozo.\n\nInicio del segundo trozo."
        )

    def test_paragraph_break_preserved_when_both_sides_dirty(self):
        """A trailing \\n on chunk_0 AND a leading \\n on chunk_1's non-overlap
        text must still produce exactly one \\n\\n paragraph break.
        """
        chunks = [
            create_test_chunk(
                0,
                translated_text="Párrafo uno.\n",
                overlap_start=0,
                overlap_end=0,
            ),
            create_test_chunk(
                1,
                translated_text="\nPárrafo dos.",
                overlap_start=0,
                overlap_end=0,
            ),
        ]

        result = combine_chunks(chunks)

        assert result == "Párrafo uno.\n\nPárrafo dos."

    def test_crlf_trailing_newline_normalized(self):
        """A chunk ending with Windows-style \\r\\n must produce a clean
        paragraph break, not a stray \\r before the blank line.
        """
        chunks = [
            create_test_chunk(
                0,
                translated_text="Primer párrafo.\r\n",
                overlap_start=0,
                overlap_end=0,
            ),
            create_test_chunk(
                1,
                translated_text="Segundo párrafo.",
                overlap_start=0,
                overlap_end=0,
            ),
        ]

        result = combine_chunks(chunks)

        assert "\r" not in result
        assert result == "Primer párrafo.\n\nSegundo párrafo."

    def test_empty_non_overlap_does_not_append_separator(self):
        """When overlap removal consumes the entire chunk text, no spurious
        \\n\\n should be appended to chapter_text.
        """
        chunks = [
            create_test_chunk(
                0,
                translated_text="Only chunk text.",
                overlap_start=0,
                overlap_end=16,  # entire text is 'overlap'
            ),
            create_test_chunk(
                1,
                translated_text="Only chunk text.",
                overlap_start=16,  # overlap_start >= len(text) → empty non-overlap
                overlap_end=0,
            ),
        ]

        result = combine_chunks(chunks)

        assert not result.endswith("\n\n"), "spurious \\n\\n appended after empty non-overlap"
        assert result == "Only chunk text."


class TestEdgeCases:
    """Additional edge case tests."""

    def test_very_small_overlap(self):
        """Test with 1 character overlap."""
        chunks = [
            create_test_chunk(0, translated_text="Firstx", overlap_end=1),
            create_test_chunk(1, translated_text="x Second", overlap_start=1),
        ]

        result = combine_chunks(chunks)
        assert result == "Firstx\n\n Second"
        # "x" should appear only once

    def test_large_overlap(self):
        """Test with overlap larger than half of chunk."""
        chunks = [
            create_test_chunk(
                0,
                translated_text="First chunk with long overlap text here",
                overlap_end=25  # 25 chars
            ),
            create_test_chunk(
                1,
                translated_text="long overlap text here and more",
                overlap_start=25
            )
        ]

        result = combine_chunks(chunks)
        assert "long overlap text here" in result
        # Should appear once, not twice

    def test_all_chunks_same_size(self):
        """Test with uniformly sized chunks."""
        chunks = [
            create_test_chunk(i, translated_text=f"Chunk {i} text", overlap_start=5 if i > 0 else 0)
            for i in range(5)
        ]

        result = combine_chunks(chunks)
        # Chunk 0 keeps full text; for i>0 the first 5 chars ("Chunk") are stripped as overlap
        assert "Chunk 0 text" in result
        assert all(f"{i} text" in result for i in range(1, 5))

    def test_mixed_overlap_sizes(self):
        """Test with varying overlap sizes between chunks."""
        chunks = [
            create_test_chunk(0, translated_text="First AB", overlap_start=0, overlap_end=2),
            create_test_chunk(1, translated_text="AB Second ABCD", overlap_start=2, overlap_end=4),
            create_test_chunk(2, translated_text="ABCD Third ABCDEF", overlap_start=4, overlap_end=6),
            create_test_chunk(3, translated_text="ABCDEF Fourth", overlap_start=6, overlap_end=0),
        ]

        result = combine_chunks(chunks)

        # Check all chunks are represented
        assert "First" in result
        assert "Second" in result
        assert "Third" in result
        assert "Fourth" in result
