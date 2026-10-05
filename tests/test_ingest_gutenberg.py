"""Tests for the Project Gutenberg ingest converter."""

import json

from bs4 import BeautifulSoup

from scripts.ingest_gutenberg import (
    Converter,
    decode_html_bytes,
    fetch_html,
    linked_images,
    write_heading_outline,
)


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
        # hard-code UTF-8 while the URL branch used a different decoder.
        src = tmp_path / "book.htm"
        src.write_bytes(_page("<h2>I. — THE BUILDING OF ROME</h2>").encode("windows-1252"))
        html, _base_url = fetch_html(str(src))
        assert "I. — THE BUILDING OF ROME" in html
        assert "�" not in html

    def test_url_fetch_uses_same_decoder(self, monkeypatch):
        # Local and URL must share decode_html_bytes so the same windows-1252
        # document does not diverge by source path.
        raw = _page("<h2>I. — THE BUILDING OF ROME</h2>").encode("windows-1252")

        class FakeResp:
            content = raw

            def raise_for_status(self):
                return None

        monkeypatch.setattr(
            "scripts.ingest_gutenberg.requests.get",
            lambda *a, **k: FakeResp(),
        )
        html, base_url = fetch_html("https://example.com/books/rome.htm")
        assert "I. — THE BUILDING OF ROME" in html
        assert "�" not in html
        assert base_url == "https://example.com/books/"


class TestHeadingCapture:
    """The outline the splitter anchors on is built here."""

    def _convert_with_chapters(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        root = soup.find("body") or soup
        conv = Converter(base_url="", images_dir=None, download_images=False)
        text = conv.convert(root)
        return conv, text

    def test_multiline_heading_collapses_to_one_line(self):
        # A hand-typeset "staircase" title: one <h2> whose raw source wraps
        # across several physical lines. get_text(separator=...) does not
        # collapse whitespace *within* a text node, so without normalization the
        # embedded newlines survive, later read as paragraph breaks, and shatter
        # the heading into fragments that leak into neighbouring chapters.
        conv, text = self._convert_with_chapters(
            "<body><h2>The GRASSHOPPER\nand\nthe MEASURING\nWORM\n"
            "RUN a RACE</h2><p>Once upon a time.</p></body>"
        )
        assert [c["heading"] for c in conv.chapters] == [
            "The GRASSHOPPER and the MEASURING WORM RUN a RACE"]
        assert "The GRASSHOPPER and the MEASURING WORM RUN a RACE\n" in text
        assert "WORM\nRUN" not in text

    def test_heading_records_its_level(self):
        conv, _ = self._convert_with_chapters(
            "<body><h1>Book</h1><p>x</p><h2>One</h2><p>y</p>"
            "<h3>Sub</h3><p>z</p></body>"
        )
        assert [(c["level"], c["heading"]) for c in conv.chapters] == [
            (1, "Book"), (2, "One"), (3, "Sub")]

    def test_heading_with_nested_markup_still_collapses(self):
        # Headings are extracted with get_text(), not walked, so inline markup
        # is flattened rather than marked up — which is what the splitter wants
        # to anchor on. What matters is that the whitespace collapses.
        conv, _ = self._convert_with_chapters(
            "<body><h2>A <i>Tale</i>\n  of\n Two</h2><p>x</p></body>")
        assert conv.chapters[0]["heading"] == "A Tale of Two"


class TestWriteHeadingOutline:
    def test_writes_level_and_text_in_document_order(self, tmp_path):
        n = write_heading_outline(tmp_path, [
            {"heading": "Book", "level": 1, "word_offset": 0},
            {"heading": "One", "level": 2, "word_offset": 10},
        ])
        assert n == 2
        data = json.loads((tmp_path / "headings.json").read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["headings"] == [
            {"level": 1, "text": "Book"}, {"level": 2, "text": "One"}]

    def test_skips_empty_headings(self, tmp_path):
        n = write_heading_outline(tmp_path, [
            {"heading": "", "level": 2, "word_offset": 0},
            {"heading": "Real", "level": 2, "word_offset": 1},
        ])
        assert n == 1

    def test_round_trips_through_the_splitter_loader(self, tmp_path):
        write_heading_outline(tmp_path, [
            {"heading": "Chapter One", "level": 2, "word_offset": 0}])
        from src.book_splitter import load_heading_outline
        assert load_heading_outline(tmp_path) == [
            {"level": 2, "text": "Chapter One"}]


class TestCaptionExtraction:
    """<figcaption> / class="caption" -> a [CAPTION] block."""

    def test_figcaption_becomes_a_caption_block(self):
        out = _convert(
            '<body><p>Before.</p><figure><img src="i1.jpg" alt="A lamb"/>'
            '<figcaption>The lamb with the longest tail.</figcaption></figure>'
            '<p>After.</p></body>'
        )
        assert "[IMAGE:images/i1.jpg:A lamb]" in out
        assert "[CAPTION] The lamb with the longest tail." in out

    def test_p_class_caption_becomes_a_caption_block(self):
        # The classic Gutenberg shape.
        out = _convert(
            '<body><div class="illustration"><img src="i2.jpg" alt="THE LAMB"/>'
            '<p class="caption">THE LAMB WITH THE LONGEST TAIL.</p></div></body>'
        )
        assert "[CAPTION] THE LAMB WITH THE LONGEST TAIL." in out

    def test_caption_is_its_own_block(self):
        out = _convert(
            '<body><figure><img src="i1.jpg"/><figcaption>Cap.</figcaption></figure></body>'
        )
        blocks = [b.strip() for b in out.split("\n\n") if b.strip()]
        assert "[IMAGE:images/i1.jpg]" in blocks
        assert "[CAPTION] Cap." in blocks

    def test_container_carrying_a_caption_class_does_not_swallow_its_image(self):
        out = _convert(
            '<body><div class="caption"><img src="i3.jpg" alt="x"/>'
            '<p class="caption">Real caption</p></div></body>'
        )
        assert "[IMAGE:images/i3.jpg:x]" in out
        assert "[CAPTION] Real caption" in out
        assert "[CAPTION] [IMAGE" not in out

    def test_italics_survive_inside_a_caption(self):
        out = _convert(
            '<body><figure><img src="i4.jpg"/>'
            '<figcaption>La gente <i>gatuna</i> comia bien</figcaption></figure></body>'
        )
        assert "[CAPTION] La gente _gatuna_ comia bien" in out

    def test_br_inside_a_caption_does_not_split_the_block(self):
        out = _convert(
            '<body><figure><img src="i5.jpg"/>'
            '<figcaption>Primera linea<br/>segunda linea</figcaption></figure></body>'
        )
        assert "[CAPTION] Primera linea segunda linea" in out

    def test_empty_caption_emits_nothing(self):
        out = _convert(
            '<body><figure><img src="i6.jpg"/><figcaption>  </figcaption></figure></body>'
        )
        assert "[CAPTION]" not in out

    def test_plain_paragraph_is_unaffected(self):
        out = _convert('<body><p>Just a normal paragraph.</p></body>')
        assert "[CAPTION]" not in out


class TestImageBlockSpacing:
    """An image placeholder must be its own blank-line-separated block."""

    def test_image_is_separated_by_blank_lines(self):
        out = _convert('<body><p>Before.</p><img src="i1.jpg"/><p>After.</p></body>')
        blocks = [b.strip() for b in out.split("\n\n") if b.strip()]
        assert "[IMAGE:images/i1.jpg]" in blocks

    def test_inline_image_splits_its_paragraph_into_blocks(self):
        # Previously the single-newline form glued the token to the prose, so
        # the EPUB builder's fullmatch failed and the raw token was rendered.
        out = _convert('<body><p>text <img src="i5.jpg"/> more text</p></body>')
        blocks = [b.strip() for b in out.split("\n\n") if b.strip()]
        assert "[IMAGE:images/i5.jpg]" in blocks


THUMB_LINK = '<a href="images/042_l.gif"><img alt="A MAP" src="images/042.jpg"></a>'


def _picture(path, size, fmt):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 40, 40)).save(path, format=fmt)
    return path


def _ingest(tmp_path, html: str, **kwargs):
    """Convert ``html`` as a page saved in ``tmp_path/site``, images beside it."""
    out = tmp_path / "book" / "images"
    out.mkdir(parents=True)
    conv = Converter(
        base_url=(tmp_path / "site").as_uri() + "/", images_dir=out, download_images=True, **kwargs
    )
    text = conv.convert(BeautifulSoup(f"<body>{html}</body>", "html.parser").find("body"))
    return text, sorted(p.name for p in out.iterdir()), conv


class TestLinkedLargerScans:
    """A thumbnail that links to a larger scan of itself: import the scan."""

    def test_the_linked_scan_replaces_the_thumbnail(self):
        out = _convert(f"<body><p>{THUMB_LINK} Drawings of land.</p></body>")
        assert "[IMAGE:images/042_l.gif:A MAP]" in out
        assert "042.jpg" not in out

    def test_split_halves_under_one_link_become_one_image(self):
        out = _convert(
            '<body><a href="images/029_l.gif">'
            '<img alt="RIVER" src="images/029.1.jpg"> <img alt="RIVER" src="images/029.2.jpg">'
            "</a></body>"
        )
        assert out.count("[IMAGE:") == 1
        assert "[IMAGE:images/029_l.gif:RIVER]" in out

    def test_other_links_keep_the_image_the_page_displays(self):
        for html in (
            '<a href="notes.html"><img src="images/1.jpg"></a>',          # not an image
            '<a href="images/1_l.gif"><img src="images/1.jpg"> Enlarge</a>',  # carries text
            '<a href="images/1.jpg"><img src="images/1.jpg"></a>',        # links to itself
        ):
            assert "[IMAGE:images/1.jpg]" in _convert(f"<body>{html}</body>"), html

    def test_inline_images_can_be_kept(self):
        soup = BeautifulSoup(f"<body>{THUMB_LINK}</body>", "html.parser")
        conv = Converter("", None, False, prefer_linked_images=False)
        assert "[IMAGE:images/042.jpg:A MAP]" in conv.convert(soup.find("body"))

    def test_only_the_larger_scan_is_downloaded(self, tmp_path):
        _picture(tmp_path / "site" / "images" / "042_l.gif", (600, 400), "GIF")
        _picture(tmp_path / "site" / "images" / "042.jpg", (200, 130), "JPEG")
        text, files, conv = _ingest(tmp_path, THUMB_LINK)
        assert "[IMAGE:images/042_l.gif:A MAP]" in text
        assert files == ["042_l.gif"]
        assert (conv._images_downloaded, conv._images_linked) == (1, 1)

    def test_a_link_to_something_no_larger_is_not_taken(self, tmp_path):
        _picture(tmp_path / "site" / "images" / "042_l.gif", (200, 130), "GIF")
        _picture(tmp_path / "site" / "images" / "042.jpg", (200, 130), "JPEG")
        text, files, conv = _ingest(tmp_path, THUMB_LINK)
        assert "[IMAGE:images/042.jpg:A MAP]" in text
        assert files == ["042.jpg"] and conv._images_linked == 0

    def test_a_missing_or_broken_scan_falls_back_to_the_thumbnail(self, tmp_path):
        _picture(tmp_path / "site" / "images" / "042.jpg", (200, 130), "JPEG")
        text, files, _ = _ingest(tmp_path, THUMB_LINK)
        assert "[IMAGE:images/042.jpg:A MAP]" in text and files == ["042.jpg"]

        other = tmp_path / "second"
        _picture(other / "site" / "images" / "042.jpg", (200, 130), "JPEG")
        (other / "site" / "images" / "042_l.gif").write_bytes(b"<html>not found</html>")
        text, files, _ = _ingest(other, THUMB_LINK)
        assert "[IMAGE:images/042.jpg:A MAP]" in text and files == ["042.jpg"]

    def test_a_scan_of_a_different_picture_is_not_taken(self, tmp_path):
        from PIL import Image, ImageDraw

        def plate(path, size, box):
            image = Image.new("L", size, 255)
            ImageDraw.Draw(image).rectangle([v * s for v, s in zip(box, size * 2)], fill=0)
            path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(path)

        images = tmp_path / "site" / "images"
        plate(images / "042.jpg", (200, 130), (0.1, 0.1, 0.4, 0.4))
        plate(images / "042_l.gif", (600, 400), (0.6, 0.6, 0.9, 0.9))   # another plate
        plate(images / "043.jpg", (200, 130), (0.5, 0.1, 0.9, 0.5))
        plate(images / "043_l.gif", (600, 400), (0.5, 0.1, 0.9, 0.5))   # the same one
        text, files, conv = _ingest(
            tmp_path,
            THUMB_LINK + '<a href="images/043_l.gif"><img src="images/043.jpg"></a>',
        )
        assert "[IMAGE:images/042.jpg:A MAP]" in text
        assert "[IMAGE:images/043_l.gif]" in text
        assert files == ["042.jpg", "043_l.gif"]
        assert conv._images_unlike == ["042.jpg"]

    def test_linked_images_maps_each_displayed_file_to_its_scan(self):
        soup = BeautifulSoup(
            f'<body>{THUMB_LINK}<a href="#top"><img src="images/x.jpg"></a>'
            '<a href="images/029_l.gif"><img src="images/029.1.jpg"><img src="images/029.2.jpg"></a>'
            "</body>",
            "html.parser",
        )
        links = linked_images(soup, "https://pg.example/book/")
        assert [(link["name"], link["inline_names"]) for link in links] == [
            ("042_l.gif", ["042.jpg"]),
            ("029_l.gif", ["029.1.jpg", "029.2.jpg"]),
        ]
        assert links[0]["url"] == "https://pg.example/book/images/042_l.gif"
