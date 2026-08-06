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

    def test_css_cache_bust_present(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "reader.css?v=" in html


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

    def test_js_exposes_rederive_helper(self):
        js = READER_JS.read_text(encoding="utf-8")
        assert "function rederiveTourPos(" in js
        assert "rederiveTourPos(tour.stops, tour.pos, stops)" in js


def rederive_tour_pos(prev_stops, prev_pos, new_stops):
    """Mirror of reader.js ``rederiveTourPos`` — keep in lockstep with that helper."""
    landed = prev_stops[prev_pos] if prev_pos >= 0 else None
    if landed is None:
        return -1
    try:
        return new_stops.index(landed)
    except ValueError:
        pass
    after = next((i for i, s in enumerate(new_stops) if s > landed), None)
    if after is None:
        return len(new_stops) - 1
    return after - 1


class TestTourPosRederive:
    """Behaviour of the tour cursor across save/delete (no JS runner)."""

    def test_untouched_tour_stays_unstarted(self):
        assert rederive_tour_pos([0, 5, 9], -1, [0, 5, 9]) == -1

    def test_landed_sentence_still_present_keeps_exact_index(self):
        # Was on es_idx 5 (pos 1); a different stop vanished — stay on 5.
        assert rederive_tour_pos([0, 5, 9], 1, [5, 9]) == 0

    def test_delete_landed_advances_to_next_stop_not_restart(self):
        # Landed on 5; 5 deleted → park just before the next stop (9) so the
        # following tap lands on 9 rather than wrapping back to 0.
        assert rederive_tour_pos([0, 5, 9], 1, [0, 9]) == 0
        # Next tap: (0 + 1) % 2 → index 1 → es_idx 9.
        assert [0, 9][0 + 1] == 9

    def test_delete_last_stop_parks_at_end_so_next_tap_wraps(self):
        assert rederive_tour_pos([0, 5, 9], 2, [0, 5]) == 1
        # Next tap wraps: (1 + 1) % 2 → 0.
        assert (1 + 1) % 2 == 0

    def test_delete_all_stops_yields_unstarted(self):
        assert rederive_tour_pos([0, 5], 0, []) == -1
