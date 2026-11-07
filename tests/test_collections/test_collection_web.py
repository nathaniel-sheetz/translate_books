"""The web UI's collections: one library card, the collection page, the reader's next work."""

from __future__ import annotations

import json
import re

import pytest

import web_ui.app as app_module
from src import collection as collection_mod
from tests.test_collections.conftest import write_json
from web_ui.app import app
from web_ui.project_cards import clear_card_cache


def _align_every_chapter(project):
    for chunk in sorted((project / "chunks").glob("*.json")):
        chapter_id = chunk.stem.split("_chunk_")[0]
        path = project / "alignments" / f"{chapter_id}.json"
        if not path.exists():
            write_json(path, {"chapter_id": chapter_id,
                              "alignments": [{"es_idx": 0, "es": "Texto.", "en": "Text."}]})


@pytest.fixture
def client(library, plumer, monkeypatch):
    for project in plumer:
        _align_every_chapter(project)
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: library)
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})
    clear_card_cache()
    collection_mod.create("plumer-volumen", members=["short-sermons", "plain-thoughts"],
                          title="Sermones y pensamientos")
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.set_cookie("reader_lang", "es")
        yield c


def test_library_shows_one_collection_card_and_hides_its_members(client):
    html = client.get("/read/").get_data(as_text=True)
    assert 'href="/read/collection/plumer-volumen"' in html
    assert "Colección · 2 obras" in html
    assert 'href="/read/short-sermons"' not in html
    assert 'href="/read/plain-thoughts"' not in html
    assert 'id="show-works-btn"' in html and 'aria-pressed="false"' in html


def test_show_works_toggle_lists_the_members_too(client):
    resp = client.post("/api/set-show-works", json={"show": True})
    assert resp.get_json() == {"ok": True, "show": True}
    html = client.get("/read/").get_data(as_text=True)
    assert 'href="/read/short-sermons"' in html
    assert 'href="/read/plain-thoughts"' in html
    assert 'href="/read/collection/plumer-volumen"' in html


def test_a_library_without_collections_has_no_toggle(client, library):
    (library / "plumer-volumen" / "collection.json").unlink()
    html = client.get("/read/").get_data(as_text=True)
    assert 'id="show-works-btn"' not in html
    assert 'href="/read/short-sermons"' in html


def _section(html: str, slug: str) -> str:
    """One work's section of the collection page."""
    return html.split(f'id="work-{slug}"', 1)[1].split("</section>", 1)[0]


def test_collection_page_lists_works_then_chapters(client):
    resp = client.get("/read/collection/plumer-volumen")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<h1 class=\"book-title\">Sermones y pensamientos</h1>" in html
    assert html.index('id="work-short-sermons"') < html.index('id="work-plain-thoughts"')
    for href in ("/read/short-sermons/chapter_01?collection=plumer-volumen",
                 "/read/short-sermons/chapter_03?collection=plumer-volumen",
                 "/read/plain-thoughts/chapter_01?collection=plumer-volumen"):
        assert href in html
    # The EPUB is built on the volume's dashboard, which the page's ⋮ leads to.
    assert "btn-collection-epub" not in html and "collection-status" not in html
    top = html.split('id="collection-menu"', 1)[1].split("</div>\n            </div>", 1)[0]
    assert 'href="/project/plumer-volumen"' in top


def test_each_work_collapses_to_a_summary_with_its_menu(client):
    html = client.get("/read/collection/plumer-volumen").get_data(as_text=True)
    sermons = _section(html, "short-sermons")
    # Collapsed: the toggle, the summary and the ⋮ menu; the book view is hidden.
    assert 'class="work-toggle" type="button" aria-expanded="false" aria-controls="work-body-short-sermons"' in sermons
    assert "3/3 capítulos" in sermons
    assert re.search(r'id="work-body-short-sermons"[^>]*hidden', sermons)
    for href in ("/read/short-sermons", "/project/short-sermons", "/book-review/short-sermons",
                 "/recommendations/short-sermons", "/image-pass/short-sermons",
                 "/quotations/short-sermons"):
        assert f'href="{href}"' in sermons
    # Open: what the work's own page shows, with ids of its own.
    assert 'id="w1-phase-strip"' in sermons and 'aria-controls="w1-tab-process"' in sermons
    assert 'id="w1-tab-btn-chapters"' in sermons and 'id="w1-tab-process"' in sermons
    assert 'id="w1-ch-chapter_01"' in sermons
    assert 'id="w2-tab-chapters"' in _section(html, "plain-thoughts")
    assert 'id="works-toggle-all"' in html


def test_a_work_with_nothing_to_read_leads_to_its_dashboard(client, library):
    from tests.test_collections.conftest import make_project

    make_project(library, "fresh", chapters=[("ONE\n\nOne.", None)])
    coll = collection_mod.load_collection(library / "plumer-volumen")
    collection_mod.add_member(coll, "fresh")
    section = _section(client.get("/read/collection/plumer-volumen").get_data(as_text=True), "fresh")
    assert "Aún no hay capítulos para leer." in section
    assert 'href="/read/fresh"' not in section and 'href="/project/fresh"' in section


def test_triage_strip_keeps_the_works_id_prefix(client):
    data = client.get("/api/book-phases/short-sermons/triage?idp=w1-").get_json()
    assert 'id="w1-phase-strip"' in data["strip"]
    data = client.get("/api/book-phases/short-sermons/triage").get_json()
    assert 'id="phase-strip"' in data["strip"]
    assert client.get("/api/book-phases/short-sermons/triage?idp=..").status_code == 400


def test_library_card_menu_leads_to_the_volume_dashboard(client):
    html = client.get("/read/").get_data(as_text=True)
    card = html.split('href="/read/collection/plumer-volumen"', 1)[1].split("</article>", 1)[0]
    assert 'href="/project/plumer-volumen"' in card
    assert 'data-project-id="plumer-volumen"' in card


def test_archiving_a_collection_reports_its_own_status(client, library):
    resp = client.patch("/api/project/plumer-volumen/archived", json={"archived": True})
    assert resp.get_json()["status"] == "archived"
    resp = client.patch("/api/project/plumer-volumen/archived", json={"archived": False})
    assert resp.get_json()["status"] != "archived"


def test_volume_dashboard_has_only_the_export_step(client):
    html = client.get("/project/plumer-volumen").get_data(as_text=True)
    for stage in ("source", "split", "chunk", "style-guide", "glossary", "translate", "review"):
        assert f'<li data-stage="{stage}" class="disabled" aria-disabled="true"' in html
    assert '<li data-stage="export">' in html
    assert 'href="/project/short-sermons"' in html and 'href="/project/plain-thoughts"' in html
    assert 'href="/read/collection/plumer-volumen"' in html
    assert 'id="epub-allow-partial"' in html
    assert 'id="epub-source-title"' not in html
    assert "window.DASHBOARD_IS_COLLECTION = true" in html


def test_a_book_dashboard_is_unchanged(client):
    html = client.get("/project/short-sermons").get_data(as_text=True)
    assert 'class="disabled"' not in html
    assert 'id="epub-source-title"' in html and 'id="epub-allow-partial"' not in html
    assert "window.DASHBOARD_IS_COLLECTION = false" in html


def test_unknown_collection_is_404(client):
    assert client.get("/read/collection/nope").status_code == 404


def test_last_chapter_of_a_member_leads_to_the_next_work(client):
    html = client.get("/read/short-sermons/chapter_03?collection=plumer-volumen").get_data(as_text=True)
    assert 'href="/read/plain-thoughts/chapter_01?collection=plumer-volumen"' in html
    assert "Siguiente obra: Pensamientos sencillos" in html
    assert 'href="/read/collection/plumer-volumen#work-short-sermons"' in html
    # Inside the member, prev/next are unchanged but keep the context.
    assert 'href="/read/short-sermons/chapter_02?collection=plumer-volumen"' in html


def test_without_the_collection_param_the_reader_is_unchanged(client):
    html = client.get("/read/short-sermons/chapter_03").get_data(as_text=True)
    assert "plain-thoughts" not in html
    assert 'href="/read/short-sermons?from=chapter_03"' in html


def test_the_last_work_has_no_next_work(client):
    html = client.get("/read/plain-thoughts/chapter_01?collection=plumer-volumen").get_data(as_text=True)
    assert "nav-next-work" not in html
    assert 'href="/read/collection/plumer-volumen#work-plain-thoughts"' in html


def test_a_collection_param_naming_another_book_is_ignored(client):
    html = client.get("/read/short-sermons/chapter_03?collection=nope").get_data(as_text=True)
    assert "nav-next-work" not in html


def _split_chapters(project):
    """The chapters/ files a split leaves, which the Export step counts."""
    for chunk in sorted((project / "chunks").glob("*.json")):
        chapter_id = chunk.stem.split("_chunk_")[0]
        (project / "chapters").mkdir(exist_ok=True)
        (project / "chapters" / f"{chapter_id}.txt").write_text("x", encoding="utf-8")


def _add_half_done(library):
    from tests.test_collections.conftest import make_project

    half = make_project(library, "half-done",
                        chapters=[("ONE\n\nOne.", "UNO\n\nUno."), ("TWO\n\nTwo.", None)])
    coll = collection_mod.load_collection(library / "plumer-volumen")
    collection_mod.add_member(coll, "half-done")
    return half


def test_epub_status_sums_the_works(client, library, plumer):
    for project in plumer:
        _split_chapters(project)
    _split_chapters(_add_half_done(library))
    data = client.get("/api/project/plumer-volumen/epub-status").get_json()
    assert data["is_collection"]
    assert (data["translated_chapters"], data["total_chapters"]) == (5, 6)
    by_id = {w["id"]: w for w in data["works"]}
    assert (by_id["half-done"]["translated"], by_id["half-done"]["total"]) == (1, 2)
    assert by_id["short-sermons"]["title"] == "Sermones cortos"
    assert data["author_fallback"] == "William S. Plumer"
    assert data["spanish_title"] == "Sermones y pensamientos"


def test_a_book_epub_status_is_not_a_collection(client, plumer):
    _split_chapters(plumer[0])
    data = client.get("/api/project/short-sermons/epub-status").get_json()
    assert data["is_collection"] is False and data["total_chapters"] == 3
    assert "works" not in data


def test_build_and_download_the_volume_epub(client, library):
    resp = client.post("/api/project/plumer-volumen/build-epub", json={
        "title": "Sermones y pensamientos", "publisher": "Libros Vivos",
        "translator_heading": "Nota", "translator_note": "Una nota del traductor."})
    data = resp.get_json()
    assert resp.status_code == 200, data
    assert data["filename"] == "Sermones y pensamientos.epub" and data["endnotes"] == 2
    assert len(data["works"]) == 2 and data["chapters_included"] == 4 and not data["skipped"]
    # The form's fields land in the volume's own files, where the build reads them.
    config = json.loads((library / "plumer-volumen" / "project.json").read_text(encoding="utf-8"))
    assert config["publisher"] == "Libros Vivos"
    note = json.loads((library / "plumer-volumen" / "translator_note.json").read_text(encoding="utf-8"))
    assert note == {"heading": "Nota", "body": "Una nota del traductor."}
    download = client.get("/api/project/plumer-volumen/download-epub")
    assert download.status_code == 200
    assert download.data[:2] == b"PK"


def test_build_reports_an_untranslated_member(client, library):
    _add_half_done(library)
    resp = client.post("/api/project/plumer-volumen/build-epub", json={})
    assert resp.status_code == 400 and "half-done" in resp.get_json()["error"]
    resp = client.post("/api/project/plumer-volumen/build-epub", json={"allow_partial": True})
    data = resp.get_json()
    assert resp.status_code == 200, data
    assert data["skipped"] == 1 and len(data["works"]) == 3


def test_a_nested_collection_resolves(client, library):
    import shutil

    (library / "shelf").mkdir()
    shutil.move(str(library / "plumer-volumen"), str(library / "shelf" / "plumer-volumen"))
    assert client.get("/api/project/plumer-volumen/epub-status").get_json()["is_collection"]
    assert client.get("/project/plumer-volumen").status_code == 200


def test_collection_card_aggregates_member_cards(library, plumer):
    from web_ui.project_cards import build_collection_card, build_project_card

    clear_card_cache()
    coll, _ = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    cards = [build_project_card(m.path, m.slug) for m in coll.members]
    card = build_collection_card(coll.path, cards)
    assert card["is_collection"] and card["members"] == ["short-sermons", "plain-thoughts"]
    assert card["chapters_total"] == 4 and card["chapters_translated"] == 4
    assert card["total_chunks"] == sum(c["total_chunks"] for c in cards)
    # No style guide anywhere: the volume waits on setup, like its members.
    assert card["status"] == "pending"
    write_json(coll.path / "project.json", {"archived": True})
    assert build_collection_card(coll.path, cards)["status"] == "archived"


def test_a_corrupt_collection_json_is_not_a_500(client, library):
    (library / "plumer-volumen" / "collection.json").write_text("{not json", encoding="utf-8")
    resp = client.post("/api/project/plumer-volumen/build-epub", json={})
    assert resp.status_code < 500, resp.get_json()


def test_the_collection_card_shows_the_volumes_own_cover(client, library):
    from PIL import Image

    (library / "plumer-volumen" / "images").mkdir()
    Image.new("RGB", (60, 90), (200, 30, 30)).save(library / "plumer-volumen" / "images" / "cover.png")
    resp = client.get("/projects/plumer-volumen/cover-thumb")
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"
