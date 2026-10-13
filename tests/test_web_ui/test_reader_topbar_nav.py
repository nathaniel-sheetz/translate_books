"""Tests for the split annotation/flag/footnote nav in the reader top bar.

The single `#reader-stats` counter walked every annotated sentence, footnotes
included. It is replaced by three buttons — `#btn-ann-nav` (review notes),
`#btn-flag-nav` (review findings and open save-check warnings) and `#btn-fn-nav`
(footnotes) — each cycling only its own class and hidden while its count is
zero. On a phone only the active one shows, and `#btn-tour-pick` opens the
picker that switches it. JS has no runner here, so the behaviour that lives in
reader.js is asserted by reading the source (the pattern in
test_reader_ui_version.py).
"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app
from web_ui.i18n import STRINGS


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
READER_CSS = Path(__file__).resolve().parents[2] / "web_ui" / "static" / "reader.css"


def _js_function(js, name):
    """The source of reader.js's top-level ``function <name>(``, to its closing brace."""
    start = js.index(f"function {name}(")
    return js[start:js.index("\n    }\n", start)]


def _save_handler(js):
    """The Save button's click handler, which is an arrow function with no name."""
    start = js.index("btnSave.addEventListener('click'")
    return js[start:js.index("\n    });", start)]


def _topbar_button(html, button_id):
    """The markup of the top-bar button with this id, up to its closing '>'."""
    start = html.index(f'id="{button_id}"')
    open_tag = html.rindex("<button", 0, start)
    return html[open_tag:html.index(">", start) + 1]


class TestTopbarMarkup:
    def test_nav_buttons_render_hidden(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        for button_id in ("btn-ann-nav", "btn-flag-nav", "btn-fn-nav"):
            assert f'id="{button_id}"' in html
            # JS unhides each button when its count is non-zero, so the server
            # must ship them hidden — otherwise empty buttons flash on load.
            assert "hidden" in _topbar_button(html, button_id)
        assert 'id="ann-nav-count"' in html
        assert 'id="flag-nav-count"' in html
        assert 'id="fn-nav-count"' in html

    def test_picker_renders_hidden(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        # The caret only means something once two tours have stops.
        assert "hidden" in _topbar_button(html, "btn-tour-pick")
        popup = html[html.index('id="tour-pick-popup"'):]
        assert "hidden" in popup[:popup.index(">")]

    def test_old_single_counter_is_gone(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert 'id="reader-stats"' not in html

    def test_buttons_are_labelled(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "Jump through annotations" in html
        assert "Jump through flags" in html
        assert "Jump through footnotes" in html
        assert "Choose what to jump through" in html

    def test_spanish_labels(self, client, project_with_alignment):
        client.set_cookie("reader_lang", "es")
        html = client.get(READ_URL).get_data(as_text=True)
        assert "Recorrer las anotaciones" in html
        assert "Recorrer los avisos" in html
        assert "Recorrer las notas al pie" in html
        assert "Elegir qué recorrer" in html

    def test_picker_rows_are_labelled_in_both_languages(self):
        # reader.js falls back to English when a key is missing, so a Spanish
        # picker with English rows would not show up as a broken page.
        for lang in ("en", "es"):
            for key in ("nav_pick_annotations", "nav_pick_flags", "nav_pick_footnotes"):
                assert STRINGS[lang]["js"].get(key), (lang, key)

    def test_css_cache_bust_present(self, client, project_with_alignment):
        html = client.get(READ_URL).get_data(as_text=True)
        assert "reader.css?v=" in html


class TestReaderJsSource:
    def test_js_drives_the_nav_buttons(self):
        js = READER_JS.read_text(encoding="utf-8")
        for element_id in ("btn-ann-nav", "ann-nav-count", "btn-flag-nav", "flag-nav-count",
                           "btn-fn-nav", "fn-nav-count", "btn-tour-pick", "tour-pick-popup"):
            assert element_id in js

    def test_flag_tour_walks_findings_and_save_warnings(self):
        body = _js_function(READER_JS.read_text(encoding="utf-8"), "collectFlags")
        assert "reviewMap" in body
        assert "saveWarnMap" in body

    def test_flag_changes_refresh_the_counters(self):
        # Until the flag tour, nothing a dismissal or a Save did to these two
        # maps reached a counter, so none of these paths called updateStats.
        js = READER_JS.read_text(encoding="utf-8")
        for name in ("setSaveWarn", "refreshSaveWarns", "dropFromReviewMap"):
            assert "updateStats()" in _js_function(js, name), name

    def test_active_tour_is_stored_per_book(self):
        js = READER_JS.read_text(encoding="utf-8")
        assert "'reader_tour:' + projectId" in js

    def test_active_tour_holds_while_it_has_stops(self):
        # Saving a note halfway through the flags gives the annotations tour
        # its first stop. The one button a phone shows must not change tours
        # under the reader, so the tour already showing is asked first.
        js = READER_JS.read_text(encoding="utf-8")
        body = _js_function(js, "updateTourPicker")
        assert "live.indexOf(activeTour) !== -1 ? activeTour" in body
        assert body.index("activeTour") < body.index("preferredTour")
        assert "activeTour = tour;" in _js_function(js, "pickTour")

    def test_only_a_pick_is_stored(self):
        js = READER_JS.read_text(encoding="utf-8")
        assert js.count("localStorage.setItem(TOUR_KEY") == 1
        assert "localStorage.setItem(TOUR_KEY" in _js_function(js, "pickTour")

    def test_picker_hidden_until_two_tours_have_stops(self):
        body = _js_function(READER_JS.read_text(encoding="utf-8"), "updateTourPicker")
        assert "btnTourPick.hidden = live.length < 2" in body

    def test_save_repaints_the_findings_it_leaves_standing(self):
        # A Save rewrites the sentence's text, which wipes its highlights, but
        # its findings stay in reviewMap and so stay on the flag tour. Both the
        # online branch and the offline one have to paint them back.
        online, offline = _save_handler(READER_JS.read_text(encoding="utf-8")).split(".catch(")
        assert "refreshSentenceReview(savedIdx)" in online
        assert "refreshSentenceReview(savedIdx)" in offline

    def test_phone_shows_only_the_active_tour(self):
        css = READER_CSS.read_text(encoding="utf-8")
        phone = css[css.index(".tour-group .topbar-count-btn:not(.tour-active)"):]
        assert "display: none" in phone[:phone.index("}")]
        # The rule has to sit under the phone breakpoint, or a wide screen
        # would lose its separate counters too.
        before = css[:css.index(".tour-group .topbar-count-btn:not(.tour-active)")]
        assert before.rstrip().endswith("@media (max-width: 480px) {")

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
