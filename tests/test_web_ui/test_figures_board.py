"""The figures board routes (/figures/<id> and its API). The plan logic is
tested in tests/test_figures.py; these pin the HTTP surface."""

from __future__ import annotations

import json

import pytest

from web_ui.app import app

FIG = {"id": "leaf035_1", "src": "", "image": "images/plate_leaf035_1.jpg",
       "captions_en": ["JENNY WREN. A wren."], "captions_es": [], "alt_en": "Jenny Wren",
       "alt_es": "", "chapter_id": None, "anchor_en": None, "status": "draft"}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def book(tmp_path, monkeypatch):
    import web_ui.app as webapp

    root = tmp_path / "fbook"
    root.mkdir()
    (root / "figures.json").write_text(json.dumps({"version": 1, "figures": [FIG]}),
                                       encoding="utf-8")
    monkeypatch.setattr(webapp, "_resolve_project_dir", lambda pid: tmp_path / pid)
    return root


def test_a_non_string_field_is_refused_and_nothing_is_saved(client, book):
    before = (book / "figures.json").read_text(encoding="utf-8")
    for body in ({"alt_es": 5}, {"chapter_id": ["chapter_01"]}, {"status": True},
                 {"captions_es": "UNO."}, {"captions_es": [1]}):
        r = client.post("/api/project/fbook/figures/leaf035_1", json=body)
        assert r.status_code == 400, body
    assert (book / "figures.json").read_text(encoding="utf-8") == before
    assert client.get("/api/project/fbook/figures").status_code == 200


def test_a_string_field_saves_and_returns_the_row(client, book):
    r = client.post("/api/project/fbook/figures/leaf035_1", json={"alt_es": "Lupita"})
    assert r.status_code == 200 and r.get_json()["alt_es"] == "Lupita"
    saved = json.loads((book / "figures.json").read_text(encoding="utf-8"))
    assert saved["figures"][0]["alt_es"] == "Lupita"
