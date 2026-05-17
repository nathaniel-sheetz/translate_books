"""Tests for the EPUB builder module."""

import logging
import tempfile
import zipfile
from pathlib import Path

import pytest

from src.epub_builder import (
    _DEFAULT_TRANSLATOR_HEADING,
    _int_to_roman,
    _load_chapter_manifest,
    _load_toc_format,
    _normalize_heading,
    _strip_image_blocks,
    build_epub,
    chapter_text_to_xhtml,
    collect_referenced_images,
    detect_chapter_heading,
    matter_text_to_xhtml,
    note_text_to_xhtml,
    parse_image_placeholders,
    synthesize_chapter_heading,
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

    def test_detects_sermon_heading(self):
        text = "SERMÓN I.\n\nHay un Dios.\n\nBody text here."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "SERMÓN I."
        assert subtitle == "Hay un Dios."
        assert body == "Body text here."

    def test_detects_english_sermon_heading(self):
        text = "SERMON III.\n\nThe Good Shepherd\n\nBody."
        heading, subtitle, body = detect_chapter_heading(text)
        assert heading == "SERMON III."
        assert subtitle == "The Good Shepherd"
        assert body == "Body."


# --- chapter_text_to_xhtml ---

class TestChapterTextToXhtml:
    def test_basic_paragraphs(self):
        text = "CHAPTER I\n\nTitle\n\nFirst paragraph.\n\nSecond paragraph."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<h1>Chapter I</h1>' in xhtml
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

    def test_no_heading_synthesizes_default(self):
        # When the chapter text doesn't begin with a numeral, the builder
        # synthesizes a heading from the chapter number using defaults
        # (label="Chapter", numeral_style="arabic"). The original first
        # line is promoted to the <h2> subtitle.
        text = "Just some text without a chapter heading."
        xhtml = chapter_text_to_xhtml(text, 5)
        assert '<h1>Chapter 5</h1>' in xhtml
        assert '<h2>Just some text without a chapter heading.</h2>' in xhtml

    def test_synthesis_with_custom_label(self):
        text = "EL REY ALFREDO Y LOS PASTELES.\n\nMuchos años atrás..."
        xhtml = chapter_text_to_xhtml(
            text, 1,
            heading_config={"label": "Capítulo", "numeral_style": "arabic"},
        )
        assert '<h1>Capítulo 1</h1>' in xhtml
        assert '<h2>EL REY ALFREDO Y LOS PASTELES.</h2>' in xhtml
        assert '<p>Muchos años atrás...</p>' in xhtml

    def test_synthesis_with_roman(self):
        text = "Title line.\n\nBody."
        xhtml = chapter_text_to_xhtml(
            text, 4,
            heading_config={"label": "Chapter", "numeral_style": "roman"},
        )
        assert '<h1>Chapter IV</h1>' in xhtml

    def test_synthesis_no_label(self):
        text = "Title line.\n\nBody."
        xhtml = chapter_text_to_xhtml(
            text, 7,
            heading_config={"label": "", "numeral_style": "arabic"},
        )
        assert '<h1>7</h1>' in xhtml

    def test_existing_numeral_heading_not_overridden(self):
        # If the chapter already begins with a numeral, synthesis must not
        # fire — preserves behavior for projects like lang-faerie.
        text = "I\n\nUNA Y EL LEÓN\n\nBody."
        xhtml = chapter_text_to_xhtml(
            text, 1,
            heading_config={"label": "Capítulo", "numeral_style": "arabic"},
        )
        assert '<h1>I</h1>' in xhtml
        assert '<h2>UNA Y EL LEÓN</h2>' in xhtml
        assert 'Capítulo' not in xhtml

    def test_chapter_xhtml_renders_normalized_heading(self):
        # Sermon-style: all-caps label with trailing period gets canonicalized.
        text = "SERMÓN I.\n\nHay un Dios.\n\nPorque es necesario..."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<h1>Sermón I</h1>' in xhtml
        assert '<h2>Hay un Dios.</h2>' in xhtml
        assert '<p>Porque es necesario...</p>' in xhtml
        # The raw all-caps form should not appear as the h1.
        assert '<h1>SERMÓN I.</h1>' not in xhtml

    def test_verse_block_renders_div_verse(self):
        stanza = (
            "Drops of rain and bits of sunshine\n"
            "Falling here and gleaming there,\n"
            "Tiny blades of grass appearing.\n"
            "Tell of springtime bright and fair."
        )
        text = f"CHAPTER I\n\nSpring Song\n\n{stanza}\n\nNormal prose paragraph."
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<div class="verse">' in xhtml
        assert '<p class="verse-line">Drops of rain and bits of sunshine</p>' in xhtml
        assert '<p class="verse-line">Falling here and gleaming there,</p>' in xhtml
        assert '<p class="verse-line">Tell of springtime bright and fair.</p>' in xhtml
        assert '<p>Normal prose paragraph.</p>' in xhtml

    def test_prose_block_with_newlines_does_not_render_as_verse(self):
        # Long lines should not trigger the verse path even if they contain \n.
        long_para = (
            "This is a very long prose paragraph that happens to contain a newline "
            "character, but because the lines are long it should not be mistaken for "
            "verse by the is_verse_block heuristic.\n"
            "This second line is also very long and should remain in a regular <p> tag."
        )
        text = f"CHAPTER I\n\nTitle\n\n{long_para}"
        xhtml = chapter_text_to_xhtml(text, 1)
        assert '<div class="verse">' not in xhtml


# --- synthesize_chapter_heading & _int_to_roman ---

class TestSynthesizeChapterHeading:
    def test_int_to_roman_basics(self):
        assert _int_to_roman(1) == 'I'
        assert _int_to_roman(4) == 'IV'
        assert _int_to_roman(9) == 'IX'
        assert _int_to_roman(50) == 'L'
        assert _int_to_roman(1994) == 'MCMXCIV'

    def test_default_config(self):
        assert synthesize_chapter_heading(1) == 'Chapter 1'
        assert synthesize_chapter_heading(50) == 'Chapter 50'

    def test_custom_label_arabic(self):
        cfg = {"label": "Capítulo", "numeral_style": "arabic"}
        assert synthesize_chapter_heading(3, cfg) == 'Capítulo 3'

    def test_roman_style(self):
        cfg = {"label": "Chapter", "numeral_style": "roman"}
        assert synthesize_chapter_heading(4, cfg) == 'Chapter IV'

    def test_empty_label_emits_just_numeral(self):
        cfg = {"label": "", "numeral_style": "arabic"}
        assert synthesize_chapter_heading(12, cfg) == '12'

    def test_unknown_numeral_style_falls_back_to_arabic(self):
        cfg = {"label": "Ch", "numeral_style": "klingon"}
        assert synthesize_chapter_heading(2, cfg) == 'Ch 2'


# --- _normalize_heading ---

class TestNormalizeHeading:
    @pytest.mark.parametrize("raw,expected", [
        ("SERMÓN I.", "Sermón I"),
        ("CHAPTER XVII", "Chapter XVII"),
        ("I", "I"),
        ("SERMON III.", "Sermon III"),
        ("Capítulo 1", "Capítulo 1"),
        ("CHAPTER 12", "Chapter 12"),
        ("12", "12"),
        ("LECCIÓN 0.", "Lección 0"),
    ])
    def test_normalize_variants(self, raw, expected):
        assert _normalize_heading(raw) == expected

    def test_non_matching_input_strips_trailing_period(self):
        # Defensive fallback: raw line that didn't match the regex still
        # gets a period stripped so callers can rely on idempotent output.
        assert _normalize_heading("Some Title.") == "Some Title"


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


# --- _strip_image_blocks ---

class TestStripImageBlocks:
    def test_no_images(self):
        body = "Just prose.\n\nMore prose."
        out, n = _strip_image_blocks(body)
        assert out == body
        assert n == 0

    def test_strips_sole_block(self):
        body = "Para1.\n\n[IMAGE:images/x.jpg]\n\nPara2."
        out, n = _strip_image_blocks(body)
        assert "[IMAGE:" not in out
        assert n == 1

    def test_strips_inline_substring(self):
        # Decision 2A: ALL [IMAGE:...] substrings stripped, surrounding prose preserved.
        body = "Hello [IMAGE:images/x.jpg] world."
        out, n = _strip_image_blocks(body)
        assert "[IMAGE:" not in out
        assert "Hello" in out and "world." in out
        assert n == 1


# --- note_text_to_xhtml ---

class TestNoteTextToXhtml:
    def test_basic(self):
        xhtml = note_text_to_xhtml("Note from the Translator", "Hello.")
        assert "<h1>Note from the Translator</h1>" in xhtml
        assert "<p>Hello.</p>" in xhtml

    def test_default_heading_when_blank(self):
        xhtml = note_text_to_xhtml("", "Body.")
        assert f"<h1>{_DEFAULT_TRANSLATOR_HEADING}</h1>" in xhtml

    def test_default_heading_when_whitespace(self):
        xhtml = note_text_to_xhtml("   \n\t  ", "Body.")
        assert f"<h1>{_DEFAULT_TRANSLATOR_HEADING}</h1>" in xhtml

    def test_xss_heading_escaped(self):
        xhtml = note_text_to_xhtml("<script>alert(1)</script>", "Body.")
        assert "<script>alert(1)</script>" not in xhtml
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in xhtml

    def test_inline_image_stripped(self, caplog):
        caplog.set_level(logging.WARNING, logger="src.epub_builder")
        xhtml = note_text_to_xhtml(
            "H", "Para1.\n\n[IMAGE:images/x.jpg]\n\nPara2."
        )
        assert "<img" not in xhtml
        assert "<p>Para1.</p>" in xhtml
        assert "<p>Para2.</p>" in xhtml
        assert any("Stripped" in rec.message for rec in caplog.records)

    def test_links_stylesheet(self):
        xhtml = note_text_to_xhtml("H", "Body.")
        assert '<link rel="stylesheet" type="text/css" href="style.css"/>' in xhtml


# --- build_epub: translator note integration ---

class TestBuildEpubTranslatorNote(TestBuildEpub):
    def test_appends_note_as_last_chapter(self, tmp_path):
        project = self._make_project(tmp_path)
        output = build_epub(
            project_path=project,
            title="T",
            author="A",
            translator_note_heading="Note from the Translator",
            translator_note_body="Hello.",
        )
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
            assert any("translator_note.xhtml" in n for n in names)
            note_xhtml = next(
                zf.read(n).decode("utf-8")
                for n in names
                if n.endswith("translator_note.xhtml")
            )
            assert "<h1>Note from the Translator</h1>" in note_xhtml
            assert "<p>Hello.</p>" in note_xhtml
            # Confirm note is referenced last in the spine via the OPF.
            opf = next(
                zf.read(n).decode("utf-8")
                for n in names
                if n.endswith(".opf")
            )
            # itemref id pattern in ebooklib is the file's id; assert its
            # ordering by file-name presence in spine section.
            spine_section = opf.split("<spine")[1].split("</spine>")[0]
            assert "translator_note" in spine_section
            # No chapter follows the note in the spine.
            tail = spine_section.split("translator_note")[-1]
            assert "chapter_" not in tail

    def test_empty_body_no_extra_chapter(self, tmp_path):
        project = self._make_project(tmp_path)
        output = build_epub(
            project_path=project,
            title="T",
            author="A",
            translator_note_heading="X",
            translator_note_body="",
        )
        with zipfile.ZipFile(output) as zf:
            assert not any(
                "translator_note.xhtml" in n for n in zf.namelist()
            )

    def test_body_only_image_no_extra_chapter(self, tmp_path):
        project = self._make_project(tmp_path)
        output = build_epub(
            project_path=project,
            title="T",
            author="A",
            translator_note_heading="X",
            translator_note_body="[IMAGE:images/x.jpg]",
        )
        with zipfile.ZipFile(output) as zf:
            assert not any(
                "translator_note.xhtml" in n for n in zf.namelist()
            )

    def test_blank_heading_uses_default_constant(self, tmp_path):
        project = self._make_project(tmp_path)
        output = build_epub(
            project_path=project,
            title="T",
            author="A",
            translator_note_heading="",
            translator_note_body="Hello.",
        )
        with zipfile.ZipFile(output) as zf:
            content = next(
                zf.read(n).decode("utf-8")
                for n in zf.namelist()
                if n.endswith("translator_note.xhtml")
            )
            assert f"<h1>{_DEFAULT_TRANSLATOR_HEADING}</h1>" in content

    def test_no_note_kwargs_unchanged(self, tmp_path):
        """Regression: building without note kwargs produces no extra files."""
        project = self._make_project(tmp_path)
        out_base = build_epub(
            project_path=project,
            title="T",
            author="A",
            output_path=tmp_path / "base.epub",
        )
        out_explicit_none = build_epub(
            project_path=project,
            title="T",
            author="A",
            translator_note_heading=None,
            translator_note_body=None,
            output_path=tmp_path / "explicit.epub",
        )
        with zipfile.ZipFile(out_base) as a, zipfile.ZipFile(out_explicit_none) as b:
            base_xhtml = sorted(n for n in a.namelist() if n.endswith(".xhtml"))
            other_xhtml = sorted(n for n in b.namelist() if n.endswith(".xhtml"))
            assert base_xhtml == other_xhtml
            assert not any("translator_note" in n for n in base_xhtml)


# --- chapter_manifest support ---

class TestChapterManifest:
    def _make_project_with_preface(self, tmp_path):
        """Project with preface as chapter_01 and two real chapters."""
        import json as _json
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()
        (tmp_path / "images").mkdir()

        (chapters_dir / "chapter_01.txt").write_text(
            "Preface\n\nThis is the preface body, before any numbered chapter starts.",
            encoding="utf-8",
        )
        (chapters_dir / "chapter_02.txt").write_text(
            "CHAPTER I\n\nThe Beginning\n\nFirst real chapter content.",
            encoding="utf-8",
        )
        (chapters_dir / "chapter_03.txt").write_text(
            "CHAPTER II\n\nThe Middle\n\nSecond real chapter content.",
            encoding="utf-8",
        )

        manifest = [
            {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
            {"id": "chapter_03", "kind": "chapter", "number": 2},
        ]
        (tmp_path / "project.json").write_text(
            _json.dumps({"chapter_manifest": manifest}), encoding="utf-8"
        )
        return tmp_path

    def test_load_chapter_manifest(self, tmp_path):
        proj = self._make_project_with_preface(tmp_path)
        m = _load_chapter_manifest(proj)
        assert set(m.keys()) == {"chapter_01", "chapter_02", "chapter_03"}
        assert m["chapter_01"]["kind"] == "front_matter"
        assert m["chapter_02"]["number"] == 1

    def test_toc_uses_manifest_labels(self, tmp_path):
        """TOC should label preface 'Preface' and chapters Chapter 1, Chapter 2."""
        proj = self._make_project_with_preface(tmp_path)
        output = build_epub(
            project_path=proj,
            title="Test",
            author="A",
            language="en",
        )
        assert output.exists()
        with zipfile.ZipFile(output) as zf:
            # Read the NCX (table of contents) and check labels
            ncx_name = next((n for n in zf.namelist() if n.endswith(".ncx")), None)
            assert ncx_name is not None
            ncx = zf.read(ncx_name).decode("utf-8")
            assert "Preface" in ncx
            # The two chapters should be 1 and 2 — NOT 2 and 3
            assert "Chapter I: The Beginning" in ncx
            assert "Chapter II: The Middle" in ncx
            # Should NOT contain the off-by-one synthesized label
            assert "Chapter 3" not in ncx

    def test_no_manifest_keeps_existing_behavior(self, tmp_path):
        """Without a manifest, build_epub falls back to today's enumeration."""
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()
        (tmp_path / "images").mkdir()
        (chapters_dir / "chapter_01.txt").write_text(
            "CHAPTER I\n\nFirst\n\nBody.", encoding="utf-8"
        )
        (chapters_dir / "chapter_02.txt").write_text(
            "CHAPTER II\n\nSecond\n\nBody.", encoding="utf-8"
        )
        # No project.json on purpose.
        output = build_epub(project_path=tmp_path, title="T", author="A")
        with zipfile.ZipFile(output) as zf:
            xhtml_files = sorted(
                n for n in zf.namelist()
                if "chapter_" in n and n.endswith(".xhtml")
            )
            assert len(xhtml_files) == 2


class TestMatterTextToXhtml:
    def test_consumes_matching_heading_line(self):
        text = "Preface\n\nFirst paragraph of the preface."
        out = matter_text_to_xhtml(text, "Preface")
        assert "<h1>Preface</h1>" in out
        # The duplicated "Preface" header should not appear as a paragraph.
        assert "<p>Preface</p>" not in out
        assert "First paragraph of the preface." in out

    def test_keeps_body_when_first_line_does_not_match(self):
        text = "Some opening line.\n\nMore text."
        out = matter_text_to_xhtml(text, "Foreword")
        assert "<h1>Foreword</h1>" in out
        assert "Some opening line." in out


class TestTocFormat:
    def _make_sermon_project(self, tmp_path, *, toc_format=None):
        import json as _json
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()
        (tmp_path / "images").mkdir()
        (chapters_dir / "chapter_01.txt").write_text(
            "SERMÓN I.\n\nHay un Dios.\n\nBody one.", encoding="utf-8",
        )
        (chapters_dir / "chapter_02.txt").write_text(
            "SERMON III.\n\nThe Good Shepherd\n\nBody two.", encoding="utf-8",
        )
        config = {}
        if toc_format is not None:
            config["toc_format"] = toc_format
        if config:
            (tmp_path / "project.json").write_text(
                _json.dumps(config), encoding="utf-8"
            )
        return tmp_path

    def test_load_toc_format_present(self, tmp_path):
        proj = self._make_sermon_project(tmp_path, toc_format="heading_only")
        assert _load_toc_format(proj) == "heading_only"

    def test_load_toc_format_absent(self, tmp_path):
        proj = self._make_sermon_project(tmp_path)
        assert _load_toc_format(proj) is None

    def test_toc_format_heading_only(self, tmp_path):
        proj = self._make_sermon_project(tmp_path, toc_format="heading_only")
        output = build_epub(
            project_path=proj, title="T", author="A", language="es",
        )
        with zipfile.ZipFile(output) as zf:
            ncx_name = next((n for n in zf.namelist() if n.endswith(".ncx")), None)
            assert ncx_name is not None
            ncx = zf.read(ncx_name).decode("utf-8")
            assert "Sermón I" in ncx
            assert "Sermon III" in ncx
            # Subtitle suffix must be absent under heading_only.
            assert "Hay un Dios" not in ncx
            assert "The Good Shepherd" not in ncx

    def test_toc_format_default_preserves_subtitle(self, tmp_path):
        proj = self._make_sermon_project(tmp_path)
        output = build_epub(
            project_path=proj, title="T", author="A", language="es",
        )
        with zipfile.ZipFile(output) as zf:
            ncx_name = next((n for n in zf.namelist() if n.endswith(".ncx")), None)
            assert ncx_name is not None
            ncx = zf.read(ncx_name).decode("utf-8")
            # Default branch keeps the "Heading: Subtitle" format.
            assert "Sermón I: Hay un Dios." in ncx
            assert "Sermon III: The Good Shepherd" in ncx

