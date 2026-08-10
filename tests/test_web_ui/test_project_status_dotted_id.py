"""Regression test: archiving a project whose folder name contains a period.

Real project directories can end up with names like ``foo.bak-ch1-restore``
(manual backups saved next to the original). ``_safe_id`` previously
rejected any id containing a period, so ``PATCH /api/project/<id>/archived``
(then ``.../status``) returned 400 for these — silently, since the frontend's
fetch chain only acts on ``data.ok``, so the choice looked applied until the
next reload, when nothing turned out to have been persisted.

Conventions follow ``test_dashboard_workflow_improvements.py`` (Flask test
client + monkeypatch on ``_get_projects_dir``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import _safe_id, app
from web_ui.project_cards import clear_card_cache


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: root)
    return root


class TestSafeIdAllowsPeriods:
    def test_dotted_name_is_safe(self):
        assert _safe_id("the-little-duke.bak-ch1-restore") is True

    def test_bare_dot_is_unsafe(self):
        assert _safe_id(".") is False

    def test_parent_traversal_is_unsafe(self):
        assert _safe_id("..") is False

    def test_slash_is_still_unsafe(self):
        assert _safe_id("../etc") is False


class TestArchiveDottedProject:
    def test_patch_archived_persists_for_dotted_project_id(self, client, projects_dir):
        proj_id = "the-little-duke.bak-ch1-restore"
        proj_dir = projects_dir / proj_id
        proj_dir.mkdir()
        (proj_dir / "chunks").mkdir()
        clear_card_cache()

        rv = client.patch(
            f"/api/project/{proj_id}/archived",
            json={"archived": True},
        )
        assert rv.status_code == 200
        assert rv.get_json() == {"ok": True, "archived": True, "status": "archived"}

        saved = json.loads((proj_dir / "project.json").read_text(encoding="utf-8"))
        assert saved["archived"] is True

    def test_unarchiving_returns_the_derived_status(self, client, projects_dir):
        """Unarchiving falls back to whatever the files imply — here, nothing read."""
        proj_id = "the-little-duke.bak-ch1-restore"
        proj_dir = projects_dir / proj_id
        proj_dir.mkdir()
        (proj_dir / "chunks").mkdir()
        # A project written before status was derived: only the legacy key.
        (proj_dir / "project.json").write_text(
            json.dumps({"title": "Dotted", "status": "archived"}), encoding="utf-8"
        )
        clear_card_cache()

        rv = client.patch(f"/api/project/{proj_id}/archived", json={"archived": False})
        assert rv.status_code == 200
        assert rv.get_json() == {"ok": True, "archived": False, "status": "pending"}

        saved = json.loads((proj_dir / "project.json").read_text(encoding="utf-8"))
        assert saved["archived"] is False
        # The stale hand-set field is gone, so the two can never disagree.
        assert "status" not in saved

    def test_non_boolean_body_is_rejected(self, client, projects_dir):
        proj_id = "the-little-duke.bak-ch1-restore"
        (projects_dir / proj_id).mkdir()
        rv = client.patch(f"/api/project/{proj_id}/archived", json={"archived": "yes"})
        assert rv.status_code == 400

    def test_missing_project_is_404_and_does_not_mkdir(self, client, projects_dir):
        rv = client.patch("/api/project/ghost-book/archived", json={"archived": True})
        assert rv.status_code == 404
        assert rv.get_json() == {"error": "Project not found"}
        assert not (projects_dir / "ghost-book").exists()
