"""Regression tests for the dashboard workflow improvements.

Covers three high-value behaviors introduced in
``feat/dashboard-workflow-improvements``:

1. ``/api/project/<id>/status`` reports active annotation counts (dedup +
   tombstones), matching what the reader's chapter list shows. The pre-fix
   route naively counted every line in ``annotations.jsonl``.
2. ``/api/project/<id>/epub-status`` surfaces the auto-detected cover image
   with the same precedence (``.jpg`` > ``.jpeg`` > ``.png``) that
   ``src.epub_builder._resolve_cover`` uses at build time. The Export tab's
   thumbnail depends on this.
3. POST ``/api/setup/<id>/style-guide`` with only ``content`` set must
   preserve any existing ``light_content``. The new inline "Edit" button in
   the Style stage relies on this invariant — without it, editing the main
   guide would silently wipe the light guide.

Conventions follow ``test_glossary_routes.py`` (Flask test client +
``monkeypatch`` on ``_get_projects_dir``).
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
    """Minimal project dir with the app's ``_get_projects_dir`` redirected."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_chapter(proj_dir: Path, chapter_id: str, text: str = "hello world") -> None:
    chapters = proj_dir / "chapters"
    chapters.mkdir(exist_ok=True)
    (chapters / f"{chapter_id}.txt").write_text(text, encoding="utf-8")


def _write_annotations(proj_dir: Path, records: list[dict]) -> None:
    lines = "\n".join(json.dumps(r) for r in records) + "\n"
    (proj_dir / "annotations.jsonl").write_text(lines, encoding="utf-8")


# ===========================================================================
# Fix #2 — Review tab annotation count matches reader
# ===========================================================================

class TestProjectStatusAnnotationCount:
    """``/api/project/<id>/status`` must dedup by ``es_idx`` and skip
    ``removed: True`` tombstones, just like ``_load_annotations`` does
    for the reader."""

    def _get_chapter(self, client, project_id: str, chapter_id: str) -> dict:
        rv = client.get(f"/api/project/{project_id}/status")
        assert rv.status_code == 200
        data = rv.get_json()
        chapters = {c["id"]: c for c in data["chapters"]}
        assert chapter_id in chapters, f"{chapter_id} missing from {list(chapters)}"
        return chapters[chapter_id]

    def test_no_annotations_file_means_zero(self, client, project):
        _write_chapter(project, "chapter_001")
        ch = self._get_chapter(client, "proj1", "chapter_001")
        assert ch["annotation_count"] == 0

    def test_superseded_edit_only_counts_once(self, client, project):
        # Two records for the same es_idx — the latest one wins (mirrors
        # _load_annotations behavior). Pre-fix code would have counted 2.
        _write_chapter(project, "chapter_001")
        _write_annotations(project, [
            {"chapter_id": "chapter_001", "es_idx": 5,
             "kind": "word_choice", "note": "first attempt"},
            {"chapter_id": "chapter_001", "es_idx": 5,
             "kind": "word_choice", "note": "revised"},
        ])
        ch = self._get_chapter(client, "proj1", "chapter_001")
        assert ch["annotation_count"] == 1

    def test_removed_tombstone_drops_entry(self, client, project):
        # A removed: True record for an es_idx should pop the prior entry.
        # Pre-fix code would have counted 2 (the original + the tombstone).
        _write_chapter(project, "chapter_001")
        _write_annotations(project, [
            {"chapter_id": "chapter_001", "es_idx": 1, "kind": "word_choice"},
            {"chapter_id": "chapter_001", "es_idx": 1, "removed": True},
        ])
        ch = self._get_chapter(client, "proj1", "chapter_001")
        assert ch["annotation_count"] == 0

    def test_mixed_realistic_scenario(self, client, project):
        # One active entry, one superseded edit on a different idx, one
        # tombstoned entry — final active count is 2.
        _write_chapter(project, "chapter_001")
        _write_annotations(project, [
            {"chapter_id": "chapter_001", "es_idx": 1, "kind": "word_choice"},
            {"chapter_id": "chapter_001", "es_idx": 2, "kind": "footnote",
             "note": "first"},
            {"chapter_id": "chapter_001", "es_idx": 2, "kind": "footnote",
             "note": "revised"},
            {"chapter_id": "chapter_001", "es_idx": 3, "kind": "flag"},
            {"chapter_id": "chapter_001", "es_idx": 3, "removed": True},
        ])
        ch = self._get_chapter(client, "proj1", "chapter_001")
        assert ch["annotation_count"] == 2

    def test_other_chapters_dont_leak_into_count(self, client, project):
        _write_chapter(project, "chapter_001")
        _write_chapter(project, "chapter_002")
        _write_annotations(project, [
            {"chapter_id": "chapter_001", "es_idx": 1, "kind": "word_choice"},
            {"chapter_id": "chapter_002", "es_idx": 1, "kind": "word_choice"},
            {"chapter_id": "chapter_002", "es_idx": 2, "kind": "word_choice"},
        ])
        ch1 = self._get_chapter(client, "proj1", "chapter_001")
        ch2 = self._get_chapter(client, "proj1", "chapter_002")
        assert ch1["annotation_count"] == 1
        assert ch2["annotation_count"] == 2


# ===========================================================================
# Fix #6 — Cover thumbnail surfaced from /epub-status
# ===========================================================================

class TestEpubStatusCover:
    """``/api/project/<id>/epub-status`` returns the cover the EPUB builder
    will pick. Precedence must match ``src.epub_builder._resolve_cover``:
    ``cover.jpg`` > ``cover.jpeg`` > ``cover.png``."""

    def _get_status(self, client, project_id: str) -> dict:
        rv = client.get(f"/api/project/{project_id}/epub-status")
        assert rv.status_code == 200
        return rv.get_json()

    def test_no_images_dir_returns_null(self, client, project):
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] is None
        assert data["cover_mtime"] is None

    def test_empty_images_dir_returns_null(self, client, project):
        (project / "images").mkdir()
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] is None
        assert data["cover_mtime"] is None

    def test_png_only_is_picked_with_mtime(self, client, project):
        images = project / "images"
        images.mkdir()
        (images / "cover.png").write_bytes(b"fake-png")
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] == "cover.png"
        assert isinstance(data["cover_mtime"], int)
        assert data["cover_mtime"] > 0

    def test_jpg_wins_over_jpeg_and_png(self, client, project):
        # Precedence pinning — if this drifts out of sync with
        # src.epub_builder._resolve_cover, the dashboard thumbnail will
        # show one cover while the built EPUB uses another.
        images = project / "images"
        images.mkdir()
        (images / "cover.jpg").write_bytes(b"jpg")
        (images / "cover.jpeg").write_bytes(b"jpeg")
        (images / "cover.png").write_bytes(b"png")
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] == "cover.jpg"

    def test_jpeg_wins_over_png(self, client, project):
        images = project / "images"
        images.mkdir()
        (images / "cover.jpeg").write_bytes(b"jpeg")
        (images / "cover.png").write_bytes(b"png")
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] == "cover.jpeg"

    def test_non_cover_images_are_ignored(self, client, project):
        images = project / "images"
        images.mkdir()
        (images / "chapter1.jpg").write_bytes(b"jpg")
        (images / "frontispiece.png").write_bytes(b"png")
        data = self._get_status(client, "proj1")
        assert data["cover_filename"] is None


# ===========================================================================
# Fix #4 — Inline-edit must preserve light_content
# ===========================================================================

class TestStyleGuideEditPreservesLight:
    """The new inline "Edit" button POSTs only ``content``; the endpoint
    must keep any existing ``light_content`` intact. Without this, editing
    the main guide would silently wipe the light style guide that the
    reader's retranslate prompt relies on."""

    def test_edit_main_content_preserves_existing_light(self, client, project):
        # Seed a style guide with both main + light content.
        rv = client.post(
            "/api/setup/proj1/style-guide",
            json={"content": "Original main guide"},
        )
        assert rv.status_code == 200, rv.get_json()

        rv = client.post(
            "/api/setup/proj1/style-guide/light",
            json={"light_content": "Condensed retranslation guide"},
        )
        assert rv.status_code == 200, rv.get_json()

        # Simulate the inline Edit handler: POST only content.
        rv = client.post(
            "/api/setup/proj1/style-guide",
            json={"content": "Revised main guide"},
        )
        assert rv.status_code == 200, rv.get_json()

        # Load style.json directly to confirm both fields are intact.
        from src.utils.file_io import load_style_guide

        guide = load_style_guide(project / "style.json")
        assert guide.content == "Revised main guide"
        assert guide.light_content == "Condensed retranslation guide"

    def test_edit_with_no_prior_light_stays_none(self, client, project):
        # No light content seeded — editing main must not invent one.
        rv = client.post(
            "/api/setup/proj1/style-guide",
            json={"content": "Original main"},
        )
        assert rv.status_code == 200

        rv = client.post(
            "/api/setup/proj1/style-guide",
            json={"content": "Revised main"},
        )
        assert rv.status_code == 200

        from src.utils.file_io import load_style_guide
        guide = load_style_guide(project / "style.json")
        assert guide.content == "Revised main"
        assert guide.light_content is None
