"""Tests for chunking module."""

import pytest
from pathlib import Path
from datetime import datetime

from src.chunker import (
    chunk_chapter,
    _generate_chunk_id,
    _calculate_overlap,
    _calculate_chunk_metadata,
    _validate_chunk_size
)
from src.models import ChunkingConfig, ChunkingMethod, ChunkMetadata, ChunkStatus


class TestGenerateChunkId:
    """Tests for _generate_chunk_id function."""

    def test_first_chunk(self):
        """Test ID generation for first chunk."""
        chunk_id = _generate_chunk_id("chapter_01", 0)
        assert chunk_id == "chapter_01_chunk_000"

    def test_tenth_chunk(self):
        """Test ID generation for double-digit position."""
        chunk_id = _generate_chunk_id("chapter_01", 9)
        assert chunk_id == "chapter_01_chunk_009"

    def test_hundredth_chunk(self):
        """Test ID generation for triple-digit position."""
        chunk_id = _generate_chunk_id("chapter_01", 99)
        assert chunk_id == "chapter_01_chunk_099"

    def test_different_chapter(self):
        """Test with different chapter ID."""
        chunk_id = _generate_chunk_id("chapter_42", 5)
        assert chunk_id == "chapter_42_chunk_005"


class TestCalculateOverlap:
    """Tests for _calculate_overlap function with dual-constraint logic."""

    def test_long_paragraphs_meets_both_constraints(self):
        """Test overlap with long paragraphs - paragraph count sufficient."""
        # Each paragraph ~50 words
        paragraphs = [
            " ".join(["word"] * 50),  # 50 words
            " ".join(["word"] * 50),  # 50 words
            " ".join(["word"] * 50),  # 50 words
        ]

        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=50)
        overlap = _calculate_overlap(paragraphs, config)

        # Should take last 2 paragraphs (100 words total)
        assert len(overlap) == 2
        assert overlap == paragraphs[-2:]

    def test_short_paragraphs_needs_more_for_word_count(self):
        """Test overlap with short dialogue - needs more paragraphs for word minimum."""
        # Each paragraph ~5 words
        paragraphs = [
            "Hello there friend",  # 3 words
            "How are you",  # 3 words
            "I am fine",  # 3 words
            "That is good",  # 3 words
            "Let us go home now",  # 5 words
            "Yes that sounds great",  # 4 words
        ]

        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=15)
        overlap = _calculate_overlap(paragraphs, config)

        # Should take more than 2 paragraphs to reach 15 words
        assert len(overlap) > 2
        # Count words in overlap
        overlap_word_count = sum(len(p.split()) for p in overlap)
        assert overlap_word_count >= 15

    def test_zero_overlap_paragraphs(self):
        """Test with overlap_paragraphs=0."""
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=0, min_overlap_words=10)
        overlap = _calculate_overlap(paragraphs, config)

        # Should still get overlap to meet word count
        assert len(overlap) > 0

    def test_zero_overlap_words(self):
        """Test with min_overlap_words=0."""
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=0)
        overlap = _calculate_overlap(paragraphs, config)

        # Should get 2 paragraphs
        assert len(overlap) == 2

    def test_both_zero_no_overlap(self):
        """Test with both constraints at zero - no overlap."""
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=0, min_overlap_words=0)
        overlap = _calculate_overlap(paragraphs, config)

        assert len(overlap) == 0

    def test_empty_paragraphs(self):
        """Test with empty paragraph list."""
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=100)
        overlap = _calculate_overlap([], config)

        assert len(overlap) == 0

    def test_overlap_exceeds_available_paragraphs(self):
        """Test when overlap requirements exceed available paragraphs."""
        paragraphs = ["Short para"]  # Only 1 paragraph
        config = ChunkingConfig(overlap_paragraphs=5, min_overlap_words=100)
        overlap = _calculate_overlap(paragraphs, config)

        # Should return all available paragraphs
        assert overlap == paragraphs


class TestCalculateChunkMetadata:
    """Tests for _calculate_chunk_metadata function."""

    def test_basic_metadata(self):
        """Test basic metadata calculation."""
        paragraphs = ["Paragraph one", "Paragraph two"]
        metadata = _calculate_chunk_metadata(paragraphs, 0, 0, 0)

        assert metadata.char_start == 0
        assert metadata.overlap_start == 0
        assert metadata.overlap_end == 0
        assert metadata.paragraph_count == 2
        assert metadata.word_count == 4

    def test_with_overlap(self):
        """Test metadata with overlap values."""
        paragraphs = ["Para 1", "Para 2"]
        metadata = _calculate_chunk_metadata(paragraphs, 100, 50, 25)

        assert metadata.char_start == 100
        assert metadata.overlap_start == 50
        assert metadata.overlap_end == 25
        # char_end should be char_start + text length
        chunk_text = "Para 1\n\nPara 2"
        assert metadata.char_end == 100 + len(chunk_text)


class TestValidateChunkSize:
    """Tests for _validate_chunk_size function."""

    def test_chunk_within_bounds(self):
        """Test chunk that meets size requirements."""
        from src.models import Chunk

        chunk = Chunk(
            id="test_chunk",
            chapter_id="ch01",
            position=0,
            source_text=" ".join(["word"] * 1000),  # 1000 words
            metadata=ChunkMetadata(
                char_start=0, char_end=5000,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=1000
            ),
            status=ChunkStatus.PENDING,
            created_at=datetime.now()
        )

        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=2000)
        warnings = _validate_chunk_size(chunk, config)

        assert len(warnings) == 0

    def test_chunk_too_small(self):
        """Test chunk below minimum size."""
        from src.models import Chunk

        chunk = Chunk(
            id="test_chunk",
            chapter_id="ch01",
            position=0,
            source_text=" ".join(["word"] * 300),  # 300 words
            metadata=ChunkMetadata(
                char_start=0, char_end=1500,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=300
            ),
            status=ChunkStatus.PENDING,
            created_at=datetime.now()
        )

        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=2000)
        warnings = _validate_chunk_size(chunk, config)

        assert len(warnings) == 1
        assert "too small" in warnings[0]
        assert "300" in warnings[0]

    def test_chunk_too_large(self):
        """Test chunk above maximum size."""
        from src.models import Chunk

        chunk = Chunk(
            id="test_chunk",
            chapter_id="ch01",
            position=0,
            source_text=" ".join(["word"] * 3500),  # 3500 words
            metadata=ChunkMetadata(
                char_start=0, char_end=17500,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=3500
            ),
            status=ChunkStatus.PENDING,
            created_at=datetime.now()
        )

        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        warnings = _validate_chunk_size(chunk, config)

        assert len(warnings) == 1
        assert "too large" in warnings[0]
        assert "3500" in warnings[0]


class TestChunkChapter:
    """Tests for main chunk_chapter function."""

    def test_small_chapter_single_chunk(self):
        """Test chapter smaller than min_chunk_size creates single chunk."""
        text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
        config = ChunkingConfig(
            target_size=1000,
            min_chunk_size=500,
            overlap_paragraphs=2,
            min_overlap_words=100
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        assert len(chunks) == 1
        assert chunks[0].id == "chapter_01_chunk_000"
        assert chunks[0].chapter_id == "chapter_01"
        assert chunks[0].position == 0
        assert chunks[0].metadata.paragraph_count == 3

    def test_medium_chapter_multiple_chunks(self):
        """Test chapter that creates 2-3 chunks."""
        # Create text with multiple long paragraphs
        paragraphs = [" ".join(["word"] * 200) for _ in range(10)]  # 10 paragraphs, 200 words each
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=800,  # Should create ~2-3 chunks
            overlap_paragraphs=1,
            min_overlap_words=50
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        assert len(chunks) > 1
        # Verify chunk IDs are sequential
        for i, chunk in enumerate(chunks):
            assert chunk.position == i
            assert chunk.id == f"chapter_01_chunk_{i:03d}"

    def test_overlap_appears_in_adjacent_chunks(self):
        """Test that overlap text appears in both adjacent chunks."""
        # Create clear paragraphs
        paragraphs = [f"Paragraph {i} with some content here" for i in range(6)]
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=50,  # Small target to create multiple chunks
            overlap_paragraphs=1,
            min_overlap_words=5
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        if len(chunks) >= 2:
            # Extract overlap from first chunk (end)
            chunk1_paragraphs = chunks[0].source_text.split("\n\n")
            chunk2_paragraphs = chunks[1].source_text.split("\n\n")

            # Last paragraph of chunk1 should be first paragraph of chunk2
            # (if overlap is working correctly)
            assert chunk1_paragraphs[-1] == chunk2_paragraphs[0]

    def test_zero_overlap_configuration(self):
        """Test chunking with no overlap."""
        paragraphs = [f"Paragraph {i}" for i in range(10)]
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=10,  # Small to create multiple chunks
            overlap_paragraphs=0,
            min_overlap_words=0
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        # Verify no overlap in metadata
        for chunk in chunks[1:]:  # Skip first chunk
            assert chunk.metadata.overlap_start == 0

    def test_empty_chapter(self):
        """Test chunking empty text."""
        config = ChunkingConfig()
        chunks = chunk_chapter("", config, "chapter_01")

        assert len(chunks) == 0

    def test_single_paragraph_chapter(self):
        """Test chapter with single paragraph."""
        text = "This is a single paragraph with some content here and there."
        config = ChunkingConfig()

        chunks = chunk_chapter(text, config, "chapter_01")

        assert len(chunks) == 1
        assert chunks[0].metadata.paragraph_count == 1

    def test_chunk_ids_sequential(self):
        """Test that chunk IDs are properly sequential."""
        paragraphs = [" ".join(["word"] * 100) for _ in range(20)]
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(target_size=500)
        chunks = chunk_chapter(text, config, "chapter_42")

        for i, chunk in enumerate(chunks):
            expected_id = f"chapter_42_chunk_{i:03d}"
            assert chunk.id == expected_id
            assert chunk.position == i

    def test_metadata_char_positions(self):
        """Test that char_start and char_end are calculated correctly."""
        text = "Para 1\n\nPara 2\n\nPara 3"
        config = ChunkingConfig(target_size=5, overlap_paragraphs=0, min_overlap_words=0)

        chunks = chunk_chapter(text, config, "chapter_01")

        # First chunk should start at 0
        assert chunks[0].metadata.char_start == 0


class TestIntegration:
    """Integration tests with real fixtures."""

    def test_pride_and_prejudice_fixture(self):
        """Test chunking Pride & Prejudice Chapter 1."""
        fixture_path = Path("tests/fixtures/chapter_sample.txt")

        if not fixture_path.exists():
            pytest.skip("Pride & Prejudice fixture not found")

        text = fixture_path.read_text(encoding='utf-8')

        # Default configuration
        config = ChunkingConfig(
            target_size=2000,
            overlap_paragraphs=2,
            min_overlap_words=100
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        # Should create at least one chunk
        assert len(chunks) > 0

        # All chunks should have required fields
        for chunk in chunks:
            assert chunk.id.startswith("chapter_01_chunk_")
            assert chunk.chapter_id == "chapter_01"
            assert chunk.source_text
            assert chunk.metadata.paragraph_count > 0
            assert chunk.metadata.word_count > 0
            assert chunk.status == ChunkStatus.PENDING

        # First chunk should start at position 0
        assert chunks[0].position == 0
        assert chunks[0].metadata.char_start == 0

        # Chunks should be sequential
        for i, chunk in enumerate(chunks):
            assert chunk.position == i

    def test_different_target_sizes(self):
        """Test chunking with different target_size values."""
        fixture_path = Path("tests/fixtures/chapter_sample.txt")

        if not fixture_path.exists():
            pytest.skip("Pride & Prejudice fixture not found")

        text = fixture_path.read_text(encoding='utf-8')

        # Test with smaller target (more chunks)
        small_config = ChunkingConfig(target_size=500)
        small_chunks = chunk_chapter(text, small_config, "chapter_01")

        # Test with larger target (fewer chunks)
        large_config = ChunkingConfig(target_size=3000)
        large_chunks = chunk_chapter(text, large_config, "chapter_01")

        # Smaller target should create more chunks
        assert len(small_chunks) >= len(large_chunks)

    def test_chunk_save_and_load_integrity(self):
        """Test that chunks can be saved and loaded correctly."""
        text = "Para 1\n\nPara 2\n\nPara 3"
        config = ChunkingConfig()
        chunks = chunk_chapter(text, config, "chapter_01")

        # Test that chunk can be serialized (Pydantic model)
        for chunk in chunks:
            chunk_dict = chunk.model_dump()
            assert "id" in chunk_dict
            assert "source_text" in chunk_dict
            assert "metadata" in chunk_dict


class TestEdgeCases:
    """Additional edge case tests."""

    def test_very_long_single_paragraph(self):
        """Test single paragraph exceeding max_chunk_size."""
        # Create single paragraph with 4000 words
        text = " ".join(["word"] * 4000)

        config = ChunkingConfig(
            target_size=2000,
            max_chunk_size=3000,
            overlap_paragraphs=0,
            min_overlap_words=0
        )

        chunks = chunk_chapter(text, config, "chapter_01")

        # Should create single chunk (can't split paragraphs)
        assert len(chunks) == 1
        # Should be marked as too large (via warnings)
        warnings = _validate_chunk_size(chunks[0], config)
        assert len(warnings) > 0
        assert "too large" in warnings[0]

    def test_mixed_paragraph_lengths(self):
        """Test mix of long and short paragraphs."""
        paragraphs = [
            " ".join(["word"] * 500),  # Long
            "Short",  # Short
            " ".join(["word"] * 300),  # Medium
            "Tiny",  # Tiny
            " ".join(["word"] * 600),  # Long
        ]
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(target_size=800, overlap_paragraphs=1, min_overlap_words=50)
        chunks = chunk_chapter(text, config, "chapter_01")

        assert len(chunks) > 0
        # Verify all paragraphs are captured
        total_chunk_paras = sum(chunk.metadata.paragraph_count for chunk in chunks)
        # Note: With overlap, paragraphs are counted multiple times
        assert total_chunk_paras >= len(paragraphs)

    def test_windows_newlines(self):
        """Test chunking text with Windows-style newlines."""
        text = "Para 1\r\n\r\nPara 2\r\n\r\nPara 3"
        config = ChunkingConfig()

        chunks = chunk_chapter(text, config, "chapter_01")

        assert len(chunks) > 0
        # Should normalize and process correctly
        assert chunks[0].metadata.paragraph_count == 3
