"""The dashboard's API translate routes refuse a modernize book.

Those routes render prompts/translation.txt over the source as English. A
modernize book (``task: modernize``) must go through the harness instead:
spelling pass, over-edit guard, archaism list.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.harness import state
from web_ui.app import app

ROUTES = [
    ("/api/project/proj1/translate/realtime", {"chunk_id": "chapter_001_chunk_000"}),
    ("/api/project/proj1/translate/batch", {"chapter_ids": ["chapter_001"]}),
    ("/api/project/proj1/batch-api/submit", {"chapter_ids": ["chapter_001"]}),
]


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "proj1"
    (proj / "chunks").mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj


@pytest.mark.parametrize("route,body", ROUTES)
def test_modernize_book_is_refused(client, project, route, body):
    state.save_config(project, {"task": "modernize"})
    rv = client.post(route, json=body)
    assert rv.status_code == 400
    assert "modernize" in rv.get_json()["error"]


def test_translate_book_passes_the_guard(client, project):
    state.save_config(project, {})
    # Past the guard: the missing chunk is the next thing it reports.
    rv = client.post(*ROUTES[0][:1], json=ROUTES[0][1])
    assert rv.status_code == 404
