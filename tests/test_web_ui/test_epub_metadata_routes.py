"""Tests for Dublin Core metadata fields in epub-related API routes.

Covers three gaps identified in the coverage audit for
``feat/dashboard-workflow-improvements``:

1. ``/api/project/<id>/epub-status`` returns the five optional DC metadata
   fields (translator, description, rights, source_title, publisher) from
   ``project.json`` so the Export tab can pre-populate its form on load.

2. ``POST /api/project/<id>/build-epub`` persists DC metadata fields back to
   ``project.json`` (``config_dirty`` path) so re-exports keep the same
   values without the user retyping them.

3. ``POST /api/project/<id>/build-epub`` falls back to ``project.json`` values
   when the request body does not include a given field (the
   ``else: metadata[key] = str(config.get(...))`` branch).

These tests do NOT attempt to complete a full EPUB build — they either
inspect the epub-status response directly or rely on the fact that the
note-save / config-persist happens before the build step that would fail.
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
    """Minimal project dir with ``_get_projects_dir`` redirected."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_project_json(proj_dir: Path, data: dict) -> None:
    (proj_dir / "project.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _read_project_json(proj_dir: Path) -> dict:
    p = proj_dir / "project.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ===========================================================================
# epub-status DC metadata fields
# ===========================================================================

class TestEpubStatusDublinCoreFields:
    """``/api/project/<id>/epub-status`` must return the five optional DC
    fields from ``project.json`` so the Export form can pre-populate."""

    def _get_status(self, client, project_id: str) -> dict:
        rv = client.get(f"/api/project/{project_id}/epub-status")
        assert rv.status_code == 200
        return rv.get_json()

    def test_all_dc_fields_present_in_response_when_set(self, client, project):
        _write_project_json(project, {
            "translator": "Jane Doe",
            "description": "A riveting story.",
            "rights": "Translation © 2026 Jane Doe",
            "source_title": "El libro original",
            "publisher": "My Press",
        })
        data = self._get_status(client, "proj1")
        assert data["translator"] == "Jane Doe"
        assert data["description"] == "A riveting story."
        assert data["rights"] == "Translation © 2026 Jane Doe"
        assert data["source_title"] == "El libro original"
        assert data["publisher"] == "My Press"

    def test_dc_fields_empty_string_for_fresh_project(self, client, project):
        """A project with no project.json must return empty strings (not None)
        for all five DC fields so the JS pre-fill logic can safely compare."""
        data = self._get_status(client, "proj1")
        for field in ("translator", "description", "rights", "source_title", "publisher"):
            assert field in data, f"Missing field: {field}"
            assert data[field] == "", f"Expected '' for {field}, got {data[field]!r}"

    def test_dc_fields_returned_alongside_cover_fields(self, client, project):
        """DC fields must coexist with the cover_filename / cover_mtime fields
        added in the same commit (no key conflict / accidental omission)."""
        images = project / "images"
        images.mkdir()
        (images / "cover.jpg").write_bytes(b"jpg")
        _write_project_json(project, {"translator": "T"})

        data = self._get_status(client, "proj1")
        assert data["cover_filename"] == "cover.jpg"
        assert data["translator"] == "T"


# ===========================================================================
# build-epub DC metadata persistence
# ===========================================================================

class TestBuildEpubDublinCorePersistence:
    """``POST /api/project/<id>/build-epub`` persists DC metadata to
    ``project.json`` even when the build itself fails (no chunks exist).
    Mirrors ``TestBuildEpubPersistsNote`` for the translator-note field."""

    def _post_build(self, client, payload: dict) -> None:
        # The build will return 400 (no chunks) but the persist runs first.
        client.post("/api/project/proj1/build-epub", json=payload)

    def test_translator_persisted_to_project_json(self, client, project):
        self._post_build(client, {"translator": "Persisted Translator"})
        cfg = _read_project_json(project)
        assert cfg.get("translator") == "Persisted Translator"

    def test_description_persisted_to_project_json(self, client, project):
        self._post_build(client, {"description": "A great book."})
        cfg = _read_project_json(project)
        assert cfg.get("description") == "A great book."

    def test_rights_persisted_to_project_json(self, client, project):
        self._post_build(client, {"rights": "© 2026"})
        cfg = _read_project_json(project)
        assert cfg.get("rights") == "© 2026"

    def test_source_title_persisted_to_project_json(self, client, project):
        self._post_build(client, {"source_title": "Original Title"})
        cfg = _read_project_json(project)
        assert cfg.get("source_title") == "Original Title"

    def test_publisher_persisted_to_project_json(self, client, project):
        self._post_build(client, {"publisher": "Big Press"})
        cfg = _read_project_json(project)
        assert cfg.get("publisher") == "Big Press"

    def test_all_dc_fields_persisted_together(self, client, project):
        payload = {
            "translator": "Jane",
            "description": "Synopsis",
            "rights": "CC0",
            "source_title": "Orig",
            "publisher": "Imprint",
        }
        self._post_build(client, payload)
        cfg = _read_project_json(project)
        assert cfg["translator"] == "Jane"
        assert cfg["description"] == "Synopsis"
        assert cfg["rights"] == "CC0"
        assert cfg["source_title"] == "Orig"
        assert cfg["publisher"] == "Imprint"

    def test_empty_string_clears_previously_saved_value(self, client, project):
        """Sending an empty string must overwrite (clear) a prior value so
        users can remove metadata they previously saved."""
        _write_project_json(project, {"translator": "Old Name"})
        self._post_build(client, {"translator": ""})
        cfg = _read_project_json(project)
        assert cfg["translator"] == ""

    def test_missing_key_falls_back_to_project_json(self, client, project):
        """When a DC key is absent from the request body the route must read
        the value from ``project.json`` rather than writing an empty string."""
        _write_project_json(project, {"translator": "Config Translator"})
        # Post without the translator key — the pre-existing value must survive.
        self._post_build(client, {"description": "New desc"})
        cfg = _read_project_json(project)
        assert cfg["translator"] == "Config Translator"

    def test_config_dirty_flag_does_not_write_when_value_unchanged(
        self, client, project
    ):
        """When the submitted value equals the stored value, ``_save_project_config``
        must not be called redundantly. We verify by checking the file's stored
        value stays correct (not a deep file-stat test, just value correctness)."""
        _write_project_json(project, {"translator": "Same", "description": "Same"})
        self._post_build(client, {"translator": "Same", "description": "Same"})
        cfg = _read_project_json(project)
        assert cfg["translator"] == "Same"
        assert cfg["description"] == "Same"

    def test_author_and_title_also_persisted(self, client, project):
        """The route also persists author and title via the second persistence
        block — verify both land in project.json."""
        self._post_build(client, {"title": "My Book", "author": "Author Name"})
        cfg = _read_project_json(project)
        assert cfg["title"] == "My Book"
        assert cfg["author"] == "Author Name"
