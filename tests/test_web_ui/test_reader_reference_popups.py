"""Tests for the reader's reference documents (⋮ menu) and the dashboard map.

The style guide, glossary and address map are what a line was translated under,
and none of them used to reach the reader. `GET /api/project/<id>/reference/<kind>`
serves all three to the bottom sheet, led by the part bearing on the tapped
sentence, and the address map also gets its first read-only dashboard surface.

Conventions follow ``test_reader_ui_version.py``: Flask test client, a temp
projects dir via ``_get_projects_dir``, HTML-marker assertions for markup, and
source-reading assertions for behaviour that lives in JS (there is no JS runner
here).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


STATIC = Path(__file__).resolve().parents[2] / "web_ui" / "static"

EARLY = "2026-01-01T00:00:00"
JUDGE_RUN = "2026-02-01T00:00:00"
LATE = "2026-03-01T00:00:00"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one aligned chapter naming two address-map characters."""
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "test-project"
    (proj / "alignments").mkdir(parents=True)
    (proj / "chunks").mkdir(parents=True)
    (proj / "evaluations").mkdir(parents=True)

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000",
             "en": "Bambi asked his mother about the meadow.",
             "es": "Bambi le preguntó a la madre de Bambi por el prado."},
            {"es_idx": 1, "en_idx": 1, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000",
             "en": "The rain fell.", "es": "Llovía."},
        ],
    }
    (proj / "alignments" / "chapter_01.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8")

    _write_style_guide(proj)
    _write_glossary(proj)
    _write_address_map(proj)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj


def _write_style_guide(proj: Path, *, light: str | None = "Mexican Spanish, warm.",
                       updated: str = EARLY) -> None:
    (proj / "style.json").write_text(json.dumps({
        "content": "DIALECT AND REGISTER\nStandard educated Mexican Spanish.",
        "light_content": light,
        "version": "1.0",
        "created_at": EARLY,
        "updated_at": updated,
    }, ensure_ascii=False), encoding="utf-8")


def _write_glossary(proj: Path, updated: str = EARLY) -> None:
    (proj / "glossary.json").write_text(json.dumps({
        "terms": [
            {"english": "meadow", "spanish": "el prado", "type": "place",
             "context": "Open grass where the deer graze.", "alternatives": ["la pradera"]},
            {"english": "thicket", "spanish": "la espesura", "type": "place",
             "context": "", "alternatives": []},
        ],
        "version": "1.0",
        "updated_at": updated,
    }, ensure_ascii=False), encoding="utf-8")


def _write_address_map(proj: Path, updated: str = EARLY) -> None:
    (proj / "address_map.json").write_text(json.dumps({
        "content": "FORMS OF ADDRESS\nThe forest is stratified.",
        "style_guide_summary": "Tú within the family.",
        "global_rules": "Tú between family; usted to elders.",
        "pairs": [
            {"a": "Bambi", "b": "la madre de Bambi",
             "relationship": "fawn and his mother",
             "directions": {
                 "a_to_b": [{"form": "tú", "when": "default", "notes": "Always."}],
                 "b_to_a": [{"form": "tú", "when": "default"}],
             }},
            {"a": "Bambi", "b": "el viejo Príncipe",
             "relationship": "fawn and his mentor",
             "directions": {
                 "a_to_b": [{"form": "usted", "when": "default"}],
                 "b_to_a": [{"form": "tú", "when": "default"}],
             }},
            {"a": "Faline", "b": "Gobo", "relationship": "siblings",
             "directions": {"a_to_b": [{"form": "tú", "when": "default"}],
                            "b_to_a": [{"form": "tú", "when": "default"}]}},
        ],
        "version": "1.0",
        "created_at": EARLY,
        "updated_at": updated,
    }, ensure_ascii=False), encoding="utf-8")


def _write_evaluation(proj: Path, judge: str, ran_at: str = JUDGE_RUN) -> None:
    (proj / "evaluations" / "chapter_01_chunk_000.json").write_text(json.dumps({
        "chunk_id": "chapter_01_chunk_000",
        "evaluated_at": ran_at,
        "judges_at": ran_at,
        "judges": {judge: {}},
        "eval_runs": {judge: {"at": ran_at, "text_sha": "abc"}},
    }), encoding="utf-8")


def _ref(client, kind, *, project_id="test-project", chapter="chapter_01", es_idx=0):
    url = f"/api/project/{project_id}/reference/{kind}"
    if chapter is not None:
        url += f"?chapter={chapter}&es_idx={es_idx}"
    return client.get(url)


class TestGuards:
    def test_unknown_kind_is_rejected(self, client, project):
        assert _ref(client, "secrets").status_code == 400

    def test_bad_project_id_is_rejected(self, client, project):
        assert client.get("/api/project/.../reference/glossary").status_code == 400

    def test_missing_project_is_404(self, client, project):
        assert _ref(client, "glossary", project_id="nope").status_code == 404

    def test_bad_chapter_is_rejected(self, client, project):
        rv = client.get("/api/project/test-project/reference/glossary?chapter=..&es_idx=0")
        assert rv.status_code == 400

    def test_dotted_project_id_resolves(self, client, project, tmp_path):
        # Real dirs carry periods (``foo.bak-ch1-restore``); _safe_id allows them
        # and this route must not be the one place that forgets.
        dotted = project.parent / "test-project.bak"
        dotted.mkdir()
        (dotted / "glossary.json").write_text(
            json.dumps({"terms": [], "version": "1.0", "updated_at": EARLY}),
            encoding="utf-8")
        rv = _ref(client, "glossary", project_id="test-project.bak", chapter=None)
        assert rv.status_code == 200


class TestGlossaryRelevance:
    def test_relevant_narrows_to_the_sentence(self, client, project):
        d = _ref(client, "glossary", es_idx=0).get_json()
        assert [t["english"] for t in d["relevant"]] == ["meadow"]
        assert len(d["all"]) == 2

    def test_sentence_with_no_terms_has_no_relevant(self, client, project):
        d = _ref(client, "glossary", es_idx=1).get_json()
        assert d["relevant"] == []
        assert len(d["all"]) == 2

    def test_context_and_alternatives_survive(self, client, project):
        term = _ref(client, "glossary", es_idx=0).get_json()["relevant"][0]
        assert term["spanish"] == "el prado"
        assert term["context"].startswith("Open grass")
        assert term["alternatives"] == ["la pradera"]

    def test_without_a_sentence_nothing_is_relevant(self, client, project):
        d = _ref(client, "glossary", chapter=None).get_json()
        assert d["relevant"] == []
        assert len(d["all"]) == 2


class TestAddressRelevance:
    """A pair governs who addresses whom, so both parties must be present.

    Matching a lone name made the filter useless on real books: the protagonist
    is half of nearly every pair, so "Bambi" alone selected 9 of 11.
    """

    def test_both_parties_present_matches(self, client, project):
        d = _ref(client, "address-map", es_idx=0).get_json()
        assert [(p["a"], p["b"]) for p in d["relevant"]] == [("Bambi", "la madre de Bambi")]

    def test_hub_name_alone_does_not_flood(self, client, project):
        # "Bambi" is in 2 of 3 pairs -- a hub, so it cannot carry a match alone.
        d = _ref(client, "address-map", es_idx=1).get_json()
        assert d["relevant"] == []
        assert len(d["all"]) == 3

    def test_global_rules_always_ship(self, client, project):
        # They are the judge's own fallback, so they matter most when nothing
        # matched -- the popup shows them regardless.
        for es_idx in (0, 1):
            d = _ref(client, "address-map", es_idx=es_idx).get_json()
            assert d["global_rules"].startswith("Tú between family")

    def test_directions_keep_their_rules(self, client, project):
        pair = _ref(client, "address-map", es_idx=0).get_json()["relevant"][0]
        assert pair["directions"]["a_to_b"][0]["form"] == "tú"
        assert pair["directions"]["a_to_b"][0]["notes"] == "Always."
        assert pair["relationship"] == "fawn and his mother"


class TestStyleGuide:
    def test_light_guide_is_the_summary_view(self, client, project):
        d = _ref(client, "style-guide").get_json()
        assert d["light"] == "Mexican Spanish, warm."
        assert "DIALECT AND REGISTER" in d["content"]

    def test_absent_light_guide_still_returns_content(self, client, project):
        _write_style_guide(project, light=None)
        d = _ref(client, "style-guide").get_json()
        assert d["light"] == ""
        assert "DIALECT AND REGISTER" in d["content"]


class TestMissingAndUnreadable:
    def test_absent_address_map_names_the_command(self, client, project):
        (project / "address_map.json").unlink()
        d = _ref(client, "address-map").get_json()
        assert d["exists"] is False
        assert "harness.py address-map prepare" in d["empty_command"]
        assert d["stale"] is None

    def test_absent_glossary_is_not_an_error(self, client, project):
        (project / "glossary.json").unlink()
        rv = _ref(client, "glossary")
        assert rv.status_code == 200
        assert rv.get_json()["exists"] is False

    def test_malformed_file_degrades_rather_than_500(self, client, project):
        (project / "address_map.json").write_text("{not json", encoding="utf-8")
        rv = _ref(client, "address-map")
        assert rv.status_code == 200
        assert rv.get_json()["exists"] is False
        assert rv.get_json()["unreadable"] is True


class TestStaleness:
    """Document-vs-run, which is NOT the chunk-text-vs-run question that
    ``evaluator_freshness`` answers."""

    def test_document_edited_after_the_judge_warns(self, client, project):
        _write_evaluation(project, "address")
        _write_address_map(project, updated=LATE)
        stale = _ref(client, "address-map").get_json()["stale"]
        assert stale["stale"] is True
        assert stale["judge"] == "address"
        assert stale["ran_at"] == JUDGE_RUN

    def test_document_older_than_the_judge_does_not_warn(self, client, project):
        _write_evaluation(project, "address")
        _write_address_map(project, updated=EARLY)
        assert _ref(client, "address-map").get_json()["stale"]["stale"] is False

    def test_no_recorded_run_never_warns(self, client, project):
        # Absence of a run is not evidence of staleness; warning here would put
        # a banner on every chapter nobody has judged yet.
        _write_address_map(project, updated=LATE)
        assert _ref(client, "address-map").get_json()["stale"] is None

    def test_each_document_reads_its_own_judge(self, client, project):
        # An address run says nothing about whether the glossary is stale.
        _write_evaluation(project, "address")
        _write_glossary(project, updated=LATE)
        assert _ref(client, "glossary").get_json()["stale"] is None

    def test_legacy_projects_fall_back_to_judges_at(self, client, project):
        # Evaluated before the per-evaluator eval_runs ledger existed.
        (project / "evaluations" / "chapter_01_chunk_000.json").write_text(json.dumps({
            "chunk_id": "chapter_01_chunk_000",
            "evaluated_at": JUDGE_RUN,
            "judges_at": JUDGE_RUN,
            "judges": {"address": {}},
        }), encoding="utf-8")
        _write_address_map(project, updated=LATE)
        assert _ref(client, "address-map").get_json()["stale"]["stale"] is True

    def test_without_a_chapter_staleness_is_unanswerable(self, client, project):
        _write_evaluation(project, "address")
        _write_address_map(project, updated=LATE)
        assert _ref(client, "address-map", chapter=None).get_json()["stale"] is None


class TestReaderMarkup:
    def test_menu_and_dialog_render_in_v2(self, client, project):
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        for marker in ('id="rv2-refmenu-btn"', 'id="rv2-refmenu-popup"',
                       'data-ref="style-guide"', 'data-ref="address-map"',
                       'data-ref="glossary"', 'id="rv2-doc"'):
            assert marker in html

    def test_dialog_is_a_real_dialog(self, client, project):
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        assert 'role="dialog"' in html
        assert 'aria-modal="true"' in html
        assert 'aria-haspopup="true"' in html

    def test_absent_from_classic(self, client, project):
        # Scoped to v2 deliberately: classic is no longer offered in the UI.
        html = client.get("/read/test-project/chapter_01?ui=classic").get_data(as_text=True)
        assert "rv2-refmenu" not in html
        assert 'id="rv2-doc"' not in html

    def test_assets_are_cache_busted_past_the_old_build(self, client, project):
        # The ?v= is hand-maintained; a stale one silently serves the old sheet.
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        assert "reader_sheet_v2.js?v=11" not in html
        assert "reader_sheet_v2.css?v=19" not in html

    def test_spanish_labels(self, client, project):
        client.set_cookie("reader_lang", "es")
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        assert "Formas de tratamiento" in html
        assert "Glosario" in html


class TestSheetSource:
    """Behaviour that lives in JS, asserted by reading the source."""

    def test_document_text_is_not_interpolated_into_html(self):
        js = (STATIC / "reader_sheet_v2.js").read_text(encoding="utf-8")
        body = js.split("Reference documents (⋮ menu)")[1].split("── State ──")[0]
        # Comments stripped first: this region's prose explains *why* it avoids
        # innerHTML, and matching that would fail on the explanation itself.
        code = "\n".join(
            line for line in body.splitlines()
            if not line.lstrip().startswith("//")
        )
        # These documents are operator-authored free text and this sheet has
        # already shipped one injection of this class; nodes only.
        assert "innerHTML" not in code
        assert "function node(" in js

    def test_viewer_restores_focus_and_traps_tab(self):
        js = (STATIC / "reader_sheet_v2.js").read_text(encoding="utf-8")
        assert "trapDocFocus" in js
        assert "docReturnFocus" in js

    def test_address_map_links_to_the_style_guide_stage(self):
        # The map's read-only block lives under the light style guide.
        js = (STATIC / "reader_sheet_v2.js").read_text(encoding="utf-8")
        assert "'address-map': 'style-guide'" in js


class TestHiddenIsHonoured:
    """Anything toggled via the `hidden` property needs a `[hidden]` rule.

    The attribute is only `display: none` from the UA stylesheet, so any author
    `display` beats it. Getting this wrong left the viewer on screen from page
    load and made its close button inert -- the same trap `.rv2-sheet[hidden]`
    and two rules in reader.css already exist to avoid.
    """

    TOGGLED = ["rv2-doc", "rv2-doc-overlay", "rv2-refmenu-popup", "rv2-doc-stale"]

    def test_every_toggled_element_that_sets_display_also_unsets_it(self):
        css = (STATIC / "reader_sheet_v2.css").read_text(encoding="utf-8")
        for name in self.TOGGLED:
            block = css.split("." + name + " {")
            if len(block) < 2:
                continue
            declarations = block[1].split("}")[0]
            if "display:" in declarations:
                assert f".{name}[hidden]" in css, (
                    f".{name} sets display, so it needs a [hidden] rule"
                )

    def test_the_viewer_and_its_overlay_are_explicitly_hideable(self):
        css = (STATIC / "reader_sheet_v2.css").read_text(encoding="utf-8")
        assert ".rv2-doc[hidden] { display: none; }" in css
        assert ".rv2-doc-overlay[hidden] { display: none; }" in css

    def test_css_cache_bust_moved_past_the_broken_build(self, client, project):
        # v=20 shipped the viewer that would not hide, v=21 the one that was
        # transparent; a stale ?v= serves those from cache and the fixes look
        # like they never landed.
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        for dead in ("v=19", "v=20", "v=21"):
            assert f"reader_sheet_v2.css?{dead}" not in html


class TestViewerPalette:
    """The viewer sits OUTSIDE #reader-sheet-v2, where --rv2-* is scoped.

    A `var(--rv2-*)` used outside that scope resolves to nothing, so the panel
    came up with a transparent background and the reader showed through it.
    """

    def test_tokens_are_scoped_to_the_viewer_too(self):
        css = (STATIC / "reader_sheet_v2.css").read_text(encoding="utf-8")
        # The selector immediately preceding each palette must name .rv2-doc.
        light = css.split("--rv2-bg: #faf8f5;")[0]
        assert light.rstrip().endswith("{")
        assert ".rv2-doc," in light or ".rv2-doc {" in light
        dark = css.split("--rv2-bg: #1a1a1a;")[0]
        assert ':root[data-theme="dark"] .rv2-doc' in dark

    def test_surface_colours_carry_literal_fallbacks(self):
        # Belt-and-braces, the way .rv2-bubble does it: never invisible again.
        # Anchored on a declaration unique to the rule block -- splitting on
        # ".rv2-doc {" alone lands in the token block, which opens the same way.
        css = (STATIC / "reader_sheet_v2.css").read_text(encoding="utf-8")
        block = css.split("max-height: 88dvh;")[1].split("}")[0]
        assert "var(--rv2-sheet-bg, #fff)" in block
        assert "var(--rv2-text, #2b2b2b)" in block


class TestDashboardAddressMap:
    def test_status_carries_the_map(self, client, project):
        s = client.get("/api/project/test-project/status").get_json()
        assert s["has_address_map"] is True
        assert len(s["address_map"]["pairs"]) == 3
        assert s["address_map"]["global_rules"].startswith("Tú between family")

    def test_status_defaults_when_absent(self, client, project):
        (project / "address_map.json").unlink()
        s = client.get("/api/project/test-project/status").get_json()
        assert s["has_address_map"] is False
        assert s["address_map"] is None

    def test_stage_renders_the_container(self, client, project):
        html = client.get("/project/test-project").get_data(as_text=True)
        assert 'id="address-map-view"' in html
        assert "Forms of Address" in html

    def test_renderer_builds_nodes(self):
        js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
        assert "function renderAddressMap(" in js
        body = js.split("function renderAddressMap(")[1].split("\n    }")[0]
        assert "innerHTML" not in body
