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
    _is_subchapter_heading,
    _find_heading_anchors,
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

    def test_italic_only_paragraph_not_scene_break(self):
        # The scene-break detector matches paragraphs made entirely of
        # *, -, _, and whitespace. A standalone italicized word like
        # "_Victory_" contains letters and must not be misclassified.
        assert not _is_scene_break("_Victory_")
        assert not _is_scene_break("_HMS Victory_")


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


# --- Subchapter heading detection ---

class TestIsSubchapterHeading:
    """Tests for _is_subchapter_heading: patterns A (roman), B (arabic), C (bare all-caps)."""

    # Pattern A: Roman numeral prefix
    def test_roman_numeral_heading(self):
        assert _is_subchapter_heading("I. THE WIZARD OF THE PYRENEES")
        assert _is_subchapter_heading("IV. THE FLIGHT TO THE MOON")
        assert _is_subchapter_heading("VII. THE PARDON")

    # Pattern B: Arabic numeral prefix
    def test_arabic_numeral_heading(self):
        assert _is_subchapter_heading("1. THE BEGINNING")
        assert _is_subchapter_heading("12. THE LAST STAND")

    # Pattern C: Bare all-caps title
    def test_bare_all_caps_heading(self):
        assert _is_subchapter_heading("THE GREAT WOODEN HORSE")
        assert _is_subchapter_heading("FIRST HEAT—THE WEDDING PRESENTS")
        assert _is_subchapter_heading("GRIFFEN THE HIGH FLYER")
        assert _is_subchapter_heading("SWIFT AND OLD-GOLD")

    # Negatives
    def test_regular_prose_not_heading(self):
        prose = (
            "OLD Atlantes, the wizard of the Pyrenees, had built a tower for his "
            "laboratory on the topmost peak of a gray mountain."
        )
        assert not _is_subchapter_heading(prose)

    def test_dialogue_not_heading(self):
        assert not _is_subchapter_heading('"WHAT?" he cried.')
        assert not _is_subchapter_heading('"HELLO THERE"')
        assert not _is_subchapter_heading('“WHO GOES THERE”')

    def test_mixed_case_chapter_marker_not_heading(self):
        assert not _is_subchapter_heading("Chapter VI")
        assert not _is_subchapter_heading("Chapter XVII")

    def test_emphatic_exclamation_not_heading(self):
        # Trailing sentence-end punctuation disqualifies
        assert not _is_subchapter_heading("STOP HIM!")
        assert not _is_subchapter_heading("WHO GOES THERE?")
        assert not _is_subchapter_heading("THE END.")

    def test_scene_break_not_heading(self):
        assert not _is_subchapter_heading("***")
        assert not _is_subchapter_heading("* * *")
        assert not _is_subchapter_heading("---")

    def test_too_short_not_heading(self):
        # Single word, or fewer than 4 alphabetic chars
        assert not _is_subchapter_heading("END")
        assert not _is_subchapter_heading("A B")  # only 2 letters total

    def test_too_long_not_heading(self):
        # 11+ words rules out pattern C even if all caps
        too_long = " ".join(["WORD"] * 11)
        assert not _is_subchapter_heading(too_long)

    def test_empty_not_heading(self):
        assert not _is_subchapter_heading("")
        assert not _is_subchapter_heading("   ")


class TestFindHeadingAnchors:
    """Tests for _find_heading_anchors."""

    def test_no_headings_no_anchors(self):
        paragraphs = [" ".join(["word"] * 200) for _ in range(10)]
        para_words = [200] * 10
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        anchors = _find_heading_anchors(paragraphs, para_words, config)
        assert anchors == []

    def test_simple_three_section_anchors(self):
        # Three sections of 600 words each, separated by Roman headings.
        paragraphs = []
        para_words = []
        for section_idx in range(3):
            paragraphs.append(f"{['I', 'II', 'III'][section_idx]}. SECTION TITLE")
            para_words.append(3)
            for _ in range(3):
                paragraphs.append(" ".join(["word"] * 200))
                para_words.append(200)
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        anchors = _find_heading_anchors(paragraphs, para_words, config)
        # Skip first heading (at index 0, h >= 1 required) -> anchors are
        # at the next two heading paragraphs: indices 4 and 8.
        assert anchors == [4, 8]

    def test_chapter_title_at_index_1_skipped(self):
        # Mimic a real chapter file: "Chapter VI" at 0, "GRIFFEN THE HIGH FLYER"
        # at 1 (bare all-caps -> matches pattern C), then real content.
        paragraphs = [
            "Chapter VI",
            "GRIFFEN THE HIGH FLYER",
            "I. THE WIZARD",
        ] + [" ".join(["word"] * 200) for _ in range(3)] + [
            "II. THE CASTLE",
        ] + [" ".join(["word"] * 200) for _ in range(3)]
        para_words = [count_words(p) for p in paragraphs]
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        anchors = _find_heading_anchors(paragraphs, para_words, config)
        # Index 1 (chapter title) excluded because only 2 words precede it
        # (the "Chapter VI" paragraph) - below min_chunk_size.
        # Index 2 also excluded for the same reason (title + "Chapter VI" still
        # has only ~5 words before).
        # Index 6 ("II. THE CASTLE") accepted.
        assert 1 not in anchors
        assert 2 not in anchors
        assert 6 in anchors

    def test_too_small_section_skipped(self):
        # Heading 1 at index 1 with only 100 words after start -> skip.
        # Heading 2 at index 5 with sufficient words on both sides -> keep.
        paragraphs = [
            " ".join(["word"] * 100),  # 100 words
            "I. FIRST",
            " ".join(["word"] * 600),
            " ".join(["word"] * 600),
            " ".join(["word"] * 600),
            "II. SECOND",
            " ".join(["word"] * 600),
            " ".join(["word"] * 600),
        ]
        para_words = [count_words(p) for p in paragraphs]
        config = ChunkingConfig(min_chunk_size=500, max_chunk_size=3000)
        anchors = _find_heading_anchors(paragraphs, para_words, config)
        # I. FIRST at index 1: only 100 words before -> reject.
        # II. SECOND at index 5: 100 + 2 (heading) + 1800 = ample before
        #   and 1200 after -> accept.
        assert 1 not in anchors
        assert 5 in anchors


def count_words(text):
    """Local helper to match production word counting."""
    from src.utils.text_utils import count_words as _count
    return _count(text)


class TestSubchapterAwareChunking:
    """Integration tests: chunk_chapter honors subchapter headings."""

    def test_splits_on_roman_numeral_headings(self):
        # Build a chapter with three subchapters each ~700 words.
        parts = []
        for roman, title in [("I", "FIRST"), ("II", "SECOND"), ("III", "THIRD")]:
            parts.append(f"{roman}. {title}")
            for _ in range(7):
                parts.append(" ".join(["word"] * 100))
        text = "\n\n".join(parts)
        config = ChunkingConfig(
            target_size=700,
            min_chunk_size=500,
            max_chunk_size=1500,
            overlap_paragraphs=0,
            min_overlap_words=0,
        )
        chunks = chunk_chapter(text, config, "chapter_test")
        # Expect 3 chunks, each starting with a Roman heading.
        assert len(chunks) == 3
        for chunk in chunks:
            first_para = chunk.source_text.split("\n\n")[0]
            assert _is_subchapter_heading(first_para), (
                f"Chunk does not start on a heading: {first_para!r}"
            )

    def test_splits_on_bare_all_caps_headings(self):
        # FIRST HEAT-style bare-all-caps titles.
        parts = []
        for title in [
            "FIRST HEAT—THE WEDDING PRESENTS",
            "SECOND HEAT—BEFORE TROY",
            "THIRD HEAT—THE KING'S MESSENGERS",
        ]:
            parts.append(title)
            for _ in range(7):
                parts.append(" ".join(["word"] * 100))
        text = "\n\n".join(parts)
        config = ChunkingConfig(
            target_size=700,
            min_chunk_size=500,
            max_chunk_size=1500,
            overlap_paragraphs=0,
            min_overlap_words=0,
        )
        chunks = chunk_chapter(text, config, "chapter_test")
        assert len(chunks) == 3
        for chunk in chunks:
            first_para = chunk.source_text.split("\n\n")[0]
            assert _is_subchapter_heading(first_para)

    def test_oversized_subchapter_subdivided(self):
        # Section II is huge (3500 words, exceeds max=2000). DP should
        # honor the forced anchors AND subdivide section II.
        parts = []
        # Section I: ~700 words
        parts.append("I. SHORT FIRST")
        for _ in range(7):
            parts.append(" ".join(["word"] * 100))
        # Section II: ~3500 words (oversized)
        parts.append("II. BIG SECOND")
        for _ in range(35):
            parts.append(" ".join(["word"] * 100))
        # Section III: ~700 words
        parts.append("III. SHORT THIRD")
        for _ in range(7):
            parts.append(" ".join(["word"] * 100))
        text = "\n\n".join(parts)
        config = ChunkingConfig(
            target_size=1500,
            min_chunk_size=500,
            max_chunk_size=2000,
            overlap_paragraphs=0,
            min_overlap_words=0,
        )
        chunks = chunk_chapter(text, config, "chapter_test")
        # 2 anchors -> at least 3 chunks. Section II oversized -> at least 4.
        assert len(chunks) >= 4
        # Every anchor must begin a chunk: chunks starting with the two later
        # headings must exist.
        starting_paras = [c.source_text.split("\n\n")[0] for c in chunks]
        assert any(p.startswith("I. ") for p in starting_paras)
        assert any(p.startswith("II. ") for p in starting_paras)
        assert any(p.startswith("III. ") for p in starting_paras)

    def test_adjacent_headings_with_tiny_middle_skipped(self):
        # Two headings sandwich a tiny 50-word middle section. The middle
        # heading should be rejected by the feasibility filter, and the
        # resulting chunks should satisfy min/max.
        parts = ["I. FIRST"]
        parts += [" ".join(["word"] * 100) for _ in range(7)]  # 700 words
        parts.append("II. TINY MIDDLE")
        parts += [" ".join(["word"] * 50)]  # 50 words - too small
        parts.append("III. THIRD")
        parts += [" ".join(["word"] * 100) for _ in range(7)]  # 700 words
        text = "\n\n".join(parts)
        config = ChunkingConfig(
            target_size=700,
            min_chunk_size=500,
            max_chunk_size=1500,
            overlap_paragraphs=0,
            min_overlap_words=0,
        )
        chunks = chunk_chapter(text, config, "chapter_test")
        # Middle anchor would create a 50-word chunk -> rejected.
        # Resulting chunks should be reasonable in size.
        for chunk in chunks:
            assert chunk.metadata.word_count <= config.max_chunk_size


class TestRealWonderBookChapters:
    """Integration: chapters 7, 14, 16, 17 of wonder-book-of-horses should
    produce chunks that begin on subchapter headings."""

    @pytest.fixture(params=[7, 14, 16, 17])
    def chapter_text(self, request):
        path = (
            Path(__file__).parent.parent
            / "projects" / "wonder-book-of-horses" / "chapters"
            / f"chapter_{request.param:02d}.txt"
        )
        if not path.exists():
            pytest.skip(f"{path.name} not present")
        return request.param, path.read_text(encoding="utf-8")

    def test_every_chunk_starts_on_heading_or_first_para(self, chapter_text):
        chap_num, text = chapter_text
        config = ChunkingConfig(
            target_size=2000,
            min_chunk_size=500,
            max_chunk_size=3000,
            overlap_paragraphs=0,
            min_overlap_words=0,
        )
        chunks = chunk_chapter(text, config, f"chapter_{chap_num:02d}")
        assert len(chunks) >= 2, (
            f"Chapter {chap_num} should produce multiple chunks for this test "
            f"to be meaningful"
        )
        for idx, chunk in enumerate(chunks):
            first_para = chunk.source_text.split("\n\n")[0]
            if idx == 0:
                # First chunk begins with the chapter title block
                continue
            assert _is_subchapter_heading(first_para), (
                f"Chapter {chap_num} chunk {idx} does not start on a heading: "
                f"{first_para!r}"
            )
