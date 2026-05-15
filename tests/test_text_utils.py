"""Tests for text utility functions."""

import pytest
from pathlib import Path
from datetime import datetime

from src.utils.text_utils import (
    normalize_newlines,
    count_words,
    extract_paragraphs,
    count_paragraphs,
    detect_paragraph_boundaries,
    strip_image_placeholders,
    image_placeholder_ranges,
    image_placeholder_instruction,
)


class TestNormalizeNewlines:
    """Tests for normalize_newlines function."""

    def test_windows_style_newlines(self):
        """Test conversion of Windows-style CRLF."""
        text = "Line 1\r\nLine 2\r\nLine 3"
        result = normalize_newlines(text)
        assert result == "Line 1\nLine 2\nLine 3"

    def test_old_mac_style_newlines(self):
        """Test conversion of old Mac-style CR."""
        text = "Line 1\rLine 2\rLine 3"
        result = normalize_newlines(text)
        assert result == "Line 1\nLine 2\nLine 3"

    def test_unix_style_unchanged(self):
        """Test Unix-style LF remains unchanged."""
        text = "Line 1\nLine 2\nLine 3"
        result = normalize_newlines(text)
        assert result == text

    def test_mixed_newline_styles(self):
        """Test handling of mixed newline styles."""
        text = "Line 1\r\nLine 2\nLine 3\rLine 4"
        result = normalize_newlines(text)
        assert result == "Line 1\nLine 2\nLine 3\nLine 4"


class TestCountWords:
    """Tests for count_words function."""

    def test_normal_text(self):
        """Test word counting in normal English text."""
        text = "The quick brown fox jumps over the lazy dog"
        assert count_words(text) == 9

    def test_empty_string(self):
        """Test empty string returns 0."""
        assert count_words("") == 0

    def test_whitespace_only(self):
        """Test whitespace-only string returns 0."""
        assert count_words("   \t  \n  ") == 0

    def test_spanish_with_accents(self):
        """Test word counting with Spanish accented characters."""
        text = "El niño comió pan y bebió agua"
        assert count_words(text) == 7

    def test_multiline_text(self):
        """Test word counting across multiple lines."""
        text = "First line\nSecond line\nThird line"
        assert count_words(text) == 6


class TestExtractParagraphs:
    """Tests for extract_paragraphs function."""

    def test_multiple_paragraphs(self):
        """Test extracting multiple paragraphs."""
        text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
        result = extract_paragraphs(text)
        assert result == ["Paragraph 1", "Paragraph 2", "Paragraph 3"]

    def test_triple_newlines(self):
        """Test that 3+ newlines still create single boundary."""
        text = "Paragraph 1\n\n\nParagraph 2\n\n\n\nParagraph 3"
        result = extract_paragraphs(text)
        assert result == ["Paragraph 1", "Paragraph 2", "Paragraph 3"]

    def test_single_paragraph(self):
        """Test text with no paragraph breaks."""
        text = "This is a single paragraph with no breaks."
        result = extract_paragraphs(text)
        assert result == [text]

    def test_empty_text(self):
        """Test empty text returns empty list."""
        assert extract_paragraphs("") == []

    def test_whitespace_only_text(self):
        """Test whitespace-only text returns empty list."""
        assert extract_paragraphs("   \n\n   \n  ") == []

    def test_paragraphs_with_internal_newlines(self):
        """Test that single newlines within paragraphs are preserved."""
        text = "Line 1\nLine 2\n\nLine 3\nLine 4"
        result = extract_paragraphs(text)
        assert result == ["Line 1\nLine 2", "Line 3\nLine 4"]


class TestCountParagraphs:
    """Tests for count_paragraphs function."""

    def test_multiple_paragraphs(self):
        """Test counting multiple paragraphs."""
        text = "Para 1\n\nPara 2\n\nPara 3\n\nPara 4"
        assert count_paragraphs(text) == 4

    def test_single_paragraph(self):
        """Test single paragraph returns 1."""
        text = "Just one paragraph here."
        assert count_paragraphs(text) == 1

    def test_empty_text(self):
        """Test empty text returns 0."""
        assert count_paragraphs("") == 0


class TestDetectParagraphBoundaries:
    """Tests for detect_paragraph_boundaries function."""

    def test_three_paragraphs(self):
        """Test boundary detection with 3 paragraphs."""
        text = "Para 1\n\nPara 2\n\nPara 3"
        boundaries = detect_paragraph_boundaries(text)
        assert len(boundaries) == 3
        assert boundaries[0] == 0
        # Para 2 starts after "Para 1\n\n" (8 characters)
        assert boundaries[1] == 8
        # Para 3 starts after "Para 1\n\nPara 2\n\n" (16 characters)
        assert boundaries[2] == 16

    def test_excessive_blank_lines(self):
        """Test that multiple blank lines don't create extra boundaries."""
        text = "Para 1\n\n\n\nPara 2"
        boundaries = detect_paragraph_boundaries(text)
        assert len(boundaries) == 2

    def test_single_paragraph(self):
        """Test single paragraph returns [0]."""
        text = "Just one paragraph"
        boundaries = detect_paragraph_boundaries(text)
        assert boundaries == [0]

    def test_empty_text(self):
        """Test empty text returns empty list."""
        assert detect_paragraph_boundaries("") == []

    def test_boundaries_align_with_paragraphs(self):
        """Test that boundaries correctly point to paragraph starts."""
        text = "First paragraph\n\nSecond paragraph\n\nThird paragraph"
        boundaries = detect_paragraph_boundaries(text)
        paragraphs = extract_paragraphs(text)

        # Normalize for comparison
        normalized_text = normalize_newlines(text)

        # Extract text at each boundary and verify it matches
        for i, boundary in enumerate(boundaries):
            if i < len(boundaries) - 1:
                # Extract from this boundary to next
                extracted = normalized_text[boundary:boundaries[i + 1]]
            else:
                # Last paragraph goes to end
                extracted = normalized_text[boundary:]

            # Should match the extracted paragraph (after stripping)
            assert extracted.strip() == paragraphs[i]


class TestIntegration:
    """Integration tests with real fixtures."""

    def test_word_count_matches_chunk_model(self):
        """Ensure count_words matches Chunk.word_count behavior."""
        try:
            from src.models import Chunk, ChunkMetadata, ChunkStatus
        except ImportError:
            pytest.skip("Pydantic not available in test environment")

        test_text = "The quick brown fox jumps over the lazy dog."

        # Our utility
        our_count = count_words(test_text)

        # Chunk model's computed property uses the same split() method
        chunk = Chunk(
            id="test",
            chapter_id="ch01",
            position=0,
            source_text=test_text,
            metadata=ChunkMetadata(
                char_start=0,
                char_end=len(test_text),
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=9
            ),
            status=ChunkStatus.PENDING,
            created_at=datetime.now()
        )

        chunk_count = chunk.word_count  # Uses computed_field with split()

        assert our_count == chunk_count
        assert our_count == 9

    def test_pride_and_prejudice_fixture(self):
        """Test with real Pride & Prejudice chapter fixture."""
        fixture_path = Path("tests/fixtures/chapter_sample.txt")

        if not fixture_path.exists():
            pytest.skip("Pride & Prejudice fixture not found")

        text = fixture_path.read_text(encoding='utf-8')

        # Extract paragraphs
        paragraphs = extract_paragraphs(text)

        # Count should match
        assert count_paragraphs(text) == len(paragraphs)
        assert len(paragraphs) > 0, "Should have at least one paragraph"

        # Boundaries should match paragraph count
        boundaries = detect_paragraph_boundaries(text)
        assert len(boundaries) == len(paragraphs)

        # First boundary should be 0
        assert boundaries[0] == 0

        # Verify word count
        total_words = count_words(text)
        assert total_words > 100, "Chapter should have substantial word count"

        # Verify each boundary points to start of a paragraph
        normalized_text = normalize_newlines(text)
        for i, boundary in enumerate(boundaries):
            # Extract text from this boundary to next (or end)
            if i < len(boundaries) - 1:
                paragraph_text = normalized_text[boundary:boundaries[i + 1]]
            else:
                paragraph_text = normalized_text[boundary:]

            # Should match extracted paragraph (after stripping)
            assert paragraph_text.strip() == paragraphs[i]


class TestEdgeCases:
    """Additional edge case tests."""

    def test_trailing_newlines(self):
        """Test that trailing newlines don't create extra paragraphs."""
        text = "Para 1\n\nPara 2\n\n\n"
        paragraphs = extract_paragraphs(text)
        assert len(paragraphs) == 2

    def test_leading_newlines(self):
        """Test that leading newlines are handled correctly."""
        text = "\n\nPara 1\n\nPara 2"
        paragraphs = extract_paragraphs(text)
        assert len(paragraphs) == 2

    def test_windows_newlines_in_paragraphs(self):
        """Test paragraph extraction with Windows-style newlines."""
        text = "Para 1\r\n\r\nPara 2\r\n\r\nPara 3"
        paragraphs = extract_paragraphs(text)
        assert len(paragraphs) == 3

    def test_mixed_indentation(self):
        """Test paragraphs with various indentation."""
        text = "Para 1\n\n  Para 2 with leading spaces\n\n\tPara 3 with tab"
        paragraphs = extract_paragraphs(text)
        # Should strip leading/trailing whitespace
        assert paragraphs[1] == "Para 2 with leading spaces"
        assert paragraphs[2] == "Para 3 with tab"

    def test_very_long_text(self):
        """Test with very long text to ensure performance."""
        # Create text with 100 paragraphs
        paragraphs_list = [f"Paragraph {i} with some content" for i in range(100)]
        text = "\n\n".join(paragraphs_list)

        result = extract_paragraphs(text)
        assert len(result) == 100

        boundaries = detect_paragraph_boundaries(text)
        assert len(boundaries) == 100


class TestStripImagePlaceholders:
    """Tests for strip_image_placeholders function."""

    def test_no_placeholders(self):
        text = "Just regular prose with no images."
        assert strip_image_placeholders(text) == text

    def test_filename_only(self):
        text = "before [IMAGE:images/i01.jpg] after"
        result = strip_image_placeholders(text)
        # Same total length, placeholder replaced with spaces
        assert len(result) == len(text)
        assert "[IMAGE:" not in result
        assert "images/i01.jpg" not in result
        assert result.startswith("before ")
        assert result.endswith(" after")

    def test_with_description(self):
        text = "before [IMAGE:images/i01.jpg:a child smiles] after"
        result = strip_image_placeholders(text)
        assert len(result) == len(text)
        assert "[IMAGE:" not in result
        assert "child" not in result
        assert "smiles" not in result

    def test_offset_preserved(self):
        """Character offsets after the placeholder must line up with the original."""
        text = "X [IMAGE:images/i01.jpg] Y"
        result = strip_image_placeholders(text)
        # The 'Y' should sit at exactly the same index it did in the source.
        assert text.index("Y") == result.index("Y")

    def test_multiple_placeholders(self):
        text = "a [IMAGE:images/a.jpg] b [IMAGE:images/b.jpg:caption] c"
        result = strip_image_placeholders(text)
        assert "IMAGE" not in result
        assert "jpg" not in result
        assert "caption" not in result
        # Non-placeholder content survives.
        assert "a " in result and " b " in result and " c" in result

    def test_empty_string(self):
        assert strip_image_placeholders("") == ""

    def test_none_passthrough(self):
        assert strip_image_placeholders(None) is None


class TestImagePlaceholderRanges:
    """Tests for image_placeholder_ranges function."""

    def test_no_placeholders(self):
        assert image_placeholder_ranges("no images here") == []

    def test_empty_string(self):
        assert image_placeholder_ranges("") == []

    def test_filename_only(self):
        text = "a [IMAGE:images/i01.jpg] b"
        ranges = image_placeholder_ranges(text)
        assert ranges == [(2, 24)]

    def test_with_description(self):
        text = "a [IMAGE:images/i01.jpg:caption] b"
        ranges = image_placeholder_ranges(text)
        assert len(ranges) == 1
        start, end = ranges[0]
        assert text[start:end] == "[IMAGE:images/i01.jpg:caption]"

    def test_multiple_placeholders(self):
        text = "a [IMAGE:a.jpg] b [IMAGE:b.jpg:cap] c"
        ranges = image_placeholder_ranges(text)
        assert len(ranges) == 2
        assert text[ranges[0][0]:ranges[0][1]] == "[IMAGE:a.jpg]"
        assert text[ranges[1][0]:ranges[1][1]] == "[IMAGE:b.jpg:cap]"

    def test_ranges_cover_replacement_whitespace(self):
        """Offsets inside the replaced region must be caught by range check."""
        text = "x [IMAGE:images/img009.jpg:Page header] y"
        ranges = image_placeholder_ranges(text)
        assert len(ranges) == 1
        start, end = ranges[0]
        # Any offset inside the replaced span should be in range
        for offset in range(start, end):
            assert any(s <= offset < e for s, e in ranges)


class TestImagePlaceholderInstruction:
    """Tests for image_placeholder_instruction function."""

    def test_no_placeholders_returns_empty(self):
        assert image_placeholder_instruction("Plain prose.") == ""

    def test_empty_string(self):
        assert image_placeholder_instruction("") == ""

    def test_filename_only(self):
        bullet = image_placeholder_instruction(
            "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two."
        )
        assert bullet != ""
        assert bullet.startswith("   - ")
        assert "[IMAGE:filename.ext]" in bullet
        # Must NOT use the description-aware wording when there are no descriptions.
        assert "image description" not in bullet
        assert "translating only" not in bullet

    def test_with_description(self):
        bullet = image_placeholder_instruction(
            "Para.\n\n[IMAGE:images/i01.jpg:a winter scene]\n\nMore."
        )
        assert bullet != ""
        assert bullet.startswith("   - ")
        assert "[IMAGE:filename.ext:image description]" in bullet
        assert "translating only the image description" in bullet

    def test_mixed_resolves_to_with_description(self):
        """Mixed-format chunks should get the with-description (superset) wording."""
        bullet = image_placeholder_instruction(
            "[IMAGE:images/a.jpg]\n\n[IMAGE:images/b.jpg:caption]"
        )
        assert "image description" in bullet
        assert "translating only" in bullet

    def test_empty_description_treated_as_filename_only(self):
        """[IMAGE:foo.jpg:] (empty description) should not trigger the description wording."""
        bullet = image_placeholder_instruction("[IMAGE:images/a.jpg:]")
        assert bullet != ""
        assert "translating only" not in bullet
