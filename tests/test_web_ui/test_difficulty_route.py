"""Tests for GET /api/project/<project_id>/difficulty.

Covers:
  - 400 for a bad (path-traversal) project id
  - 404 for a non-existent project
  - 200 happy-path with book + chapters payload
  - ?force=1 forces re-scoring
  - 500 when score_book raises unexpectedly

Conventions follow test_chunking_config_persistence.py — Flask test client +
monkeypatch on _get_projects_dir.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Minimal project dir with one chapter."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "mybook"
    (proj_dir / "chapters").mkdir(parents=True)
    (proj_dir / "chapters" / "chapter_01.txt").write_text(
        "The quick brown fox jumped over the lazy dog near the wide river. "
        "She walked slowly down the road.",
        encoding="utf-8",
    )

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


# ===========================================================================
# Tests
# ===========================================================================


class TestDifficultyRouteValidation:
    def test_bad_project_id_returns_400(self, client, project):
        # "..." contains ".." which _safe_id rejects; Flask routes it through
        # unlike "../etc" which gets cleaned before reaching the route.
        rv = client.get("/api/project/.../difficulty")
        assert rv.status_code == 400
        data = rv.get_json()
        assert "error" in data

    def test_project_id_with_colon_returns_400(self, client, project):
        # Colon allows Windows drive-relative path escape (C:foo).
        rv = client.get("/api/project/C:foo/difficulty")
        assert rv.status_code == 400

    def test_project_id_with_dot_returns_400(self, client, project):
        rv = client.get("/api/project/a.b/difficulty")
        assert rv.status_code == 400

    def test_missing_project_returns_404(self, client, project):
        rv = client.get("/api/project/does-not-exist/difficulty")
        assert rv.status_code == 404
        data = rv.get_json()
        assert "error" in data


class TestDifficultyRouteHappyPath:
    def test_returns_ok_with_book_and_chapters(self, client, project):
        rv = client.get(f"/api/project/{project.name}/difficulty")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data.get("ok") is True
        assert "book" in data
        assert "chapters" in data

    def test_book_metrics_fields_present(self, client, project):
        rv = client.get(f"/api/project/{project.name}/difficulty")
        book = rv.get_json()["book"]
        for key in ("difficulty", "length_score", "rarity_score", "suggested_target_size"):
            assert key in book, f"missing field: {key}"

    def test_chapters_list_contains_chapter_id(self, client, project):
        rv = client.get(f"/api/project/{project.name}/difficulty")
        chapters = rv.get_json()["chapters"]
        assert len(chapters) == 1
        assert chapters[0]["chapter_id"] == "chapter_01"

    def test_force_param_rescores(self, client, project):
        # First call populates cache.
        rv1 = client.get(f"/api/project/{project.name}/difficulty")
        assert rv1.status_code == 200
        gen1 = rv1.get_json()["book"].get("difficulty")

        # force=1 must re-score; result content should still be valid.
        rv2 = client.get(f"/api/project/{project.name}/difficulty?force=1")
        assert rv2.status_code == 200
        data2 = rv2.get_json()
        assert data2.get("ok") is True
        assert data2["book"].get("difficulty") == gen1  # same text → same score

    def test_difficulty_cached_on_disk(self, client, project):
        client.get(f"/api/project/{project.name}/difficulty")
        assert (project / "difficulty.json").exists()


class TestDifficultyRouteError:
    def test_score_book_exception_returns_500(self, client, project, monkeypatch):
        import web_ui.app as app_module

        def _boom(*_a, **_kw):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(app_module, "_get_projects_dir",
                            lambda: project.parent)
        # score_book is imported at module level in app.py, so patch it there.
        monkeypatch.setattr(app_module, "score_book", _boom)
        rv = client.get(f"/api/project/{project.name}/difficulty")
        assert rv.status_code == 500
        data = rv.get_json()
        assert "error" in data
        assert data["error"] == "Difficulty scoring failed"
