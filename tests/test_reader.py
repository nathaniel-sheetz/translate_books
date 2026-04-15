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


class TestReaderView:
    def test_reader_view_renders(self, client, project_with_alignment):
        rv = client.get("/read/test-project/chapter_01")
        assert rv.status_code == 200
        assert b"reader-app" in rv.data

    def test_chapter_not_found(self, client, project_with_alignment):
        rv = client.get("/read/test-project/chapter_99")
        assert rv.status_code == 404


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
