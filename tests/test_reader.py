"""Tests for reader mode routes."""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import app
from web_ui.evaluations import REVIEW_TYPES


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def project_with_alignment(tmp_path, monkeypatch):
    """Create a minimal project with alignment data."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    align_dir = proj_dir / "alignments"
    align_dir.mkdir(parents=True)
    # Create chunks dir so project appears in dashboard
    (proj_dir / "chunks").mkdir(parents=True, exist_ok=True)

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "en_count": 3,
        "es_count": 3,
        "high_confidence_pct": 100.0,
        "avg_similarity": 0.9,
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "es": "El gato.", "en": "The cat.", "similarity": 0.95, "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro.", "en": "The dog.", "similarity": 0.92, "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 2, "en_idx": 2, "es": "El pajaro.", "en": "The bird.", "similarity": 0.88, "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        ],
    }

    with open(align_dir / "chapter_01.json", "w", encoding="utf-8") as f:
        json.dump(alignment, f, ensure_ascii=False)

    # Monkey-patch _get_projects_dir to use tmp_path
    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)

    return proj_dir


def _write_annotations(proj_dir, records):
    with open(proj_dir / "annotations.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_reviewed(proj_dir, chapter_ids):
    (proj_dir / "reviewed.json").write_text(
        json.dumps({ch: "2026-06-27T00:00:00Z" for ch in chapter_ids}),
        encoding="utf-8",
    )


def _write_findings(proj_dir, chunk_id):
    """One coded finding and one judge finding on a chunk — two flags, two categories."""
    from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
    from web_ui.evaluations import merge_judge_result, save_chunk_evaluation

    save_chunk_evaluation(
        proj_dir, chunk_id, results=[], aggregated={},
        normalized_issues=[NormalizedIssue(
            eval_name="blacklist", eval_version="1.0.0", issue_index=0, severity="error",
            message="'negro': flagged term", suggestion="reconsider",
            location=NormalizedLocation(
                raw="char 3-8", side="target", char_start=3, char_end=8, match="gato",
            ),
        )],
    )
    merge_judge_result(proj_dir, chunk_id, "dialogue", {
        "eval_name": "dialogue", "eval_version": "1.0.0",
        "issues": [{"severity": "warning", "message": "use raya", "location": "El perro."}],
    })


def _write_config(proj_dir, **config):
    (proj_dir / "project.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )


class TestReaderProjectList:
    def test_projects_page_renders(self, client, project_with_alignment):
        rv = client.get("/read/")
        assert rv.status_code == 200
        assert b"test-project" in rv.data

    def test_no_projects(self, client, tmp_path, monkeypatch):
        import web_ui.app as app_module
        monkeypatch.setattr(app_module, "_get_projects_dir", lambda: tmp_path / "nonexistent")
        rv = client.get("/read/")
        assert rv.status_code == 200

    def test_card_shows_setup_gaps_not_the_old_status_rows(self, client, project_with_alignment):
        # The fixture project has neither a style guide nor a glossary and has
        # no chunks, so all three "something is missing" chips show and the
        # retired ✓/✗ status table is gone.
        html = client.get("/read/").data.decode("utf-8")
        assert "No style guide" in html
        assert "No glossary" in html
        assert "Not chunked" in html
        assert 'class="project-status"' not in html
        assert 'class="status-row"' not in html

    def test_card_hides_work_chips_before_translation(self, client, project_with_alignment):
        html = client.get("/read/").data.decode("utf-8")
        assert "project-work-chips" not in html
        assert "Nothing pending" not in html

    def test_category_picker_rendered_in_filter_bar(self, client, project_with_alignment):
        html = client.get("/read/").data.decode("utf-8")
        assert 'id="review-types-popup"' in html
        # One checkbox per review category. Counted from REVIEW_TYPES rather than
        # spelled out, so adding a judge does not fail a test about the picker.
        assert html.count('class="review-type-cb"') == len(REVIEW_TYPES)

    def test_status_picker_replaced_the_filter_buttons(self, client, project_with_alignment):
        html = client.get("/read/").data.decode("utf-8")
        assert 'id="status-filter-popup"' in html
        assert html.count('class="status-filter-cb"') == 4
        # The old one-button-per-status bar is gone.
        assert 'data-status="all"' not in html
        assert 'class="filter-btn active"' not in html

    def test_home_page_js_is_external(self, client, project_with_alignment):
        html = client.get("/read/").data.decode("utf-8")
        assert "reader_projects.js" in html
        assert "btn-create-project').addEventListener" not in html


class TestReaderChapterList:
    def test_chapters_page_renders(self, client, project_with_alignment):
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        assert b"chapter_01" in rv.data

    def test_project_not_found(self, client, project_with_alignment):
        rv = client.get("/read/nonexistent")
        assert rv.status_code == 404

    def test_flag_annotations_folded_into_review_count(self, client, project_with_alignment):
        # word_choice + inconsistency + flag all count toward the single "to
        # review" badge; the standalone flag badge is no longer rendered.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice"},
            {"chapter_id": "chapter_01", "es_idx": 1, "type": "inconsistency"},
            {"chapter_id": "chapter_01", "es_idx": 2, "type": "flag"},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "badge-flag" not in html
        assert "3 to review" in html

    def test_unknown_annotation_type_counts_as_to_review(self, client, project_with_alignment):
        # Legacy/typo'd types coerce to flag — same rule as the home-card rollup.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "typo_legacy"},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        assert "1 to review" in rv.data.decode("utf-8")

    def test_footnote_and_review_note_on_one_sentence_both_count(self, client, project_with_alignment):
        # Two annotations at the same es_idx are distinct records (distinct
        # sub_ids). Keying the badge counts on (chapter_id, es_idx) alone
        # collapsed them into one and let whichever was written last decide the
        # badge, so both badges undercounted.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "sub_id": "gb1"},
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "sub_id": "u1"},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "1 to review" in html
        assert "0/1 notes" in html

    def test_two_footnotes_on_one_sentence_count_as_two(self, client, project_with_alignment):
        # Badge counts are annotation *records*, not unique sentences — the same
        # contract the top-bar footnote tour displays (two stops still one stop).
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "sub_id": "fn1"},
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "sub_id": "fn2"},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "0/2 notes" in html

    def test_notes_badge_counts_written_notes_against_the_total(self, client, project_with_alignment):
        # A footnote mark carrying nothing but its [anchor] is dropped from the
        # built EPUB (src/endnotes.py), so the badge is written-of-total: 1/3
        # says two notes are still owed.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "sub_id": "a", "content": "Sancerre is a Loire white."},
            {"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote",
             "sub_id": "b", "content": "[Sancerre]"},
            {"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote",
             "sub_id": "c", "content": ""},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "1/3 notes" in html

    def test_all_notes_written_shows_a_full_fraction(self, client, project_with_alignment):
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote",
             "sub_id": "a", "content": "A gloss."},
            {"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote",
             "sub_id": "b", "content": "Another gloss."},
        ])
        _write_reviewed(project_with_alignment, ["chapter_01"])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "2/2 notes" in html
        # Written notes are a record, not outstanding work, so they do not block
        # the clean chip.
        assert "Nothing pending" in html

    def test_unparseable_annotation_line_does_not_break_chapter_list(self, client, project_with_alignment):
        # A half-written line must never 500 the whole chapter list.
        path = project_with_alignment / "annotations.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice"}) + "\n")
            f.write('{"chapter_id": "chapter_01", "es_idx"\n')
            f.write(json.dumps({"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote"}) + "\n")
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "1 to review" in html
        assert "0/1 notes" in html

    def test_reviewed_chapter_drops_the_unread_badge(self, client, project_with_alignment):
        # "unread" now means exactly "not marked reviewed", and the standalone
        # "reviewed" badge is gone — its absence is what says the chapter is read.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice"},
        ])
        _write_reviewed(project_with_alignment, ["chapter_01"])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "badge-unread" not in html
        assert ">reviewed<" not in html
        # Still an open annotation, so nothing is "pending"-free yet.
        assert "1 to review" in html
        assert "Nothing pending" not in html

    def test_unread_badge_shows_alongside_outstanding_work(self, client, project_with_alignment):
        # The old heuristic hid "unread" as soon as a chapter had any annotation.
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice"},
        ])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "badge-unread" in html
        assert "1 to review" in html

    def test_clean_chip_on_a_reviewed_chapter_with_nothing_outstanding(self, client, project_with_alignment):
        _write_reviewed(project_with_alignment, ["chapter_01"])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "Nothing pending" in html
        assert "badge-unread" not in html

    def test_no_clean_chip_while_a_note_is_unwritten(self, client, project_with_alignment):
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "content": ""},
        ])
        _write_reviewed(project_with_alignment, ["chapter_01"])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "0/1 notes" in html
        assert "Nothing pending" not in html

    def test_alignment_confidence_badge_is_gone(self, client, project_with_alignment):
        # Confidence is not actionable from the chapter list, so the badge went
        # away; coverage gaps, which are, kept theirs.
        align_file = project_with_alignment / "alignments" / "chapter_01.json"
        data = json.loads(align_file.read_text(encoding="utf-8"))
        data["high_confidence_pct"] = 42.0
        align_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "aligned" not in html

    def test_chapter_list_js_is_external(self, client, project_with_alignment):
        html = client.get("/read/test-project").data.decode("utf-8")
        assert "reader_chapters.js" in html
        assert "window.__reader_chapters" in html
        assert "reader_review:" not in html   # the localStorage key moved into the script

    def test_coverage_gap_badge_rendered(self, client, project_with_alignment):
        """A chapter with untranslated source runs must be badged.

        The alignment can be 100% high-confidence and still be missing whole
        paragraphs — every sentence that *was* translated matches well. Only the
        gap count exposes that, so it needs its own badge rather than riding on
        the confidence one.
        """
        align_file = project_with_alignment / "alignments" / "chapter_01.json"
        data = json.loads(align_file.read_text(encoding="utf-8"))
        data["coverage"] = {
            "en_count": 12, "en_aligned": 3, "gap_count": 1,
            "en_orphan_chars": 749, "max_gap_chars": 749,
        }
        align_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "1 gap" in html
        assert "749" in html

    def test_gap_blocks_the_clean_chip_on_a_reviewed_chapter(self, client, project_with_alignment):
        align_file = project_with_alignment / "alignments" / "chapter_01.json"
        data = json.loads(align_file.read_text(encoding="utf-8"))
        data["coverage"] = {"gap_count": 1, "en_orphan_chars": 749}
        align_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _write_reviewed(project_with_alignment, ["chapter_01"])

        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "1 gap" in html
        assert "Nothing pending" not in html

    def test_no_gap_badge_when_coverage_clean(self, client, project_with_alignment):
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        # Match the badge's title text — a bare "gap" also hits the CSS `gap:8px`.
        assert "characters of source text with no translation" not in html
        assert "badge-unread" in html

    def test_flags_badge_counts_findings_in_the_chapter(self, client, project_with_alignment):
        # Evaluator and judge findings are bucketed per chapter and shipped with
        # the row as data-flags, so reader_chapters.js can re-sum them when the
        # category picker changes without a refetch.
        _write_findings(project_with_alignment, "chapter_01_chunk_000")
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert '<span class="chip-flag-count">2</span> <span class="chip-flag-label">flags</span>' in html
        assert '"blacklist": 1' in html and '"dialogue": 1' in html

    def test_flags_badge_respects_the_category_cookie(self, client, project_with_alignment):
        _write_findings(project_with_alignment, "chapter_01_chunk_000")
        client.post("/api/set-review-types", json={"types": ["dialogue"]})
        html = client.get("/read/test-project").data.decode("utf-8")
        assert '<span class="chip-flag-count">1</span> <span class="chip-flag-label">flag</span>' in html
        # Still shipped in full so unticking the box re-sums without a reload.
        assert '"blacklist": 1' in html

    def test_flags_badge_hidden_when_the_chapter_is_clean(self, client, project_with_alignment):
        html = client.get("/read/test-project").data.decode("utf-8")
        assert "chapter-chip-flags" in html   # present but hidden, for the JS
        assert '<span class="chip-flag-count">0</span>' in html
        assert "chapter-chip-flags\" hidden" in html

    def test_flags_block_the_clean_chip(self, client, project_with_alignment):
        _write_findings(project_with_alignment, "chapter_01_chunk_000")
        _write_reviewed(project_with_alignment, ["chapter_01"])
        html = client.get("/read/test-project").data.decode("utf-8")
        # Server-rendered hidden, and reader_chapters.js flips it back if the
        # user unticks every category the findings fall into.
        assert 'chapter-chip-clean" hidden' in html

    def test_findings_in_another_chapter_do_not_leak(self, client, project_with_alignment):
        _write_findings(project_with_alignment, "chapter_02_chunk_000")
        html = client.get("/read/test-project").data.decode("utf-8")
        assert '<span class="chip-flag-count">0</span>' in html

    def test_spanish_title_shown_when_lang_es(self, client, project_with_alignment):
        # With the UI language set to Spanish, the chapters header uses the
        # project's spanish_title rather than the English title.
        _write_config(project_with_alignment, title="English Title", spanish_title="Titulo Espanol")
        client.set_cookie("reader_lang", "es")
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "Titulo Espanol" in html
        assert "<h1" in html and "English Title" not in html


class TestReaderView:
    def test_reader_view_renders(self, client, project_with_alignment):
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        assert b"reader-app" in rv.data

    def test_reader_ships_stale_chunk_toast_i18n(self, client, project_with_alignment):
        html = client.get("/read/test-project/chapter_01").data.decode("utf-8")
        assert "review_stale_chunks" in html
        assert "Some judge results are hidden:" in html
        assert "{n}" in html

    def test_reader_ships_stale_chunk_toast_i18n_es(self, client, project_with_alignment):
        client.set_cookie("reader_lang", "es")
        html = client.get("/read/test-project/chapter_01").data.decode("utf-8")
        assert "review_stale_chunks" in html
        assert "Algunos resultados de los jueces" in html
        assert "{n}" in html

    def test_chapter_not_found(self, client, project_with_alignment):
        rv = client.get("/read/test-project/chapter_99")
        assert rv.status_code == 404

    @staticmethod
    def _btn_align_open_tag(html: str) -> str:
        """Return just the opening <button ...> tag for #btn-align, no inner SVG."""
        start = html.index('id="btn-align"')
        tag_start = html.rfind("<button", 0, start)
        tag_end = html.index(">", start)
        return html[tag_start : tag_end + 1]

    def test_realign_button_hidden_when_no_pending_corrections(
        self, client, project_with_alignment,
    ):
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        # Button is in the DOM but the `hidden` attribute is present so the JS
        # can reveal it after a save without re-rendering the template.
        tag = self._btn_align_open_tag(rv.data.decode("utf-8"))
        assert "hidden" in tag

    def test_realign_button_visible_when_pending_corrections(
        self, client, project_with_alignment,
    ):
        corr = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": "El gato.",
            "corrected_es": "El gato pequeño.",
            "en_reference": "The cat.",
            "timestamp": "2026-05-20T12:00:00",
        }
        (project_with_alignment / "corrections.jsonl").write_text(
            json.dumps(corr, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        tag = self._btn_align_open_tag(rv.data.decode("utf-8"))
        assert "hidden" not in tag

    def test_realign_button_hidden_when_corrections_target_other_chapter(
        self, client, project_with_alignment,
    ):
        corr = {
            "project_id": "test-project",
            "chapter_id": "chapter_99",
            "chunk_id": "chapter_99_chunk_000",
            "es_idx": 0,
            "original_es": "x",
            "corrected_es": "y",
            "en_reference": "x",
            "timestamp": "2026-05-20T12:00:00",
        }
        (project_with_alignment / "corrections.jsonl").write_text(
            json.dumps(corr, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        tag = self._btn_align_open_tag(rv.data.decode("utf-8"))
        assert "hidden" in tag

    def test_realign_button_visible_despite_malformed_json_line(
        self, client, project_with_alignment,
    ):
        """A malformed line in corrections.jsonl is skipped; valid rows still
        show the button."""
        corr = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": "El gato.",
            "corrected_es": "El gato pequeño.",
            "en_reference": "The cat.",
            "timestamp": "2026-05-20T12:00:00",
        }
        (project_with_alignment / "corrections.jsonl").write_text(
            "NOT_JSON\n" + json.dumps(corr, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        tag = self._btn_align_open_tag(rv.data.decode("utf-8"))
        assert "hidden" not in tag


class TestAlignmentAPI:
    def test_get_alignment(self, client, project_with_alignment):
        rv = client.get("/api/alignment/test-project/chapter_01")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["es_count"] == 3
        assert len(data["alignments"]) == 3

    def test_alignment_not_found(self, client, project_with_alignment):
        rv = client.get("/api/alignment/test-project/chapter_99")
        assert rv.status_code == 404


class TestCorrectionAPI:
    def test_save_correction(self, client, project_with_alignment):
        rv = client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "original_es": "El gato.",
            "corrected_es": "El gatito.",
            "en_reference": "The cat.",
        })
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["saved"] is True

        # Verify alignment was patched
        rv2 = client.get("/api/alignment/test-project/chapter_01")
        data2 = rv2.get_json()
        assert data2["alignments"][0]["es"] == "El gatito."
        assert data2["alignments"][0]["corrected"] is True

    def test_correction_appends_to_jsonl(self, client, project_with_alignment):
        client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "original_es": "El perro.",
            "corrected_es": "El perrito.",
            "en_reference": "The dog.",
        })

        corrections_path = project_with_alignment / "corrections.jsonl"
        assert corrections_path.exists()
        lines = corrections_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["corrected_es"] == "El perrito."
        assert record["es_idx"] == 1

    def test_correction_missing_fields(self, client, project_with_alignment):
        rv = client.post("/api/correction", json={
            "project_id": "test-project",
            # missing chapter_id, es_idx, etc.
        })
        assert rv.status_code == 400

    def test_correction_invalid_project(self, client, project_with_alignment):
        rv = client.post("/api/correction", json={
            "project_id": "nonexistent",
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "original_es": "foo",
            "corrected_es": "bar",
        })
        assert rv.status_code == 404

    def test_correction_out_of_range_es_idx(self, client, project_with_alignment):
        """es_idx that doesn't exist in alignment — correction still saves but no patch."""
        rv = client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 999,
            "original_es": "foo",
            "corrected_es": "bar",
        })
        assert rv.status_code == 200  # Still saves to JSONL
