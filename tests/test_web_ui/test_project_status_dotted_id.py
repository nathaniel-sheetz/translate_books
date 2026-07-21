"""Regression test: archiving a project whose folder name contains a period.

Real project directories can end up with names like ``foo.bak-ch1-restore``
(manual backups saved next to the original). ``_safe_id`` previously
rejected any id containing a period, so ``PATCH /api/project/<id>/status``
returned 400 for these — silently, since the frontend's fetch chain has no
``.catch()`` and only acts on ``data.ok``. The native <select> still shows
the chosen option (that's just browser behavior), making it look like
nothing happened and the choice reverting on reload, because nothing was
ever persisted.

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
    def test_patch_status_persists_for_dotted_project_id(self, client, projects_dir):
        proj_id = "the-little-duke.bak-ch1-restore"
        proj_dir = projects_dir / proj_id
        proj_dir.mkdir()
        (proj_dir / "chunks").mkdir()

        rv = client.patch(
            f"/api/project/{proj_id}/status",
            json={"status": "archived"},
        )
        assert rv.status_code == 200
        assert rv.get_json() == {"ok": True, "status": "archived"}

        saved = json.loads((proj_dir / "project.json").read_text(encoding="utf-8"))
        assert saved["status"] == "archived"
