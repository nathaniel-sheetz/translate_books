"""Tests for PATCH /api/project/<id>/chapter-manifest/<chapter_id>."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "p1"
    proj_dir.mkdir(parents=True)

    config = {
        "title": "Test Project",
        "chapter_manifest": [
            {"id": "chapter_01", "kind": "front_matter", "label": "To the Children"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
            {"id": "chapter_03", "kind": "back_matter", "label": "Colophon"},
        ],
    }
    (proj_dir / "project.json").write_text(
        json.dumps(config), encoding="utf-8"
    )

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _read_config(proj_dir):
    return json.loads((proj_dir / "project.json").read_text(encoding="utf-8"))


class TestPatchChapterManifestLabel:
    def test_set_label_on_front_matter(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": "A los niños"},
        )
        assert rv.status_code == 200
        body = rv.get_json()
        assert body["id"] == "chapter_01"
        assert body["kind"] == "front_matter"
        assert body["label"] == "A los niños"

        # Round-trip: file on disk reflects the change.
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert entry["label"] == "A los niños"

    def test_set_label_on_back_matter(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_03",
            json={"label": "Colofón"},
        )
        assert rv.status_code == 200
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_03")
        assert entry["label"] == "Colofón"

    def test_empty_label_clears_override(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": ""},
        )
        assert rv.status_code == 200
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert "label" not in entry

    def test_whitespace_only_label_clears_override(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": "   "},
        )
        assert rv.status_code == 200
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert "label" not in entry

    def test_label_is_trimmed(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": "  A los niños  "},
        )
        assert rv.status_code == 200
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert entry["label"] == "A los niños"

    def test_rejects_numbered_chapter(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_02",
            json={"label": "Capítulo Uno"},
        )
        assert rv.status_code == 400
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_02")
        assert "label" not in entry

    def test_unknown_chapter_id_returns_404(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_99",
            json={"label": "Nope"},
        )
        assert rv.status_code == 404

    def test_project_without_manifest_returns_404(
        self, client, tmp_path, monkeypatch
    ):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "p2"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.json").write_text(
            json.dumps({"title": "no-manifest"}), encoding="utf-8"
        )
        import web_ui.app as app_module
        monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)

        rv = client.patch(
            "/api/project/p2/chapter-manifest/chapter_01",
            json={"label": "Nope"},
        )
        assert rv.status_code == 404

    def test_missing_label_field_returns_400(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={},
        )
        assert rv.status_code == 400

    def test_rejects_path_traversal_in_chapter_id(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/..%2Fchapter_01",
            json={"label": "x"},
        )
        # Flask routes won't decode .. into routes — but our _safe_id check
        # also rejects backslashes/forward slashes. Either way: not 200.
        assert rv.status_code in (400, 404)

    def test_rejects_path_traversal_in_project_id(self, client, project):
        rv = client.patch(
            "/api/project/..%2Fp1/chapter-manifest/chapter_01",
            json={"label": "x"},
        )
        assert rv.status_code in (400, 404)

    def test_null_label_treated_as_clear(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": None},
        )
        assert rv.status_code == 200
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert "label" not in entry

    def test_label_too_long_returns_400(self, client, project):
        rv = client.patch(
            "/api/project/p1/chapter-manifest/chapter_01",
            json={"label": "x" * 501},
        )
        assert rv.status_code == 400
        cfg = _read_config(project)
        entry = next(e for e in cfg["chapter_manifest"] if e["id"] == "chapter_01")
        assert entry.get("label") == "To the Children"
