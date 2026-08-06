"""Tests for the split annotation/footnote nav in the reader top bar.

The single `#reader-stats` counter walked every annotated sentence, footnotes
included. It is replaced by two buttons — `#btn-ann-nav` (review notes) and
`#btn-fn-nav` (footnotes) — each cycling only its own class and hidden while its
count is zero. JS has no runner here, so the behaviour that lives in reader.js is
asserted by reading the source (the pattern in test_reader_ui_version.py).
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
READER_JS = Path(__file__).resolve().parents[2] / "web_ui" / "static" / "reader.js"


def _topbar_button(html, button_id):
    """The markup of the top-bar button with this id, up to its closing '>'."""
    start = html.index(f'id="{button_id}"')
    open_tag = html.rindex("<button", 0, start)
    return html[open_tag:html.index(">", start) + 1]


class TestTopbarMarkup:
    def test_both_nav_buttons_render_hidden(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        for button_id in ("btn-ann-nav", "btn-fn-nav"):
            assert f'id="{button_id}"' in html
            # JS unhides each button when its count is non-zero, so the server
            # must ship them hidden — otherwise empty buttons flash on load.
            assert "hidden" in _topbar_button(html, button_id)
        assert 'id="ann-nav-count"' in html
        assert 'id="fn-nav-count"' in html

    def test_old_single_counter_is_gone(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert 'id="reader-stats"' not in html

    def test_buttons_are_labelled(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "Jump through annotations" in html
        assert "Jump through footnotes" in html

    def test_spanish_labels(self, client, project_with_alignment):
        client.set_cookie("reader_lang", "es")
        html = client.get(READ_URL).get_data(as_text=True)
        assert "Recorrer las anotaciones" in html
        assert "Recorrer las notas al pie" in html


class TestReaderJsSource:
    def test_js_drives_both_buttons(self):
        js = READER_JS.read_text(encoding="utf-8")
        for element_id in ("btn-ann-nav", "ann-nav-count", "btn-fn-nav", "fn-nav-count"):
            assert element_id in js

    def test_js_no_longer_references_old_counter(self):
        js = READER_JS.read_text(encoding="utf-8")
        assert "reader-stats" not in js
        assert "STICKY_NOTE_SVG" not in js

    def test_js_splits_tours_on_footnote_type(self):
        # "not a footnote" rather than an allowlist, so an unknown/legacy type
        # still lands in the annotations tour instead of vanishing.
        js = READER_JS.read_text(encoding="utf-8")
        assert "a.type !== 'footnote'" in js
        assert "a.type === 'footnote'" in js
