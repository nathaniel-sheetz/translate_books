"""Regression tests for per-project chunking parameter persistence.

The Stage 3 (Chunk) tab on the dashboard remembers the user's last
successful chunk parameters by writing them to ``projects/<id>/project.json``
under the ``chunking_config`` key. The form pre-fills from those values on
the next load via ``/api/project/<id>/status``.

These tests cover:

1. ``/api/project/<id>/status`` returns ``chunking_config: None`` for a
   fresh project (so the JS falls back to its HTML default values).
2. A successful ``POST /api/project/<id>/chunk-all`` persists the supplied
   parameters to ``project.json``, and a subsequent status call surfaces
   them.
3. A successful ``POST /api/project/<id>/chapters/<chapter_id>/rechunk``
   also persists, and subsequent runs overwrite the previously-saved
   parameters.
4. The persisted values come from the **server-validated** ``ChunkingConfig``
   (so e.g. defaults fill in for any field the client omits, instead of
   us saving a partial record).

Conventions follow ``test_dashboard_workflow_improvements.py`` — Flask
test client + ``monkeypatch`` on ``_get_projects_dir``.
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
    """Minimal project dir with one chapter long enough to chunk."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    (proj_dir / "chapters").mkdir(parents=True)

    # ~1200 words across several paragraphs — enough for the chunker to
    # actually run (min_chunk_size defaults to 500) without us having to
    # tune extreme parameters.
    paragraph = ("word " * 200).strip()
    body = "\n\n".join([paragraph] * 6)
    (proj_dir / "chapters" / "chapter_001.txt").write_text(body, encoding="utf-8")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _read_project_json(proj_dir: Path) -> dict:
    p = proj_dir / "project.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ===========================================================================
# Tests
# ===========================================================================

class TestChunkingConfigInStatus:
    def test_status_returns_none_for_fresh_project(self, client, project):
        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "chunking_config" in data
        assert data["chunking_config"] is None

    def test_status_surfaces_persisted_config(self, client, project):
        # Pre-seed project.json as if a previous chunk run had saved params.
        (project / "project.json").write_text(
            json.dumps({
                "title": "Proj 1",
                "chunking_config": {
                    "target_size": 1500,
                    "min_chunk_size": 400,
                    "max_chunk_size": 2500,
                    "overlap_paragraphs": 1,
                    "min_overlap_words": 50,
                },
            }),
            encoding="utf-8",
        )
        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        cc = rv.get_json()["chunking_config"]
        assert cc == {
            "target_size": 1500,
            "min_chunk_size": 400,
            "max_chunk_size": 2500,
            "overlap_paragraphs": 1,
            "min_overlap_words": 50,
        }


class TestChunkAllPersistsConfig:
    def test_chunk_all_writes_chunking_config_to_project_json(self, client, project):
        params = {
            "target_size": 1200,
            "min_chunk_size": 300,
            "max_chunk_size": 2000,
            "overlap_paragraphs": 1,
            "min_overlap_words": 25,
        }
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json=params,
        )
        assert rv.status_code == 200, rv.get_json()
        assert rv.get_json().get("ok") is True

        cfg = _read_project_json(project)
        assert cfg.get("chunking_config") == params

    def test_chunk_all_persisted_config_appears_in_status(self, client, project):
        params = {
            "target_size": 800,
            "min_chunk_size": 200,
            "max_chunk_size": 1500,
            "overlap_paragraphs": 2,
            "min_overlap_words": 30,
        }
        client.post(f"/api/project/{project.name}/chunk-all", json=params)

        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        assert rv.get_json()["chunking_config"] == params

    def test_chunk_all_persists_validated_defaults_for_omitted_fields(self, client, project):
        # Client only sends target_size — server should fill the rest from
        # ChunkingConfig defaults and persist the validated record.
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"target_size": 1000},
        )
        assert rv.status_code == 200, rv.get_json()

        cc = _read_project_json(project).get("chunking_config")
        assert cc is not None
        assert cc["target_size"] == 1000
        # Defaults from src.models.ChunkingConfig
        assert cc["min_chunk_size"] == 500
        assert cc["max_chunk_size"] == 3000
        assert cc["overlap_paragraphs"] == 0
        assert cc["min_overlap_words"] == 0

    def test_chunk_all_overwrites_previous_persisted_config(self, client, project):
        first = {
            "target_size": 1500,
            "min_chunk_size": 400,
            "max_chunk_size": 2500,
            "overlap_paragraphs": 1,
            "min_overlap_words": 50,
        }
        client.post(f"/api/project/{project.name}/chunk-all", json=first)
        assert _read_project_json(project)["chunking_config"] == first

        second = {
            "target_size": 1000,
            "min_chunk_size": 250,
            "max_chunk_size": 2000,
            "overlap_paragraphs": 2,
            "min_overlap_words": 75,
        }
        client.post(f"/api/project/{project.name}/chunk-all", json=second)
        assert _read_project_json(project)["chunking_config"] == second

    def test_chunk_all_preserves_other_project_json_keys(self, client, project):
        # Seed unrelated metadata; persistence must not clobber it.
        (project / "project.json").write_text(
            json.dumps({"title": "My Book", "gutenberg_url": "http://x"}),
            encoding="utf-8",
        )
        params = {
            "target_size": 1200,
            "min_chunk_size": 300,
            "max_chunk_size": 2000,
            "overlap_paragraphs": 1,
            "min_overlap_words": 25,
        }
        client.post(f"/api/project/{project.name}/chunk-all", json=params)

        cfg = _read_project_json(project)
        assert cfg["title"] == "My Book"
        assert cfg["gutenberg_url"] == "http://x"
        assert cfg["chunking_config"] == params


class TestRechunkPersistsConfig:
    def _initial_chunk(self, client, project):
        """Produce real chunk files so /rechunk has something to reconstruct from."""
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "target_size": 1500,
                "min_chunk_size": 300,
                "max_chunk_size": 2500,
                "overlap_paragraphs": 0,
                "min_overlap_words": 0,
            },
        )
        assert rv.status_code == 200, rv.get_json()

    def test_rechunk_writes_chunking_config(self, client, project):
        self._initial_chunk(client, project)

        new_params = {
            "target_size": 900,
            "min_chunk_size": 200,
            "max_chunk_size": 1800,
            "overlap_paragraphs": 1,
            "min_overlap_words": 40,
        }
        rv = client.post(
            f"/api/project/{project.name}/chapters/chapter_001/rechunk",
            json=new_params,
        )
        assert rv.status_code == 200, rv.get_json()

        assert _read_project_json(project)["chunking_config"] == new_params

    def test_rechunk_overrides_previous_chunk_all_config(self, client, project):
        self._initial_chunk(client, project)
        first = _read_project_json(project)["chunking_config"]

        new_params = {
            "target_size": 700,
            "min_chunk_size": 150,
            "max_chunk_size": 1400,
            "overlap_paragraphs": 2,
            "min_overlap_words": 20,
        }
        client.post(
            f"/api/project/{project.name}/chapters/chapter_001/rechunk",
            json=new_params,
        )
        cc = _read_project_json(project)["chunking_config"]
        assert cc == new_params
        assert cc != first
