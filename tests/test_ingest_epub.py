"""Tests for the EPUB ingest (scripts/ingest_epub.py)."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from scripts.ingest_epub import (
    DOC_SENTINEL,
    anchor_boundary_images,
    assign_image_names,
    ingest_epub,
    join_split_paragraphs,
    parse_italic_classes,
    plan_epub,
    recase_lead_ins,
)


# ---------------------------------------------------------------------------
# A tiny EPUB builder
# ---------------------------------------------------------------------------

def _png(w=40, h=40) -> bytes:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the dev env
        return b"\x89PNG fake"
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _xhtml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>x</title><link href="../Styles/style.css" rel="stylesheet"/></head>'
        f"<body>{body}</body></html>"
    )


def make_epub(path, docs, *, spine=None, ncx=(), guide=(), images=None,
              css="span.Italic { font-style: italic; }", metadata=None, nav=None):
    """Write an EPUB. ``docs`` maps Text/ file names to body HTML; ``spine`` is
    a list of names or (name, linear) pairs; ``ncx`` is (label, src, children)."""
    metadata = metadata or {"title": "Test Horses", "creator": "A. Writer",
                            "publisher": "Acme Press"}
    images = images or {}
    spine = spine or list(docs)
    items, refs = [], []
    for name in docs:
        items.append(f'<item id="{name}" href="Text/{name}" media-type="application/xhtml+xml"/>')
    for name in images:
        props = ' properties="cover-image"' if name == "cover.jpg" else ""
        mt = "image/jpeg" if name.endswith(".jpg") else "image/png"
        items.append(f'<item id="img-{name}" href="Images/{name}" media-type="{mt}"{props}/>')
    items.append('<item id="css" href="Styles/style.css" media-type="text/css"/>')
    items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    if nav:
        items.append('<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    for entry in spine:
        name, linear = (entry, True) if isinstance(entry, str) else entry
        refs.append(f'<itemref idref="{name}"' + ('' if linear else ' linear="no"') + "/>")
    guide_xml = "".join(f'<reference type="{t}" href="Text/{h}" title="{t}"/>' for t, h in guide)
    md = "".join(f"<dc:{k}>{v}</dc:{k}>" for k, v in metadata.items())
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="id">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{md}</metadata>'
        f'<manifest>{"".join(items)}</manifest>'
        f'<spine toc="ncx">{"".join(refs)}</spine>'
        f"<guide>{guide_xml}</guide></package>"
    )

    counter = iter(range(1, 1000))

    def points(entries):
        out = ""
        for label, src, children in entries:
            n = next(counter)
            out += (f'<navPoint id="p{n}" playOrder="{n}"><navLabel><text>{label}</text></navLabel>'
                    f'<content src="Text/{src}"/>{points(children)}</navPoint>')
        return out

    ncx_xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
               f"<navMap>{points(ncx)}</navMap></ncx>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx_xml)
        z.writestr("OEBPS/Styles/style.css", css)
        if nav:
            z.writestr("OEBPS/Text/nav.xhtml", _xhtml(nav))
        for name, body in docs.items():
            z.writestr(f"OEBPS/Text/{name}", _xhtml(body))
        for name, data in images.items():
            z.writestr(f"OEBPS/Images/{name}", data)
    return path


def _img(name):
    return f'<div><p class="image"><img src="../Images/{name}" alt=""/></p></div>'


@pytest.fixture
def publisher_epub(tmp_path):
    """Shaped like the Living Book Press InDesign export (horses-of-destiny)."""
    docs = {
        "cover.xhtml": '<div><img src="../Images/cover.jpg" alt=""/></div>',
        "title.xhtml": '<p><img src="../Images/title.png" alt=""/></p>',
        "copyright.xhtml": (
            '<p class="ccc">This edition published 2023 by Acme Press</p>'
            '<p class="ccc">ISBN: 978-1-922919-23-6 (ebook)</p>'
            '<p class="ccc">All rights reserved.</p>'
            '<p><img src="../Images/logo.png" alt=""/></p>'
        ),
        "toc.xhtml": (
            '<p class="toa">Contents</p>'
            '<p><a href="ch1.xhtml">BUCEPHALUS</a></p>'
            '<p><a href="ch2.xhtml">CAESAR’S HORSE</a></p>'
            '<p><a href="ch3.xhtml">JOAN OF ARC’S CHARGER</a></p>'
        ),
        "epigraph.xhtml": (
            '<p class="Dedication ParaOverride-1">What form of life has served humanity as the horse has?</p>'
            '<p><span class="Italic">Francis H. Rowley</span></p>'
        ),
        "ch1.xhtml": (
            '<p class="split">&#160;</p>'
            '<p id="t1" class="cta">B<a id="_idTextAnchor003"></a>UCEPHALUS</p>'
            '<p id="s1" class="ctb"><a id="x1"></a>CHARGER OF ALEXANDER</p>'
            '<p class="First-Paragraph"><span class="smx">T</span><span class="sm">HE</span> black '
            'yearling knew the <span class="Italic">eohippus</span> story, and he ran from the</p>'
        ),
        "plate1.xhtml": _img("plate1.jpg"),
        "ch1b.xhtml": (
            '<p class="Indent-Paragraph">field while the crowd watched the horse.</p>'
            '<p class="Indent-Paragraph">Alexander rode the black horse, and the king of the '
            'land met Joan of Arc and the Clever Hans of the story.</p>'
        ),
        "ch2.xhtml": (
            '<p id="t2" class="cta">CA<a id="a5"></a>ESAR’S HORSE</p>'
            '<p id="s2" class="ctb">THROW-BACK TO EOHIPPUS</p>'
            '<p class="First-Paragraph"><span class="smx">S</span><span class="sm">OOTHSAYERS</span> '
            'watched the young general.</p>'
            '<p><img src="../Images/blankx.png" alt=""/></p>'
            '<p class="Indent-Paragraph">He rode on, and the soothsayers were right.</p>'
        ),
        "plate2.xhtml": _img("plate2.jpg"),
        "ch3.xhtml": (
            '<p id="t3" class="cta">JOAN OF ARC’S CHARGER</p>'
            '<p id="s3" class="ctb">HE BORE A MAID</p>'
            '<p class="First-Paragraph"><span class="smx">J</span><span class="sm">OAN OF ARC</span> '
            'stood in a doorway.</p>'
            '<p class="Indent-Paragraph">She rode away at dawn.</p>'
        ),
    }
    spine = [("cover.xhtml", False)] + [n for n in docs if n != "cover.xhtml"]
    ncx = [
        ("TITLE PAGE", "title.xhtml", []),
        ("BUCEPHALUS", "ch1.xhtml#t1", [("CHARGER OF ALEXANDER", "ch1.xhtml#s1", [])]),
        ("CAESAR’S HORSE", "ch2.xhtml#t2", [("THROW-BACK TO EOHIPPUS", "ch2.xhtml#s2", [])]),
        ("JOAN OF ARC’S CHARGER", "ch3.xhtml#t3", [("HE BORE A MAID", "ch3.xhtml#s3", [])]),
    ]
    images = {"cover.jpg": _png(), "title.png": _png(), "logo.png": _png(),
              "plate1.jpg": _png(), "plate2.jpg": _png(), "blankx.png": _png()}
    return make_epub(tmp_path / "book.epub", docs, spine=spine, ncx=ncx,
                     guide=[("toc", "toc.xhtml")], images=images)


def _decisions(epub, **kw):
    _, docs = plan_epub(epub, **kw)
    return {d.name: (d.decision, d.reason) for d in docs}


# ---------------------------------------------------------------------------
# Document classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_publisher_pages_are_dropped_with_reasons(self, publisher_epub):
        dec = _decisions(publisher_epub)
        assert dec["cover.xhtml"] == ("drop", "spine linear=no")
        assert dec["title.xhtml"] == ("drop", "TOC label 'TITLE PAGE'")
        # No TOC label for the copyright page: the content heuristic catches it.
        assert dec["copyright.xhtml"][0] == "drop"
        assert dec["copyright.xhtml"][1].startswith("copyright page (")
        assert dec["toc.xhtml"] == ("drop", "guide/landmark: toc")

    def test_authored_pages_are_kept(self, publisher_epub):
        dec = _decisions(publisher_epub)
        for name in ("epigraph.xhtml", "ch1.xhtml", "plate1.xhtml", "ch1b.xhtml",
                     "ch2.xhtml", "plate2.xhtml", "ch3.xhtml"):
            assert dec[name][0] == "keep", name

    def test_overrides(self, publisher_epub):
        dec = _decisions(publisher_epub, keep_docs=["copyright.xhtml"], drop_docs=["epigraph"])
        assert dec["copyright.xhtml"][0] == "keep"
        assert dec["copyright.xhtml"][1].startswith("--keep-doc (was: copyright page")
        assert dec["epigraph.xhtml"] == ("drop", "--drop-doc")

    def test_short_chapter_mentioning_copyright_is_not_dropped(self, tmp_path):
        """Content heuristics never fire on a document holding a TOC heading."""
        docs = {"ch1.xhtml": '<h1 id="c">Chapter 1</h1><p>He read: ISBN 12, all rights '
                             'reserved, copyright Acme Press.</p>'}
        epub = make_epub(tmp_path / "b.epub", docs, ncx=[("Chapter 1", "ch1.xhtml#c", [])])
        assert _decisions(epub)["ch1.xhtml"][0] == "keep"


# ---------------------------------------------------------------------------
# Full ingest
# ---------------------------------------------------------------------------

class TestIngest:
    def test_source_text_is_clean(self, publisher_epub, tmp_path):
        out = tmp_path / "proj"
        ingest_epub(publisher_epub, out)
        text = (out / "source.txt").read_text(encoding="utf-8")
        for artifact in ("ISBN", "Acme Press", "All rights", "Contents", "B UCEPHALUS", "CA ESAR"):
            assert artifact not in text
        assert "\nBUCEPHALUS\n" in text and "CAESAR’S HORSE" in text
        assert DOC_SENTINEL not in text
        assert "_eohippus_" in text
        assert "_Francis H. Rowley_" in text

    def test_images_extracted_and_artifacts_dropped(self, publisher_epub, tmp_path):
        out = tmp_path / "proj"
        result = ingest_epub(publisher_epub, out)
        files = {p.name for p in (out / "images").iterdir()}
        assert {"plate1.jpg", "plate2.jpg", "source_cover.jpg"} <= files
        assert not files & {"logo.png", "title.png", "blankx.png", "cover.jpg"}
        text = (out / "source.txt").read_text(encoding="utf-8")
        assert "[IMAGE:images/plate1.jpg]" in text and "blankx" not in text
        assert result.cover == "images/source_cover.jpg"

    def test_headings_outline_excludes_subtitles(self, publisher_epub, tmp_path):
        out = tmp_path / "proj"
        ingest_epub(publisher_epub, out)
        outline = json.loads((out / "headings.json").read_text(encoding="utf-8"))["headings"]
        assert outline == [
            {"level": 1, "text": "Dedication"},
            {"level": 1, "text": "BUCEPHALUS"},
            {"level": 1, "text": "CAESAR’S HORSE"},
            {"level": 1, "text": "JOAN OF ARC’S CHARGER"},
        ]
        text = (out / "source.txt").read_text(encoding="utf-8")
        # Subtitles stay in the text as the first line under their title.
        assert "BUCEPHALUS\n\nCHARGER OF ALEXANDER\n\nThe black" in text

    def test_paragraph_split_across_files_is_rejoined(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj")
        assert "he ran from the field while the crowd watched the horse." in result.text
        # The plate that interrupted the paragraph follows it.
        para_end = result.text.index("watched the horse.")
        assert result.text.index("[IMAGE:images/plate1.jpg]") > para_end
        assert len(result.joins) == 1

    def test_between_chapter_plate_stays_with_previous_chapter(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj")
        t = result.text
        plate = t.index("[IMAGE:images/plate2.jpg]")
        assert t.index("CAESAR’S HORSE") < plate < t.index("JOAN OF ARC’S CHARGER")
        # Not directly above the next heading, where the splitter would pull it forward.
        assert "plate2.jpg]\n\nJOAN" not in t
        assert len(result.anchored_images) == 1

    def test_boundary_images_next_leaves_plate_above_heading(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj", boundary_images="next")
        assert "plate2.jpg]\n\nJOAN OF ARC’S CHARGER" in result.text

    def test_lead_ins_recased_from_book_casing(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj")
        assert "The black yearling" in result.text
        assert "Soothsayers watched" in result.text
        assert "Joan of Arc stood" in result.text

    def test_no_recase_keeps_caps(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj", recase=False)
        assert "THE black yearling" in result.text
        assert result.recased == []

    def test_unheaded_front_matter_gets_heading(self, publisher_epub, tmp_path):
        result = ingest_epub(publisher_epub, tmp_path / "proj")
        assert result.synthetic_headings == [
            {"doc": "epigraph.xhtml", "label": "Dedication", "level": 1}]
        assert result.text.startswith("Dedication\n\nWhat form of life")

    def test_report_written(self, publisher_epub, tmp_path):
        out = tmp_path / "proj"
        ingest_epub(publisher_epub, out)
        report = json.loads((out / "ingest_report.json").read_text(encoding="utf-8"))
        assert report["format"] == "epub"
        assert report["metadata"]["title"] == "Test Horses"
        dropped = {d["doc"] for d in report["docs"] if d["decision"] == "drop"}
        assert dropped == {"cover.xhtml", "title.xhtml", "copyright.xhtml", "toc.xhtml"}

    def test_dry_run_writes_nothing(self, publisher_epub, tmp_path):
        out = tmp_path / "proj"
        ingest_epub(publisher_epub, out, write=False)
        assert not out.exists()


class TestHeadingsAndNav:
    def test_h_tags_and_nav_without_fragments(self, tmp_path):
        """Real <h*> headings plus an EPUB3 nav that links files only."""
        docs = {
            "c1.xhtml": "<section><header><h2>Chapter One</h2></header><p>Alpha text.</p></section>",
            "c2.xhtml": "<h2>Chapter Two</h2><h3>The Sequel</h3><p>Beta text.</p>",
        }
        nav = ('<nav epub:type="toc"><ol><li><a href="c1.xhtml">Chapter One</a></li>'
               '<li><a href="c2.xhtml">Chapter Two</a></li></ol></nav>')
        epub = make_epub(tmp_path / "b.epub", docs, nav=nav)
        result = ingest_epub(epub, tmp_path / "p")
        assert [(c["level"], c["heading"]) for c in result.chapters] == [
            (2, "Chapter One"), (2, "Chapter Two")]
        assert result.subtitles == ["The Sequel"]
        assert "Chapter One\n\nAlpha text." in result.text

    def test_pagebreaks_skipped(self, tmp_path):
        docs = {"c1.xhtml": '<h1>One</h1><p>Before <span epub:type="pagebreak" '
                            'title="7">7</span>after.</p>'}
        result = ingest_epub(make_epub(tmp_path / "b.epub", docs), tmp_path / "p")
        assert "Before after." in result.text

    def test_italic_font_family_class_becomes_underscores(self, tmp_path):
        """Living Book Press marks italics with an italic font, not font-style."""
        docs = {"c1.xhtml": '<h1>One</h1><p>The <span class="em">love</span> of animals.</p>'}
        css = ('@font-face { font-family: "GandhiSerif-Italic"; src: url("../fonts/g.otf"); }\n'
               ".em { font-family: GandhiSerif-Italic; }")
        result = ingest_epub(make_epub(tmp_path / "b.epub", docs, css=css), tmp_path / "p")
        assert "The _love_ of animals." in result.text


class TestFootnotes:
    @pytest.fixture
    def noted_epub(self, tmp_path):
        docs = {
            "c1.xhtml": (
                '<h1>One</h1><p>A claim<a epub:type="noteref" href="#n1">1</a> and another'
                '<a epub:type="noteref" href="notes.xhtml#e1">2</a> here.</p>'
                '<aside epub:type="footnote" id="n1"><p>First note.</p></aside>'
            ),
            "notes.xhtml": ('<h1>Notes</h1><ol><li epub:type="endnote" id="e1">'
                            '<p>Second note.</p></li></ol>'),
        }
        return make_epub(tmp_path / "b.epub", docs)

    def test_import(self, noted_epub, tmp_path):
        out = tmp_path / "p"
        result = ingest_epub(noted_epub, out, footnotes="import")
        assert "A claim[FOOTNOTE:1] and another[FOOTNOTE:2] here." in result.text
        notes = json.loads((out / "footnotes.json").read_text(encoding="utf-8"))
        bodies = [n["source_body"] for n in (notes["footnotes"] if isinstance(notes, dict) else notes)]
        assert bodies == ["First note.", "Second note."]
        # The Notes page held nothing else, so it goes too.
        assert result.emptied_docs == ["notes.xhtml"]
        assert "Notes" not in [c["heading"] for c in result.chapters]

    def test_drop(self, noted_epub, tmp_path):
        result = ingest_epub(noted_epub, tmp_path / "p", footnotes="drop")
        assert "A claim and another here." in result.text
        assert "note." not in result.text
        assert result.footnotes_count == 2


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_parse_italic_classes(self):
        css = ("span.Italic { font-style: italic; } .Em, p.x em.Other { font-style:oblique } "
               "span.sm { font-variant: small-caps } .Off { font-style: italic } "
               ".Off { font-style: normal }")
        got = parse_italic_classes([css])
        assert got == {"Italic": {"span"}, "Em": {"*"}, "Other": {"em"}}

    def test_parse_italic_classes_from_italic_font_family(self):
        css = (
            # Faces: an italic file with no font-style, an explicit italic face,
            # and a family with both a normal and an italic face.
            '@font-face { font-family: "Body"; src: url("../fonts/BodyItalic.ttf"); }'
            '@font-face { font-family: "Slanted"; font-style: italic; src: url("s.ttf"); }'
            '@font-face { font-family: "Serif"; src: url("Serif.ttf"); }'
            '@font-face { font-family: "Serif"; font-style: italic; src: url("Serif-Italic.ttf"); }'
            ".em { font-family: GandhiSerif-Italic; }"
            "span.em1 { font-family: 'CambriaItalic', serif; }"
            ".it { font-family: Body; } .sl { font-family: Slanted; }"
            ".plain { font-family: Serif; } .reg { font-family: Cambria; }"
            # A later non-italic family does not cancel an earlier font-style,
            # and a later non-italic family does cancel an earlier italic one.
            ".keep { font-style: italic; } .keep { font-family: Cambria; }"
            ".swap { font-family: CambriaItalic; } .swap { font-family: Cambria; }"
        )
        got = parse_italic_classes([css])
        assert got == {"em": {"*"}, "em1": {"span"}, "it": {"*"}, "sl": {"*"},
                       "keep": {"*"}}

    def test_recase_keeps_caps_only_acronym(self):
        text = "TITLE\n\nNASA ENGINEERS built it.\n\nThey said NASA would call the engineers."
        out, changes = recase_lead_ins(text, ["TITLE"], set())
        assert "NASA engineers built it." in out
        assert changes == [{"from": "NASA ENGINEERS", "to": "NASA engineers"}]

    def test_recase_skips_single_capital(self):
        text = "TITLE\n\nI went home."
        out, changes = recase_lead_ins(text, ["TITLE"], set())
        assert out == text and changes == []

    def test_join_skips_terminal_and_verse(self):
        s = DOC_SENTINEL
        ended = f"It ended.\n\n{s}\n\nlower start."
        assert join_split_paragraphs(ended, set())[1] == []
        verse = f"Line one\nline two\n\n{s}\n\nand more"
        assert join_split_paragraphs(verse, set())[1] == []
        cut = f"He went to the\n\n{s}\n\n[IMAGE:images/a.jpg]\n\n{s}\n\nstore today."
        text, joins = join_split_paragraphs(cut, set())
        assert text.startswith("He went to the store today.\n\n[IMAGE:images/a.jpg]")
        assert len(joins) == 1

    def test_anchor_requires_image_only_document(self):
        s = DOC_SENTINEL
        # Ornament inside the chapter's own document: left for the splitter.
        same_doc = f"Last para.\n\n{s}\n\n[IMAGE:images/o.png]\n\nTITLE\n\nBody."
        assert anchor_boundary_images(same_doc, {"TITLE"})[1] == []
        own_doc = f"Last para.\n\n{s}\n\n[IMAGE:images/p.jpg]\n\n{s}\n\nTITLE\n\nBody."
        text, moved = anchor_boundary_images(own_doc, {"TITLE"})
        assert text.startswith("[IMAGE:images/p.jpg]\n\nLast para.")
        assert len(moved) == 1

    def test_assign_image_names(self):
        names = assign_image_names(["A/img.jpg", "B/img.jpg", "A/cover.jpg"])
        assert names == {"A/img.jpg": "img.jpg", "B/img.jpg": "B_img.jpg",
                         "A/cover.jpg": "source_cover.jpg"}


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

class TestStageIngest:
    def test_stage_ingest_epub(self, publisher_epub, tmp_path):
        from scripts.translate_book import stage_ingest

        args = SimpleNamespace(url="", epub=str(publisher_epub), footnotes="drop")
        state = stage_ingest(args, tmp_path, {"url": "https://stale.example/"})
        assert state["stage_completed"] == "ingest"
        assert state["source_format"] == "epub"
        assert "url" not in state
        assert {d["doc"] for d in state["epub_dropped_docs"]} == {
            "cover.xhtml", "title.xhtml", "copyright.xhtml", "toc.xhtml"}
        assert [c["heading"] for c in state["chapter_report"]][:2] == ["Dedication", "BUCEPHALUS"]
        assert (tmp_path / "source.txt").exists() and (tmp_path / "headings.json").exists()

    def test_stage_ingest_passes_doc_overrides(self, publisher_epub, tmp_path):
        from scripts.translate_book import stage_ingest

        args = SimpleNamespace(url="", epub=str(publisher_epub), footnotes="drop",
                               epub_keep_docs=["copyright.xhtml"], epub_drop_docs=["epigraph.xhtml"])
        state = stage_ingest(args, tmp_path, {})
        text = (tmp_path / "source.txt").read_text(encoding="utf-8")
        assert "ISBN" in text and "What form of life" not in text
        assert "copyright.xhtml" not in {d["doc"] for d in state["epub_dropped_docs"]}

    def test_split_uses_the_outline(self, publisher_epub, tmp_path):
        from scripts.translate_book import stage_ingest, stage_split

        # The fixture is too small for auto level selection (>=5 sections of
        # >=400 chars), so pin the level the way a user would for a short book.
        args = SimpleNamespace(url="", epub=str(publisher_epub), footnotes="drop",
                               chapter_pattern="headings", custom_regex=None, min_chapter_size=10,
                               heading_level=1)
        state = stage_ingest(args, tmp_path, {})
        state = stage_split(args, tmp_path, state)
        chapters = sorted((tmp_path / "chapters").glob("chapter_*.txt"))
        assert len(chapters) == 4
        caesar = chapters[2].read_text(encoding="utf-8")
        assert caesar.startswith("CAESAR’S HORSE") and "plate2.jpg" in caesar
        assert "plate2.jpg" not in chapters[3].read_text(encoding="utf-8")


class TestWebRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        import web_ui.app as app_module

        projects_dir = tmp_path / "projects"
        (projects_dir / "p1").mkdir(parents=True)
        monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as client:
            yield client, projects_dir / "p1"

    def test_upload_ingests_and_reports(self, client, publisher_epub):
        client, proj = client
        with open(publisher_epub, "rb") as fh:
            rv = client.post("/api/project/p1/ingest-epub",
                             data={"file": (fh, "My Book.epub")},
                             content_type="multipart/form-data")
        assert rv.status_code == 200, rv.get_json()
        body = rv.get_json()
        assert body["ok"] and body["images_downloaded"] == 2
        assert {d["doc"] for d in body["dropped_docs"]} == {
            "cover.xhtml", "title.xhtml", "copyright.xhtml", "toc.xhtml"}
        assert (proj / "source.txt").exists() and (proj / "headings.json").exists()
        assert (proj / "My_Book.epub").exists()
        config = json.loads((proj / "project.json").read_text(encoding="utf-8"))
        assert config["source_format"] == "epub"

    def test_rejects_non_epub(self, client):
        client, _ = client
        rv = client.post("/api/project/p1/ingest-epub",
                         data={"file": (io.BytesIO(b"x"), "book.txt")},
                         content_type="multipart/form-data")
        assert rv.status_code == 400
