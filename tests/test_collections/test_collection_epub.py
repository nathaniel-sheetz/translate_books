"""The volume EPUB: spine, nested TOC, part pages, endnotes and images across members."""

from __future__ import annotations

import re
import zipfile

import pytest

from src import collection as collection_mod
from src.epub_builder import build_collection_epub, part_page_to_xhtml
from tests.test_collections.conftest import PNG_A, PNG_B, make_project, write_json


def _build(library, *, allow_partial=False, **create):
    coll, _ = collection_mod.create(
        "plumer-volumen", members=create.pop("members", ["short-sermons", "plain-thoughts"]),
        title="Sermones y pensamientos", **create)
    write_json(coll.path / "project.json", {
        "spanish_title": "Sermones y pensamientos", "front_matter": {"title_page": True}})
    write_json(coll.path / "edition_notice.json", {"heading": "Créditos", "body": "Aviso."})
    return coll, build_collection_epub(coll.path, allow_partial=allow_partial)


def _read(epub_path):
    with zipfile.ZipFile(epub_path) as z:
        return {name: z.read(name) for name in z.namelist()}


def _text(files, suffix):
    (name,) = [n for n in files if n.endswith(suffix)]
    text = files[name].decode("utf-8")
    # ebooklib pretty-prints the nav and NCX; compare them without the indentation.
    return re.sub(r">\s+<", "><", text) if suffix.endswith(("nav.xhtml", ".ncx")) else text


def _spine(opf):
    manifest = dict(re.findall(r'<item href="([^"]+)" id="([^"]+)"', opf))
    by_id = {v: k for k, v in manifest.items()}
    return [by_id[i] for i in re.findall(r'<itemref idref="([^"]+)"', opf)]


def test_spine_order(library, plumer):
    _, result = _build(library)
    files = _read(result.path)
    assert _spine(_text(files, ".opf")) == [
        "titlepage.xhtml", "copyright.xhtml", "nav.xhtml",
        "w01_part.xhtml", "w01_chapter_01.xhtml", "w01_chapter_02.xhtml",
        "w01_chapter_03.xhtml",
        "w02_chapter_01.xhtml",
        "endnotes.xhtml",
    ]


def test_nested_toc_for_the_multi_chapter_work_and_flat_entry_for_the_story(library, plumer):
    _, result = _build(library)
    nav = _text(_read(result.path), "nav.xhtml")
    toc = nav.split('epub:type="toc"', 1)[1]
    # The sermons section links to its part page and nests its three entries.
    section = re.search(
        r'<li><a href="w01_part.xhtml">Sermones cortos</a><ol>(.*?)</ol></li>', toc, re.S)
    assert section, toc
    assert re.findall(r'href="([^"]+)"', section.group(1)) == [
        "w01_chapter_01.xhtml", "w01_chapter_02.xhtml", "w01_chapter_03.xhtml"]
    # The story is one flat entry under its own (title-cased) first line.
    assert '<li><a href="w02_chapter_01.xhtml">Pensamientos Sencillos</a></li>' in toc
    assert "Capítulo 1" not in toc


def test_a_part_page_only_for_the_multi_chapter_member(library, plumer):
    _, result = _build(library)
    files = _read(result.path)
    part = _text(files, "w01_part.xhtml")
    assert "<h1>Sermones cortos</h1>" in part
    assert "<em>Short Sermons to Little Children</em>" in part
    assert "William S. Plumer" in part and "1848" in part
    assert not any(n.endswith("w02_part.xhtml") for n in files)


def test_the_story_heading_is_its_title_not_a_chapter_number(library, plumer):
    _, result = _build(library)
    story = _text(_read(result.path), "w02_chapter_01.xhtml")
    assert "<h1>Pensamientos Sencillos</h1>" in story
    assert "Capítulo" not in story


def test_part_page_setting_overrides_auto(library, plumer):
    coll, _ = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    doc = coll.to_json()
    doc["members"][0]["part_page"] = False
    doc["members"][1]["part_page"] = True
    write_json(coll.path / "collection.json", doc)
    files = _read(build_collection_epub(coll.path).path)
    assert not any(n.endswith("w01_part.xhtml") for n in files)
    assert any(n.endswith("w02_part.xhtml") for n in files)
    nav = _text(files, "nav.xhtml")
    # No part page: the sermons section opens on its first page.
    assert '<li><a href="w01_chapter_01.xhtml">Sermones cortos</a><ol>' in nav
    ncx = _text(files, "toc.ncx")
    assert 'src=""' not in ncx


def test_endnotes_number_continuously_with_back_links(library, plumer):
    _, result = _build(library)
    files = _read(result.path)
    assert result.endnotes == 2
    sermon = _text(files, "w01_chapter_02.xhtml")
    story = _text(files, "w02_chapter_01.xhtml")
    assert 'id="enref-1" href="endnotes.xhtml#en-1"' in sermon
    assert 'id="enref-2" href="endnotes.xhtml#en-2"' in story
    notes = _text(files, "endnotes.xhtml")
    assert 'id="en-1" href="w01_chapter_02.xhtml#enref-1"' in notes
    assert 'id="en-2" href="w02_chapter_01.xhtml#enref-2"' in notes
    # Grouped by work, then chapter; a one-chapter story gets no chapter heading.
    headings = re.findall(r"<(h[123])>([^<]+)</h[123]>", notes)
    assert headings == [
        ("h1", "Notas"),
        ("h2", "Sermones cortos"), ("h3", "Sermón I"),
        ("h2", "Pensamientos sencillos"),
    ]


def test_both_images_are_embedded_under_distinct_paths(library, plumer):
    _, result = _build(library)
    files = _read(result.path)
    images = {n.split("EPUB/", 1)[-1]: data for n, data in files.items() if "images/" in n}
    assert images == {"images/w01/illus01.jpg": PNG_A, "images/w02/illus01.jpg": PNG_B}
    assert '<img src="images/w01/illus01.jpg"' in _text(files, "w01_chapter_02.xhtml")
    assert '<img src="images/w02/illus01.jpg"' in _text(files, "w02_chapter_01.xhtml")


def test_metadata_one_source_per_member_and_one_creator_per_author(library, plumer):
    make_project(library, "macdonald-story", chapters=[("THE GIANT\n\nOnce.", "EL GIGANTE\n\nUna vez.")],
                 project={"spanish_title": "El gigante", "author": "George MacDonald"},
                 harness={"title": "The Giant's Heart"})
    _, result = _build(library, members=["short-sermons", "plain-thoughts", "macdonald-story"])
    opf = _text(_read(result.path), ".opf")
    assert re.findall(r"<dc:source>([^<]+)</dc:source>", opf) == [
        "Short Sermons to Little Children", "Plain Thoughts for Children", "The Giant's Heart"]
    assert re.findall(r"<dc:creator[^>]*>([^<]+)</dc:creator>", opf) == [
        "William S. Plumer", "George MacDonald"]
    assert "<dc:title>Sermones y pensamientos</dc:title>" in opf
    title_page = _text(_read(result.path), "titlepage.xhtml")
    assert "William S. Plumer y George MacDonald" in title_page


def test_an_untranslated_member_fails_unless_partial(library, plumer):
    make_project(library, "half-done", chapters=[("ONE\n\nOne.", "UNO\n\nUno."), ("TWO\n\nTwo.", None)],
                 project={"spanish_title": "A medias"})
    coll, _ = collection_mod.create("vol", members=["short-sermons", "half-done"])
    with pytest.raises(ValueError, match="half-done: 1 untranslated"):
        build_collection_epub(coll.path)

    result = build_collection_epub(coll.path, allow_partial=True)
    assert result.works == [
        {"slug": "short-sermons", "included": ["chapter_01", "chapter_02", "chapter_03"],
         "skipped": []},
        {"slug": "half-done", "included": ["chapter_01"], "skipped": ["chapter_02"]},
    ]
    spine = _spine(_text(_read(result.path), ".opf"))
    # half-done is still a multi-chapter work, so it keeps its part page.
    assert spine[-3:] == ["w02_part.xhtml", "w02_chapter_01.xhtml", "endnotes.xhtml"]


def test_a_missing_member_is_an_error(library, plumer):
    coll, _ = collection_mod.create("vol", members=["short-sermons"])
    doc = coll.to_json()
    doc["members"].append({"slug": "gone"})
    write_json(coll.path / "collection.json", doc)
    with pytest.raises(FileNotFoundError, match="gone"):
        build_collection_epub(coll.path)


def test_the_epub_is_written_in_the_collection_folder(library, plumer):
    coll, result = _build(library)
    assert result.path == coll.path / "Sermones y pensamientos.epub"


def test_harness_collection_epub(library, plumer):
    from src.harness import flow

    collection_mod.create("vol", members=["short-sermons", "plain-thoughts"], title="Volumen")
    out = flow.collection_epub("vol")
    assert out["epub_path"].endswith("Volumen.epub")
    assert out["endnotes"] == 2
    assert set(out) <= set(flow.OUTPUT_SCHEMAS["collection-epub"])


def test_kdp_verify_accepts_one_creator_per_member_author(library, plumer):
    from src.kdp.verify import verify

    make_project(library, "macdonald-story", chapters=[("THE GIANT\n\nOnce.", "EL GIGANTE\n\nUna vez.")],
                 project={"spanish_title": "El gigante", "author": "George MacDonald"})
    coll, result = _build(library, members=["short-sermons", "plain-thoughts", "macdonald-story"])
    report = verify(coll.path, result.path)
    checks = {c["check"]: c["ok"] for c in report["checks"]}
    assert checks["author"] and checks["title"] and checks["title_page_first"]
    assert checks["edition_notice"]
    assert report["ok"], report


def test_part_page_skips_an_original_title_equal_to_the_title():
    page = part_page_to_xhtml("Phantastes", "Phantastes", "George MacDonald", None)
    assert "<em>" not in page and "George MacDonald" in page
