"""Tests for the Project Gutenberg ingest converter."""

from bs4 import BeautifulSoup

from scripts.ingest_gutenberg import Converter, decode_html_bytes, fetch_html


def _convert(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("body") or soup
    conv = Converter(base_url="", images_dir=None, download_images=False)
    return conv.convert(root)


class TestItalicConversion:
    def test_i_tag_wraps_in_underscores(self):
        text = _convert("<p>HMS <i>Victory</i> sailed.</p>")
        assert "HMS _Victory_ sailed." in text

    def test_em_tag_wraps_in_underscores(self):
        text = _convert("<p>He was <em>absolutely</em> furious.</p>")
        assert "He was _absolutely_ furious." in text

    def test_paragraph_breaks_preserved_around_italic(self):
        text = _convert(
            "<p>The <i>Victory</i> arrived.</p><p>Next paragraph.</p>"
        )
        # Two paragraphs joined by a blank line.
        assert "The _Victory_ arrived." in text
        assert "\n\nNext paragraph." in text

    def test_italic_as_entire_paragraph(self):
        text = _convert("<p><i>Italicized</i></p>")
        assert "_Italicized_" in text

    def test_empty_italic_dropped(self):
        text = _convert("<p>before <i></i>after</p>")
        # No stray underscores, and no surprise concatenation.
        assert "_" not in text
        assert "before" in text and "after" in text

    def test_italic_with_inner_whitespace_stripped(self):
        text = _convert("<p>foo <i>  bar  </i> baz</p>")
        assert "_bar_" in text
        # No double underscores from whitespace.
        assert "__" not in text

    def test_multiple_italics_in_paragraph(self):
        text = _convert(
            "<p>The <i>Victory</i> met the <i>Redoutable</i>.</p>"
        )
        assert "_Victory_" in text
        assert "_Redoutable_" in text

    def test_non_italic_inline_tag_still_recurses(self):
        # <span> is not in ITALIC_TAGS, so its text should appear unwrapped.
        text = _convert("<p>plain <span>span</span> text</p>")
        assert "plain span text" in text
        assert "_" not in text

    def test_italic_spans_multiple_words(self):
        text = _convert("<p>The <i>HMS Victory</i> sailed.</p>")
        assert "The _HMS Victory_ sailed." in text

    def test_bold_tags_are_not_italicized(self):
        # Scope is italics only; <b>/<strong> must not produce underscores.
        text = _convert(
            "<p><b>Bold</b> and <strong>strong</strong> stay plain.</p>"
        )
        assert "Bold and strong stay plain." in text
        assert "_" not in text

    def test_adjacent_italics_keep_word_boundary(self):
        text = _convert("<p><i>a</i> <i>b</i></p>")
        # Spacing between the two italic runs must survive — otherwise the
        # downstream EM regex would see "_a__b_" and produce one run.
        assert "_a_ _b_" in text

    def test_italic_inside_blockquote(self):
        text = _convert("<blockquote>He cried <em>halt!</em></blockquote>")
        assert "He cried _halt!_" in text

    def test_italic_with_internal_punctuation(self):
        text = _convert("<p>Read <i>Don Quixote, Part I</i> first.</p>")
        assert "_Don Quixote, Part I_" in text


def _page(body: str) -> str:
    return (
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=windows-1252"></head>'
        f"<body>{body}</body></html>"
    )


class TestEncodingDetection:
    def test_windows_1252_declared_in_meta_is_honored(self):
        # The em-dash separating numeral from title is byte 0x97 in windows-1252.
        # Decoded as UTF-8 it becomes U+FFFD and the chapter heading is corrupted.
        html = _page("<h2>I. — THE BUILDING OF ROME</h2>")
        text = decode_html_bytes(html.encode("windows-1252"))
        assert "I. — THE BUILDING OF ROME" in text
        assert "�" not in text

    def test_windows_1252_accents_survive(self):
        html = _page("<p>Café naïve résumé</p>")
        text = decode_html_bytes(html.encode("windows-1252"))
        assert "Café naïve résumé" in text

    def test_utf8_still_decodes(self):
        html = '<html><head><meta charset="utf-8"></head><body><p>Café —</p></body></html>'
        text = decode_html_bytes(html.encode("utf-8"))
        assert "Café —" in text
        assert "�" not in text

    def test_utf8_bom_is_honored(self):
        html = "<html><body><p>Café</p></body></html>"
        text = decode_html_bytes(html.encode("utf-8-sig"))
        assert "Café" in text

    def test_ascii_is_unaffected(self):
        text = decode_html_bytes(b"<html><body><p>Plain ASCII.</p></body></html>")
        assert "Plain ASCII." in text

    def test_local_file_read_through_fetch_html(self, tmp_path):
        # The regression the friction log hit: fetch_html's local branch used to
        # hard-code UTF-8 while the URL branch honored the real encoding.
        src = tmp_path / "book.htm"
        src.write_bytes(_page("<h2>I. — THE BUILDING OF ROME</h2>").encode("windows-1252"))
        html, _base_url = fetch_html(str(src))
        assert "I. — THE BUILDING OF ROME" in html
        assert "�" not in html
