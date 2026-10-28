"""Tests for ``src.corrections_apply.rebuild_epub``.

The corrections and judge-apply rebuild goes through ``build_epub`` with no
output path, so it must land on the same ``<Spanish title>.epub`` the web UI's
Export tab writes -- not a second, slug-named copy beside it.
"""

import json

from src.corrections_apply import rebuild_epub


def _make_project(tmp_path):
    project = tmp_path / "bambi-a-life-in-the-woods"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nBambi nació en el bosque.", encoding="utf-8",
    )
    return project


def test_rebuild_named_after_spanish_title(tmp_path):
    project = _make_project(tmp_path)
    (project / "project.json").write_text(json.dumps({
        "title": "Bambi: A Life in the Woods",
        "spanish_title": "Bambi: Una vida en el bosque",
        "author": "Felix Salten",
    }), encoding="utf-8")

    epub_path = rebuild_epub(project)

    assert epub_path == project / "Bambi- Una vida en el bosque.epub"
    assert [p.name for p in project.glob("*.epub")] == [epub_path.name]
