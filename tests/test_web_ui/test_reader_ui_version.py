"""Tests for the opt-in reader sheet UI version (classic vs v2).

The redesigned bottom sheet ships behind a per-device `reader_ui_version` cookie
(mirroring the language switch). Classic stays the default and untouched; v2 is
opt-in via the cookie, a `?ui=` link override, or the toggle's POST route.
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


class TestDefaultClassic:
    def test_default_renders_classic_sheet(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        for m in CLASSIC_MARKERS:
            assert m in html
        # v2 sheet + assets must be absent on the default path.
        assert 'id="reader-sheet-v2"' not in html
        assert V2_ASSET not in html
        assert 'window.READER_UI_VERSION = "classic"' in html

    def test_toggle_present_with_both_options(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "ui-version-toggle" in html
        assert 'data-ui-version="classic"' in html
        assert 'data-ui-version="v2"' in html


class TestCookieOptIn:
    def test_cookie_renders_v2(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "v2")
        html = client.get(READ_URL).get_data(as_text=True)
        for m in V2_MARKERS:
            assert m in html
        assert V2_ASSET in html
        assert "reader_sheet_v2.css" in html
        assert 'window.READER_UI_VERSION = "v2"' in html
        # Classic sheet stays in the DOM but hidden (reader.js still drives it).
        assert 'id="classic-sheet-host"' in html
        assert 'id="bottom-sheet"' in html

    def test_bad_cookie_falls_back_to_classic(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "bogus")
        html = client.get(READ_URL).get_data(as_text=True)
        assert 'id="reader-sheet-v2"' not in html
        assert 'window.READER_UI_VERSION = "classic"' in html


class TestQueryOverride:
    def test_ui_v2_renders_and_sets_cookie(self, client, project_with_alignment):
        rv = client.get(READ_URL + "?ui=v2")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' in html
        set_cookie = rv.headers.get("Set-Cookie", "")
        assert "reader_ui_version=v2" in set_cookie

    def test_ui_classic_overrides_v2_cookie_and_resets(self, client, project_with_alignment):
        client.set_cookie("reader_ui_version", "v2")
        rv = client.get(READ_URL + "?ui=classic")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' not in html
        assert "reader_ui_version=classic" in rv.headers.get("Set-Cookie", "")

    def test_invalid_ui_param_ignored(self, client, project_with_alignment):
        # An unknown ?ui= value is ignored (no cookie write) and cookie wins.
        client.set_cookie("reader_ui_version", "v2")
        rv = client.get(READ_URL + "?ui=nope")
        html = rv.get_data(as_text=True)
        assert 'id="reader-sheet-v2"' in html  # cookie still v2
        assert "Set-Cookie" not in rv.headers  # override ignored, no write


class TestSetUiVersionRoute:
    def test_sets_cookie(self, client):
        rv = client.post("/api/set-ui-version", json={"version": "v2"})
        assert rv.get_json()["version"] == "v2"
        assert "reader_ui_version=v2" in rv.headers.get("Set-Cookie", "")

    def test_invalid_coerces_to_classic(self, client):
        rv = client.post("/api/set-ui-version", json={"version": "hacker"})
        assert rv.get_json()["version"] == "classic"
        assert "reader_ui_version=classic" in rv.headers.get("Set-Cookie", "")

    def test_missing_body_defaults_classic(self, client):
        rv = client.post("/api/set-ui-version", json={})
        assert rv.get_json()["version"] == "classic"
