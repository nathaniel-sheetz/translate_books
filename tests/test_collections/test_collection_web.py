"""The web UI's collections: one library card, the collection page, the reader's next work."""

from __future__ import annotations

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
    # The status table: one row per work with its share translated.
    assert "100% · 3/3" in html and "100% · 1/1" in html
    assert 'id="btn-collection-epub"' in html


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


def test_build_and_download_the_volume_epub(client, library):
    resp = client.post("/api/collection/plumer-volumen/epub", json={})
    data = resp.get_json()
    assert resp.status_code == 200, data
    assert data["name"] == "Sermones y pensamientos.epub" and data["endnotes"] == 2
    download = client.get(data["download"])
    assert download.status_code == 200
    assert download.data[:2] == b"PK"


def test_build_reports_an_untranslated_member(client, library):
    from tests.test_collections.conftest import make_project

    make_project(library, "half-done", chapters=[("ONE\n\nOne.", "UNO\n\nUno."), ("TWO\n\nTwo.", None)])
    coll = collection_mod.load_collection(library / "plumer-volumen")
    collection_mod.add_member(coll, "half-done")
    resp = client.post("/api/collection/plumer-volumen/epub", json={})
    assert resp.status_code == 400 and "half-done" in resp.get_json()["error"]
    resp = client.post("/api/collection/plumer-volumen/epub", json={"allow_partial": True})
    assert resp.status_code == 200


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
