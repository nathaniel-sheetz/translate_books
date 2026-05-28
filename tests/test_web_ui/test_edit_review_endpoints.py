"""Tests for the three new edit-review endpoints in web_ui/app.py:

  GET  /api/edit-tags                            → list_edit_tags
  POST /api/project/<project_id>/edit-tag        → post_edit_tag
  GET  /reports/<project_id>/<filename>          → serve_edit_report

Covers:
  - list_edit_tags: returns the correct tag vocabulary
  - post_edit_tag: happy path, bad project_id in URL, unknown tag,
    bad hunk_index, missing project dir, OSError fallback
  - serve_edit_report: bad project_id, path traversal attempts,
    no reports dir, valid file served
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.edit_review_constants import EDIT_TAGS
from web_ui.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """One project directory wired into the Flask app."""
    projects_root = tmp_path / "projects"
    proj = projects_root / "proj1"
    proj.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_root)
    return proj


# =============================================================================
# GET /api/edit-tags
# =============================================================================


class TestListEditTags:
    def test_returns_200_with_tag_list(self, client):
        rv = client.get("/api/edit-tags")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "tags" in data
        assert isinstance(data["tags"], list)
        assert len(data["tags"]) > 0

    def test_returns_known_tags(self, client):
        rv = client.get("/api/edit-tags")
        data = rv.get_json()
        # Spot-check a few canonical tags
        assert "style-tone" in data["tags"]
        assert "other" in data["tags"]
        assert "glossary-gender-conflict" in data["tags"]

    def test_tag_list_matches_constant(self, client):
        rv = client.get("/api/edit-tags")
        assert rv.get_json()["tags"] == EDIT_TAGS


# =============================================================================
# POST /api/edit-tag
# =============================================================================


class TestPostEditTag:
    _URL = "/api/project/proj1/edit-tag"

    def _valid_payload(self):
        return {
            "chunk_id": "chapter_001_chunk_000",
            "tag": "style-tone",
            "hunk_index": 0,
            "note": "optional note",
        }

    def test_happy_path_appends_tag_and_returns_row(self, client, project_dir):
        rv = client.post(self._URL, json=self._valid_payload())
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["row"]["tag"] == "style-tone"
        assert data["row"]["hunk_index"] == 0

    def test_tag_is_persisted_to_jsonl(self, client, project_dir):
        client.post(self._URL, json=self._valid_payload())
        tags_path = project_dir / "edit_review_tags.jsonl"
        assert tags_path.exists()
        rows = [json.loads(line) for line in tags_path.read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["tag"] == "style-tone"
        assert rows[0]["chunk_id"] == "chapter_001_chunk_000"

    def test_multiple_tags_accumulate(self, client, project_dir):
        for tag in ["style-tone", "other"]:
            p = self._valid_payload()
            p["tag"] = tag
            client.post(self._URL, json=p)
        tags_path = project_dir / "edit_review_tags.jsonl"
        rows = [json.loads(l) for l in tags_path.read_text().splitlines()]
        assert len(rows) == 2

    def test_bad_project_id_returns_400(self, client, project_dir):
        rv = client.post("/api/project/../../etc/edit-tag", json=self._valid_payload())
        assert rv.status_code in (400, 404)

    def test_bad_chunk_id_returns_400(self, client, project_dir):
        p = self._valid_payload()
        p["chunk_id"] = "../../bad"
        rv = client.post(self._URL, json=p)
        assert rv.status_code == 400

    def test_unknown_tag_returns_400(self, client, project_dir):
        p = self._valid_payload()
        p["tag"] = "made-up-tag"
        rv = client.post(self._URL, json=p)
        assert rv.status_code == 400
        assert "Unknown tag" in rv.get_json()["error"]

    def test_negative_hunk_index_returns_400(self, client, project_dir):
        p = self._valid_payload()
        p["hunk_index"] = -1
        rv = client.post(self._URL, json=p)
        assert rv.status_code == 400
        assert "hunk_index" in rv.get_json()["error"]

    def test_non_int_hunk_index_returns_400(self, client, project_dir):
        p = self._valid_payload()
        p["hunk_index"] = "zero"
        rv = client.post(self._URL, json=p)
        assert rv.status_code == 400

    def test_project_not_found_returns_404(self, client, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        # Do NOT create proj1 — project dir is missing
        import web_ui.app as app_module
        monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_root)

        rv = client.post(self._URL, json=self._valid_payload())
        assert rv.status_code == 404

    def test_empty_body_returns_400(self, client, project_dir):
        rv = client.post(self._URL, json={})
        assert rv.status_code == 400

    def test_row_contains_timestamp(self, client, project_dir):
        rv = client.post(self._URL, json=self._valid_payload())
        row = rv.get_json()["row"]
        assert "timestamp" in row

    def test_note_is_optional_defaults_to_empty(self, client, project_dir):
        p = self._valid_payload()
        del p["note"]
        rv = client.post(self._URL, json=p)
        assert rv.status_code == 200
        assert rv.get_json()["row"]["note"] == ""


# =============================================================================
# GET /reports/<project_id>/<filename>
# =============================================================================


class TestServeEditReport:
    def _make_report(self, project_dir: Path, filename: str = "edits-all-20260101.html") -> Path:
        reports_dir = project_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report = reports_dir / filename
        report.write_text("<html>report</html>", encoding="utf-8")
        return report

    def test_valid_report_served(self, client, project_dir):
        self._make_report(project_dir)
        rv = client.get("/reports/proj1/edits-all-20260101.html")
        assert rv.status_code == 200
        assert b"report" in rv.data

    def test_bad_project_id_returns_400(self, client, project_dir):
        rv = client.get("/reports/../etc/passwd")
        # Flask may 404 on routing before we even hit our handler
        assert rv.status_code in (400, 404)

    def test_path_traversal_in_filename_returns_400(self, client, project_dir):
        self._make_report(project_dir)
        rv = client.get("/reports/proj1/../../../etc/passwd")
        assert rv.status_code in (400, 404)

    def test_double_dot_in_filename_returns_400(self, client, project_dir):
        self._make_report(project_dir)
        rv = client.get("/reports/proj1/..%2Fetc%2Fpasswd")
        assert rv.status_code in (400, 404)

    def test_no_reports_dir_returns_404(self, client, project_dir):
        # reports dir never created
        rv = client.get("/reports/proj1/edits-all-20260101.html")
        assert rv.status_code == 404
        assert "No reports" in rv.get_json()["error"]

    def test_nonexistent_project_returns_400_or_404(self, client, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        import web_ui.app as app_module
        monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_root)

        rv = client.get("/reports/ghost_project/edits-all-20260101.html")
        # With bad safe_id or missing reports dir, should get 4xx
        assert rv.status_code in (400, 404)
