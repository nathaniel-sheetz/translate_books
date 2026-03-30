"""Tests for the EPUB builder module."""

import tempfile
import zipfile
from pathlib import Path

import pytest

from src.epub_builder import (
    build_epub,
    chapter_text_to_xhtml,
    collect_referenced_images,
    detect_chapter_heading,
    parse_image_placeholders,
)


# --- parse_image_placeholders ---

class TestParseImagePlaceholders:
    def test_no_placeholders(self):
        assert parse_image_placeholders("Just plain text.") == []

    def test_simple_placeholder(self):
        result = parse_image_placeholders("[IMAGE:images/i010.jpg]")
        assert len(result) == 1
        assert result[0] == ("[IMAGE:images/i010.jpg]", "images/i010.jpg", "")

    def test_placeholder_with_alt(self):
        result = parse_image_placeholders("[IMAGE:images/seal.jpg:A fluffy seal]")
        assert len(result) == 1
        assert result[0] == (
            "[IMAGE:images/seal.jpg:A fluffy seal]",
            "images/seal.jpg",
            "A fluffy seal",
        )

    def test_multiple_placeholders(self):
        text = "Before\n\n[IMAGE:images/a.png]\n\nMiddle\n\n[IMAGE:images/b.jpg:Alt]\n\nAfter"
        result = parse_image_placeholders(text)
        assert len(result) == 2
        assert result[0][1] == "images/a.png"
        assert result[1][1] == "images/b.jpg"
        assert result[1][2] == "Alt"

    def test_placeholder_in_paragraph(self):
        text = "Text with [IMAGE:images/inline.jpg] in the middle."
        result = parse_image_placeholders(text)
        assert len(result) == 1


# --- detect_chapter_heading ---

class TestDetectChapterHeading:
    def test_standard_heading(self):
        text = "CHAPTER I\n\nTHE SIX\n\nBody text here."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "CHAPTER I"
        assert subtitle == "THE SIX"
        assert body == "Body text here."

    def test_roman_numeral_only(self):
        text = "I\n\nUNA AND THE LION\n\nOnce upon a time..."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "I"
        assert subtitle == "UNA AND THE LION"
        assert body == "Once upon a time..."

    def test_arabic_numeral(self):
        text = "CHAPTER 12\n\nThe End\n\nFinal text."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "CHAPTER 12"
        assert subtitle == "The End"

    def test_no_heading(self):
        text = "Just a regular paragraph of text."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == ""
        assert subtitle == ""
        assert body == text

    def test_heading_without_subtitle(self):
        text = "CHAPTER V\n\nThe body starts right away with a long paragraph."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "CHAPTER V"
        # The long line is treated as subtitle since it's < 200 chars
        assert subtitle == "The body starts right away with a long paragraph."


# --- chapter_text_to_xhtml ---

class TestChapterTextToXhtml:
    def test_basic_paragraphs(self):
        text = "CHAPTER I\n\nTitle\n\nFirst paragraph.\n\nSecond paragraph."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<h1>CHAPTER I</h1>' in xhtml
        assert '<h2>Title</h2>' in xhtml
        assert '<p>First paragraph.</p>' in xhtml
        assert '<p>Second paragraph.</p>' in xhtml

    def test_image_placeholder(self):
        text = "CHAPTER I\n\nTitle\n\n[IMAGE:images/test.jpg]\n\nAfter image."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<img src="images/test.jpg"' in xhtml
        assert 'class="image"' in xhtml
        assert '<p>After image.</p>' in xhtml

    def test_image_with_alt(self):
        text = "CHAPTER I\n\nTitle\n\n[IMAGE:images/seal.jpg:A fluffy seal]"
        xhtml = chapter_text_to_xhtml(text, 1)
        assert 'alt="A fluffy seal"' in xhtml

    def test_horizontal_rule(self):
        text = "CHAPTER I\n\nTitle\n\nBefore.\n\n---\n\nAfter."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<hr/>' in xhtml

    def test_html_escaping(self):
        text = "CHAPTER I\n\nTitle\n\nHe said <hello> & goodbye."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '&lt;hello&gt;' in xhtml
        assert '&amp;' in xhtml

    def test_no_heading(self):
        text = "Just some text without a chapter heading."
        xhtml = chapter_text_to_xhtml(text, 5)
        assert '<title>Chapter 5</title>' in xhtml
        # No h1 since no heading detected
        assert '<h1>' not in xhtml


# --- collect_referenced_images ---

class TestCollectReferencedImages:
    def test_collects_from_chapters(self, tmp_path):
        ch1 = tmp_path / "chapter_01.txt"
        ch1.write_text("Text\n\n[IMAGE:images/a.jpg]\n\nMore text.", encoding='utf-8')
        ch2 = tmp_path / "chapter_02.txt"
        ch2.write_text("[IMAGE:images/b.png:Alt]\n\nStuff.", encoding='utf-8')

        refs = collect_referenced_images(tmp_path)
        assert refs == {"images/a.jpg", "images/b.png"}

    def test_empty_directory(self, tmp_path):
        assert collect_referenced_images(tmp_path) == set()


# --- build_epub integration ---

class TestBuildEpub:
    def _make_project(self, tmp_path):
        """Create a minimal project structure for testing."""
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()
        images_dir = tmp_path / "images"
        images_dir.mkdir()

        # Create chapter files
        (chapters_dir / "chapter_01.txt").write_text(
            "CHAPTER I\n\nThe Beginning\n\nOnce upon a time.\n\n"
            "[IMAGE:images/fig01.png]\n\nThe end of chapter one.",
            encoding='utf-8',
        )
        (chapters_dir / "chapter_02.txt").write_text(
            "CHAPTER II\n\nThe Middle\n\nChapter two content.\n\n---\n\nMore content.",
            encoding='utf-8',
        )

        # Create a small valid PNG (1x1 pixel)
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
            b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
            b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        (images_dir / "fig01.png").write_bytes(png_bytes)

        return tmp_path

    def test_builds_valid_epub(self, tmp_path):
        project = self._make_project(tmp_path)
        output = build_epub(
            project_path=project,
            title="Test Book",
            author="Test Author",
            language="en",
        )
        assert output.exists()
        assert output.suffix == '.epub'

        # EPUB is a ZIP file
        assert zipfile.is_zipfile(output)
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            # Should contain chapter XHTML files (under EPUB/ prefix)
            assert any('chapter_01' in n for n in names)
            assert any('chapter_02' in n for n in names)
            # Should contain the image
            assert any('fig01.png' in n for n in names)
            # Should contain stylesheet
            assert any('style.css' in n for n in names)

    def test_missing_image_warns(self, tmp_path):
        """Build succeeds even when a referenced image is missing."""
        project = self._make_project(tmp_path)
        # Remove the image file
        (project / "images" / "fig01.png").unlink()

        output = build_epub(
            project_path=project,
            title="Test Book",
            author="Test Author",
        )
        assert output.exists()

    def test_no_chapters_raises(self, tmp_path):
        (tmp_path / "chapters").mkdir()
        (tmp_path / "images").mkdir()
        with pytest.raises(FileNotFoundError, match="No chapter_"):
            build_epub(project_path=tmp_path, title="T", author="A")

    def test_cover_auto_detection(self, tmp_path):
        project = self._make_project(tmp_path)
        # Create a cover image
        cover_bytes = b'\xff\xd8\xff\xe0' + b'\x00' * 100  # Minimal JPEG header
        (project / "images" / "cover.jpg").write_bytes(cover_bytes)

        output = build_epub(
            project_path=project,
            title="Test Book",
            author="Test Author",
        )
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert any('cover.jpg' in n for n in names)

    def test_custom_output_path(self, tmp_path):
        project = self._make_project(tmp_path)
        custom_output = tmp_path / "custom" / "book.epub"

        output = build_epub(
            project_path=project,
            title="Test Book",
            author="Test Author",
            output_path=custom_output,
        )
        assert output == custom_output
        assert output.exists()

    def test_chapter_ordering(self, tmp_path):
        """Chapters should be ordered numerically, not lexicographically."""
        project = self._make_project(tmp_path)
        chapters_dir = project / "chapters"
        # Add chapter 10 (would sort before 2 lexicographically)
        (chapters_dir / "chapter_10.txt").write_text(
            "CHAPTER X\n\nTenth\n\nContent.", encoding='utf-8'
        )

        output = build_epub(
            project_path=project,
            title="Test Book",
            author="Test Author",
        )
        with zipfile.ZipFile(output) as zf:
            xhtml_files = sorted(
                n for n in zf.namelist()
                if 'chapter_' in n and n.endswith('.xhtml')
            )
            # 3 chapters: chapter_01, chapter_02, chapter_10 (in numeric order)
            assert len(xhtml_files) == 3
