"""Tests for the reader sheet UI version (v2 vs classic).

The redesigned bottom sheet is now the default. Classic is still rendered, but
no longer offered on the home page: it is reachable through the per-device
`reader_ui_version` cookie, a `?ui=classic` link override, or the POST route.
"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def project_with_alignment(tmp_path, monkeypatch):
    """Minimal project with one aligned chapter, wired to a temp projects dir."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    align_dir = proj_dir / "alignments"
    align_dir.mkdir(parents=True)
    (proj_dir / "chunks").mkdir(parents=True, exist_ok=True)

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "es": "El gato.", "en": "The cat.",
             "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        ],
    }
    with open(align_dir / "chapter_01.json", "w", encoding="utf-8") as f:
        json.dump(alignment, f, ensure_ascii=False)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


READ_URL = "/read/test-project/chapter_01"

# Markers unique to each layout.
CLASSIC_MARKERS = ['id="sheet-edit-area"', 'id="bottom-sheet"']
V2_MARKERS = ['id="reader-sheet-v2"', 'data-tab="issues"']
V2_ASSET = "reader_sheet_v2.js"


class TestDefaultV2:
    def test_default_renders_v2_sheet(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        for m in V2_MARKERS:
            assert m in html
        assert V2_ASSET in html
        assert "reader_sheet_v2.css" in html
        assert 'window.READER_UI_VERSION = "v2"' in html
        # Classic sheet stays in the DOM but hidden (reader.js still drives it).
        assert 'id="classic-sheet-host"' in html
        assert 'id="bottom-sheet"' in html

    def test_toggle_absent_from_project_list(self, client, project_with_alignment):
        # The home page no longer offers the switch — v2 is simply the reader.
        html = client.get("/read/").get_data(as_text=True)
        assert "ui-version-toggle" not in html
        assert "data-ui-version" not in html

    def test_toggle_absent_from_chapter_reader(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "ui-version-toggle" not in html
        assert "data-ui-version" not in html


class TestCookieOptOut:
    def test_cookie_renders_classic(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "classic")
        html = client.get(READ_URL).get_data(as_text=True)
        for m in CLASSIC_MARKERS:
            assert m in html
        assert 'id="reader-sheet-v2"' not in html
        assert V2_ASSET not in html
        assert 'window.READER_UI_VERSION = "classic"' in html

    def test_bad_cookie_falls_back_to_v2(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "bogus")
        html = client.get(READ_URL).get_data(as_text=True)
        assert 'id="reader-sheet-v2"' in html
        assert 'window.READER_UI_VERSION = "v2"' in html


class TestQueryOverride:
    def test_ui_classic_renders_and_sets_cookie(self, client, project_with_alignment):
        rv = client.get(READ_URL + "?ui=classic")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' not in html
        assert "reader_ui_version=classic" in rv.headers.get("Set-Cookie", "")

    def test_ui_v2_overrides_classic_cookie_and_resets(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "classic")
        rv = client.get(READ_URL + "?ui=v2")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' in html
        assert "reader_ui_version=v2" in rv.headers.get("Set-Cookie", "")

    def test_invalid_ui_param_ignored(self, client, project_with_alignment):
        # An unknown ?ui= value is ignored (no cookie write) and cookie wins.
        client.set_cookie("reader_ui_version", "classic")
        rv = client.get(READ_URL + "?ui=nope")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' not in html  # cookie still classic
        assert "Set-Cookie" not in rv.headers  # override ignored, no write


class TestSetUiVersionRoute:
    def test_sets_cookie(self, client):
        rv = client.post("/api/set-ui-version", json={"version": "classic"})
        assert rv.get_json()["version"] == "classic"
        assert "reader_ui_version=classic" in rv.headers.get("Set-Cookie", "")

    def test_invalid_coerces_to_v2(self, client):
        rv = client.post("/api/set-ui-version", json={"version": "hacker"})
        assert rv.get_json()["version"] == "v2"
        assert "reader_ui_version=v2" in rv.headers.get("Set-Cookie", "")

    def test_missing_body_defaults_v2(self, client):
        rv = client.post("/api/set-ui-version", json={})
        assert rv.get_json()["version"] == "v2"


class TestAnnotationTypeAllowlist:
    """Regression: freeform ann.type used to land in v2 innerHTML class attrs."""

    def test_known_type_persisted(self, client, project_with_alignment):
        rv = client.post("/api/annotation", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "type": "footnote",
            "content": "note",
        })
        assert rv.status_code == 200
        path = project_with_alignment / "annotations.jsonl"
        last = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(last)["type"] == "footnote"

    def test_unknown_type_coerces_to_flag(self, client, project_with_alignment):
        rv = client.post("/api/annotation", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "type": '"><img src=x onerror=alert(1)>',
            "content": "x",
        })
        assert rv.status_code == 200
        path = project_with_alignment / "annotations.jsonl"
        last = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(last)["type"] == "flag"

    def test_v2_js_allowlists_types(self):
        js = (Path(__file__).resolve().parents[2]
              / "web_ui" / "static" / "reader_sheet_v2.js").read_text(encoding="utf-8")
        assert "function safeAnnType" in js
        assert "ANN_TYPES" in js
