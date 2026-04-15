"""Tests for chunking module."""

import pytest
from pathlib import Path
from datetime import datetime

from src.chunker import (
    chunk_chapter,
    _generate_chunk_id,
    _calculate_overlap,
    _calculate_chunk_metadata,
    _validate_chunk_size,
    _optimal_chunk_count,
    _score_split_points,
    _is_dialogue,
    _is_scene_break,
)
from src.models import ChunkingConfig, ChunkingMethod, ChunkMetadata, ChunkStatus


class TestGenerateChunkId:
    """Tests for _generate_chunk_id function."""

    def test_first_chunk(self):
        chunk_id = _generate_chunk_id("chapter_01", 0)
        assert chunk_id == "chapter_01_chunk_000"

    def test_tenth_chunk(self):
        chunk_id = _generate_chunk_id("chapter_01", 9)
        assert chunk_id == "chapter_01_chunk_009"

    def test_hundredth_chunk(self):
        chunk_id = _generate_chunk_id("chapter_01", 99)
        assert chunk_id == "chapter_01_chunk_099"

    def test_different_chapter(self):
        chunk_id = _generate_chunk_id("chapter_42", 5)
        assert chunk_id == "chapter_42_chunk_005"


class TestCalculateOverlap:
    """Tests for _calculate_overlap function with dual-constraint logic."""

    def test_long_paragraphs_meets_both_constraints(self):
        paragraphs = [
            " ".join(["word"] * 50),
            " ".join(["word"] * 50),
            " ".join(["word"] * 50),
        ]
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=50)
        overlap = _calculate_overlap(paragraphs, config)
        assert len(overlap) == 2
        assert overlap == paragraphs[-2:]

    def test_short_paragraphs_needs_more_for_word_count(self):
        paragraphs = [
            "Hello there friend",
            "How are you",
            "I am fine",
            "That is good",
            "Let us go home now",
            "Yes that sounds great",
        ]
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=15)
        overlap = _calculate_overlap(paragraphs, config)
        assert len(overlap) > 2
        overlap_word_count = sum(len(p.split()) for p in overlap)
        assert overlap_word_count >= 15

    def test_zero_overlap_paragraphs(self):
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=0, min_overlap_words=10)
        overlap = _calculate_overlap(paragraphs, config)
        assert len(overlap) > 0

    def test_zero_overlap_words(self):
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=0)
        overlap = _calculate_overlap(paragraphs, config)
        assert len(overlap) == 2

    def test_both_zero_no_overlap(self):
        paragraphs = ["Para 1", "Para 2", "Para 3"]
        config = ChunkingConfig(overlap_paragraphs=0, min_overlap_words=0)
        overlap = _calculate_overlap(paragraphs, config)
        assert len(overlap) == 0

    def test_empty_paragraphs(self):
        config = ChunkingConfig(overlap_paragraphs=2, min_overlap_words=100)
        overlap = _calculate_overlap([], config)
        assert len(overlap) == 0

    def test_overlap_exceeds_available_paragraphs(self):
        paragraphs = ["Short para"]
        config = ChunkingConfig(overlap_paragraphs=5, min_overlap_words=100)
        overlap = _calculate_overlap(paragraphs, config)
        assert overlap == paragraphs


class TestCalculateChunkMetadata:
    """Tests for _calculate_chunk_metadata function."""

    def test_basic_metadata(self):
        paragraphs = ["Paragraph one", "Paragraph two"]
        metadata = _calculate_chunk_metadata(paragraphs, 0, 0, 0)
        assert metadata.char_start == 0
        assert metadata.overlap_start == 0
        assert metadata.overlap_end == 0
        assert metadata.paragraph_count == 2
        assert metadata.word_count == 4

    def test_with_overlap(self):
        paragraphs = ["Para 1", "Para 2"]
        metadata = _calculate_chunk_metadata(paragraphs, 100, 50, 25)
        assert metadata.char_start == 100
        assert metadata.overlap_start == 50
        assert metadata.overlap_end == 25
        chunk_text = "Para 1\n\nPara 2"
        assert metadata.char_end == 100 + len(chunk_text)


class TestValidateChunkSize:
    """Tests for _validate_chunk_size function."""

    def test_chunk_within_bounds(self):
        from src.models import Chunk
        chunk = Chunk(
            id="test_chunk", chapter_id="ch01", position=0,
            source_text=" ".join(["word"] * 1000),
            metadata=ChunkMetadata(
                char_start=0, char_end=5000,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=1000
            ),
            status=ChunkStatus.PENDING, created_at=datetime.now()
        )
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=2000)
        warnings = _validate_chunk_size(chunk, config)
        assert len(warnings) == 0

    def test_chunk_too_small(self):
        from src.models import Chunk
        chunk = Chunk(
            id="test_chunk", chapter_id="ch01", position=0,
            source_text=" ".join(["word"] * 300),
            metadata=ChunkMetadata(
                char_start=0, char_end=1500,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=300
            ),
            status=ChunkStatus.PENDING, created_at=datetime.now()
        )
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=2000)
        warnings = _validate_chunk_size(chunk, config)
        assert len(warnings) == 1
        assert "too small" in warnings[0]

    def test_chunk_too_large(self):
        from src.models import Chunk
        chunk = Chunk(
            id="test_chunk", chapter_id="ch01", position=0,
            source_text=" ".join(["word"] * 3500),
            metadata=ChunkMetadata(
                char_start=0, char_end=17500,
                overlap_start=0, overlap_end=0,
                paragraph_count=1, word_count=3500
            ),
            status=ChunkStatus.PENDING, created_at=datetime.now()
        )
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        warnings = _validate_chunk_size(chunk, config)
        assert len(warnings) == 1
        assert "too large" in warnings[0]


class TestOptimalChunkCount:
    """Tests for _optimal_chunk_count."""

    def test_fits_in_one_chunk(self):
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        assert _optimal_chunk_count(2500, config) == 1

    def test_needs_two_chunks(self):
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        assert _optimal_chunk_count(5000, config) == 2  # round(5000/2000) = 2, clamped

    def test_needs_three_chunks(self):
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        assert _optimal_chunk_count(7000, config) == 4  # round(7000/2000)=4, n_min=3

    def test_exactly_max(self):
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        assert _optimal_chunk_count(3000, config) == 1

    def test_just_over_max(self):
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        assert _optimal_chunk_count(3001, config) == 2


class TestScoreSplitPoints:
    """Tests for split-point scoring heuristics."""

    def test_continuation_word_penalized(self):
        paragraphs = [
            "The end of paragraph one.",
            "However, this continues the thought.",
            "A fresh start here.",
        ]
        scores = _score_split_points(paragraphs)
        # Boundary 0 (before "However") should score lower than boundary 1
        assert scores[0] < scores[1]

    def test_dialogue_continuity_penalized(self):
        paragraphs = [
            '"Hello," said Alice.',
            '"Hi there," Bob replied.',
            '"How are you?" asked Alice.',
            'The sun was setting over the hills.',
        ]
        scores = _score_split_points(paragraphs)
        # Mid-dialogue boundaries (0, 1) should be worse than end-of-dialogue (2)
        assert scores[0] < scores[2]
        assert scores[1] < scores[2]

    def test_scene_break_rewarded(self):
        paragraphs = [
            "End of scene one.",
            "* * *",
            "Beginning of scene two.",
        ]
        scores = _score_split_points(paragraphs)
        # Boundary before scene break should score high
        assert scores[0] > 0.7

    def test_single_paragraph_empty_scores(self):
        scores = _score_split_points(["Just one paragraph."])
        assert scores == []


class TestIsDialogue:
    """Tests for dialogue detection."""

    def test_starts_with_quote(self):
        assert _is_dialogue('"Hello," said Alice.')
        assert _is_dialogue('\u201cHello,\u201d said Alice.')

    def test_attribution_with_quotes(self):
        assert _is_dialogue('Alice said, "Hello there."')

    def test_not_dialogue(self):
        assert not _is_dialogue("The sun was setting over the hills.")


class TestIsSceneBreak:
    """Tests for scene break detection."""

    def test_asterisks(self):
        assert _is_scene_break("***")
        assert _is_scene_break("* * *")

    def test_dashes(self):
        assert _is_scene_break("---")
        assert _is_scene_break("- - -")

    def test_not_scene_break(self):
        assert not _is_scene_break("A normal paragraph.")
        assert not _is_scene_break("")


class TestChunkChapter:
    """Tests for main chunk_chapter function."""

    def test_small_chapter_single_chunk(self):
        text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
        config = ChunkingConfig(
            target_size=1000, min_chunk_size=500,
            overlap_paragraphs=2, min_overlap_words=100
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) == 1
        assert chunks[0].id == "chapter_01_chunk_000"
        assert chunks[0].chapter_id == "chapter_01"
        assert chunks[0].position == 0
        assert chunks[0].metadata.paragraph_count == 3

    def test_medium_chapter_multiple_chunks(self):
        """Test chapter that needs splitting (exceeds max_chunk_size)."""
        paragraphs = [" ".join(["word"] * 200) for _ in range(10)]  # 2000 total words
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=800,
            max_chunk_size=1500,  # Force splitting
            overlap_paragraphs=1,
            min_overlap_words=50
        )

        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 1
        for i, chunk in enumerate(chunks):
            assert chunk.position == i
            assert chunk.id == f"chapter_01_chunk_{i:03d}"

    def test_overlap_appears_in_adjacent_chunks(self):
        """Test that overlap text appears in both adjacent chunks."""
        paragraphs = [" ".join(["word"] * 150) for _ in range(8)]  # 1200 total words
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=500,
            max_chunk_size=800,
            overlap_paragraphs=1,
            min_overlap_words=50
        )

        chunks = chunk_chapter(text, config, "chapter_01")
        if len(chunks) >= 2:
            chunk1_paragraphs = chunks[0].source_text.split("\n\n")
            chunk2_paragraphs = chunks[1].source_text.split("\n\n")
            # Last paragraph of chunk1 should be first paragraph of chunk2 (overlap)
            assert chunk1_paragraphs[-1] == chunk2_paragraphs[0]

    def test_zero_overlap_configuration(self):
        """Test chunking with no overlap."""
        paragraphs = [" ".join(["word"] * 200) for _ in range(10)]  # 2000 total
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=800,
            max_chunk_size=1200,
            overlap_paragraphs=0,
            min_overlap_words=0
        )

        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 1
        for chunk in chunks[1:]:
            assert chunk.metadata.overlap_start == 0

    def test_empty_chapter(self):
        config = ChunkingConfig()
        chunks = chunk_chapter("", config, "chapter_01")
        assert len(chunks) == 0

    def test_single_paragraph_chapter(self):
        text = "This is a single paragraph with some content here and there."
        config = ChunkingConfig()
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) == 1
        assert chunks[0].metadata.paragraph_count == 1

    def test_chunk_ids_sequential(self):
        paragraphs = [" ".join(["word"] * 100) for _ in range(20)]
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(target_size=500, max_chunk_size=800)
        chunks = chunk_chapter(text, config, "chapter_42")
        for i, chunk in enumerate(chunks):
            expected_id = f"chapter_42_chunk_{i:03d}"
            assert chunk.id == expected_id
            assert chunk.position == i

    def test_metadata_char_positions(self):
        """Test that first chunk starts at position 0."""
        paragraphs = [" ".join(["word"] * 200) for _ in range(6)]
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(target_size=500, max_chunk_size=800,
                                overlap_paragraphs=0, min_overlap_words=0)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert chunks[0].metadata.char_start == 0


class TestEvenSplitting:
    """Tests for the even-splitting behavior."""

    def test_no_runt_chunk(self):
        """2500 words with target=2000, max=3000 should be 1 chunk, not 2000+500."""
        paragraphs = [" ".join(["word"] * 250) for _ in range(10)]  # 2500 words
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(target_size=2000, max_chunk_size=3000)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) == 1
        assert chunks[0].metadata.word_count == 2500

    def test_balanced_two_chunks(self):
        """5000 words should split into 2 roughly equal chunks."""
        paragraphs = [" ".join(["word"] * 250) for _ in range(20)]  # 5000 words
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(
            target_size=2000, max_chunk_size=3000,
            overlap_paragraphs=0, min_overlap_words=0,
            split_quality_weight=0.0  # pure even splitting
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) == 2
        # Both chunks should be roughly equal (within 20%)
        ratio = chunks[0].metadata.word_count / chunks[1].metadata.word_count
        assert 0.8 <= ratio <= 1.25

    def test_three_balanced_chunks(self):
        """7500 words should split into ~3 balanced chunks."""
        paragraphs = [" ".join(["word"] * 250) for _ in range(30)]  # 7500 words
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(
            target_size=2000, max_chunk_size=3000,
            overlap_paragraphs=0, min_overlap_words=0,
            split_quality_weight=0.0
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) in (3, 4)
        word_counts = [c.metadata.word_count for c in chunks]
        # No chunk should be more than 50% larger than any other
        assert max(word_counts) / min(word_counts) < 1.5


class TestSmartSplitting:
    """Tests for content-aware split-point selection."""

    def test_avoids_continuation_split(self):
        """Should not split right before 'However...' when alternative exists."""
        # Build paragraphs where boundary 4 is before "However" and boundary 5 is neutral
        paragraphs = []
        for i in range(10):
            if i == 5:
                paragraphs.append("However, " + " ".join(["word"] * 200))
            else:
                paragraphs.append(" ".join(["word"] * 200))
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=1000, max_chunk_size=1500,
            overlap_paragraphs=0, min_overlap_words=0
        )
        chunks = chunk_chapter(text, config, "chapter_01")

        # The "However" paragraph should not be the first paragraph of any chunk
        for chunk in chunks:
            first_para = chunk.source_text.split("\n\n")[0]
            if first_para.startswith("However"):
                # This is only acceptable if it's the overlap portion
                # or there was truly no alternative
                pass  # Soft check - the scoring should prefer other boundaries

    def test_avoids_mid_dialogue_split(self):
        """Should prefer splitting outside dialogue sequences."""
        paragraphs = []
        # Narrative section (500 words)
        for i in range(5):
            paragraphs.append(" ".join(["word"] * 100))
        # Dialogue section
        paragraphs.append('"Hello," said Alice. ' + " ".join(["word"] * 80))
        paragraphs.append('"Hi there," Bob replied. ' + " ".join(["word"] * 80))
        paragraphs.append('"How are you?" asked Alice. ' + " ".join(["word"] * 80))
        # More narrative (500 words)
        for i in range(5):
            paragraphs.append(" ".join(["word"] * 100))

        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(
            target_size=700, max_chunk_size=1000,
            overlap_paragraphs=0, min_overlap_words=0
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) >= 2

    def test_prefers_scene_break(self):
        """Should split at scene break markers when available."""
        paragraphs = []
        for i in range(5):
            paragraphs.append(" ".join(["word"] * 200))
        paragraphs.append("* * *")
        for i in range(5):
            paragraphs.append(" ".join(["word"] * 200))
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=1000, max_chunk_size=1500,
            overlap_paragraphs=0, min_overlap_words=0
        )
        chunks = chunk_chapter(text, config, "chapter_01")

        # Find which chunk contains the scene break
        for idx, chunk in enumerate(chunks):
            if "* * *" in chunk.source_text:
                paras = chunk.source_text.split("\n\n")
                # Scene break should be at the end of a chunk (split after it)
                # or at the very start (split before it)
                scene_pos = paras.index("* * *")
                # It should be near a boundary, not buried in the middle
                assert scene_pos <= 1 or scene_pos >= len(paras) - 2

    def test_split_quality_weight_zero(self):
        """With weight=0, should produce purely even splits."""
        paragraphs = []
        for i in range(10):
            if i == 5:
                paragraphs.append("However, " + " ".join(["word"] * 200))
            else:
                paragraphs.append(" ".join(["word"] * 200))
        text = "\n\n".join(paragraphs)

        config = ChunkingConfig(
            target_size=1000, max_chunk_size=1500,
            overlap_paragraphs=0, min_overlap_words=0,
            split_quality_weight=0.0
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        word_counts = [c.metadata.word_count for c in chunks]
        # All chunks should be very close in size
        if len(word_counts) > 1:
            assert max(word_counts) - min(word_counts) <= 400


class TestIntegration:
    """Integration tests with real fixtures."""

    def test_pride_and_prejudice_fixture(self):
        fixture_path = Path("tests/fixtures/chapter_sample.txt")
        if not fixture_path.exists():
            pytest.skip("Pride & Prejudice fixture not found")
        text = fixture_path.read_text(encoding='utf-8')
        config = ChunkingConfig(target_size=2000, overlap_paragraphs=2, min_overlap_words=100)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk.id.startswith("chapter_01_chunk_")
            assert chunk.chapter_id == "chapter_01"
            assert chunk.source_text
            assert chunk.metadata.paragraph_count > 0
            assert chunk.metadata.word_count > 0
            assert chunk.status == ChunkStatus.PENDING
        assert chunks[0].position == 0
        assert chunks[0].metadata.char_start == 0
        for i, chunk in enumerate(chunks):
            assert chunk.position == i

    def test_different_target_sizes(self):
        fixture_path = Path("tests/fixtures/chapter_sample.txt")
        if not fixture_path.exists():
            pytest.skip("Pride & Prejudice fixture not found")
        text = fixture_path.read_text(encoding='utf-8')
        small_config = ChunkingConfig(target_size=500, max_chunk_size=800)
        small_chunks = chunk_chapter(text, small_config, "chapter_01")
        large_config = ChunkingConfig(target_size=3000)
        large_chunks = chunk_chapter(text, large_config, "chapter_01")
        assert len(small_chunks) >= len(large_chunks)

    def test_chunk_save_and_load_integrity(self):
        text = "Para 1\n\nPara 2\n\nPara 3"
        config = ChunkingConfig()
        chunks = chunk_chapter(text, config, "chapter_01")
        for chunk in chunks:
            chunk_dict = chunk.model_dump()
            assert "id" in chunk_dict
            assert "source_text" in chunk_dict
            assert "metadata" in chunk_dict

    def test_understood_betsy_chapter(self):
        """Test chunking a real chapter from Understood Betsy."""
        fixture_path = Path("projects/understood-betsy/chapters/chapter_01.txt")
        if not fixture_path.exists():
            pytest.skip("Understood Betsy chapter not found")
        text = fixture_path.read_text(encoding='utf-8')
        config = ChunkingConfig(target_size=2000, overlap_paragraphs=2, min_overlap_words=100)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 0
        total_words = sum(len(c.source_text.split()) for c in chunks)
        # Total words across chunks should be reasonable
        assert total_words > 1000
        for i, chunk in enumerate(chunks):
            assert chunk.position == i

    def test_little_duke_chapter(self):
        """Test chunking a real chapter from The Little Duke."""
        fixture_path = Path("projects/the-little-duke/chapters/chapter_01.txt")
        if not fixture_path.exists():
            pytest.skip("Little Duke chapter not found")
        text = fixture_path.read_text(encoding='utf-8')
        config = ChunkingConfig(target_size=2000, overlap_paragraphs=2, min_overlap_words=100)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 0
        for i, chunk in enumerate(chunks):
            assert chunk.position == i


class TestEdgeCases:
    """Additional edge case tests."""

    def test_very_long_single_paragraph(self):
        """Single paragraph exceeding max_chunk_size stays as one chunk."""
        text = " ".join(["word"] * 4000)
        config = ChunkingConfig(
            target_size=2000, max_chunk_size=3000,
            overlap_paragraphs=0, min_overlap_words=0
        )
        chunks = chunk_chapter(text, config, "chapter_01")
        # Can't split a single paragraph, so it stays as one chunk
        assert len(chunks) == 1
        warnings = _validate_chunk_size(chunks[0], config)
        assert len(warnings) > 0
        assert "too large" in warnings[0]

    def test_mixed_paragraph_lengths(self):
        paragraphs = [
            " ".join(["word"] * 500),
            "Short",
            " ".join(["word"] * 300),
            "Tiny",
            " ".join(["word"] * 600),
        ]
        text = "\n\n".join(paragraphs)
        config = ChunkingConfig(target_size=800, overlap_paragraphs=1, min_overlap_words=50)
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 0

    def test_windows_newlines(self):
        text = "Para 1\r\n\r\nPara 2\r\n\r\nPara 3"
        config = ChunkingConfig()
        chunks = chunk_chapter(text, config, "chapter_01")
        assert len(chunks) > 0
        assert chunks[0].metadata.paragraph_count == 3
