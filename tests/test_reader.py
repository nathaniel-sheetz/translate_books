"""Tests for reader mode routes."""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import app


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

    def test_reviewed_chapter_always_shows_reviewed_badge(self, client, project_with_alignment):
        # A reviewed chapter shows the "reviewed" badge even with outstanding
        # annotations (previously gated on total_ann == 0).
        _write_annotations(project_with_alignment, [
            {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice"},
        ])
        _write_reviewed(project_with_alignment, ["chapter_01"])
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        assert "badge-clean" in html
        assert ">reviewed<" in html
        assert "badge-unread" not in html

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
        # high_confidence_pct is 100 here, so the confidence badge stays away —
        # proving the gap badge is what surfaces the omission.
        assert "% aligned" not in html
        # A chapter with missing prose is not "unread".
        assert "badge-unread" not in html

    def test_no_gap_badge_when_coverage_clean(self, client, project_with_alignment):
        rv = client.get("/read/test-project")
        assert rv.status_code == 200
        html = rv.data.decode("utf-8")
        # Match the badge's title text — a bare "gap" also hits the CSS `gap:8px`.
        assert "characters of source text with no translation" not in html
        assert "badge-unread" in html

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
