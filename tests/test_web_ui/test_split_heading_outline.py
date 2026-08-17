"""Dashboard split endpoints: heading-outline wiring deferred from review.

Covers ``has_heading_outline`` on /status, the shared /split and /split/preview
helper (auto default, ledger/warnings/applied, broken sidecar, ValueError→400),
and leftover ``chapter_*.txt`` clearing on apply.

Conventions follow ``test_dashboard_workflow_improvements.py`` (Flask test
client + monkeypatch on ``_get_projects_dir``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


_BODY = "lorem ipsum dolor sit amet " * 40

_MIXED_CASE_BOOK = [
    (1, "Among the Meadow People"),
    (2, "CONTENTS"),
    (2, "INTRODUCTION."),
    (2, "The BUTTERFLY That WENT CALLING"),
    (2, "THE ROBINS BUILD A NEST."),
    (2, "The Lazy Snail"),
    (2, "Mr GREEN FROG AND HIS VISITORS"),
    (2, "The Earthworm Half-Brothers"),
    (2, "The Crickets School"),
]


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_outline(proj_dir: Path, headings, body: str = _BODY) -> None:
    parts, outline = [], []
    for level, text in headings:
        parts += [text, body]
        outline.append({"level": level, "text": text})
    (proj_dir / "source.txt").write_text("\n\n".join(parts), encoding="utf-8")
    (proj_dir / "headings.json").write_text(
        json.dumps({"version": 1, "headings": outline}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestHasHeadingOutlineStatus:
    def test_false_when_sidecar_absent(self, client, project):
        rv = client.get("/api/project/proj1/status")
        assert rv.status_code == 200
        assert rv.get_json()["has_heading_outline"] is False

    def test_true_when_sidecar_exists_even_if_broken(self, client, project):
        (project / "headings.json").write_text("{not json", encoding="utf-8")
        rv = client.get("/api/project/proj1/status")
        assert rv.status_code == 200
        assert rv.get_json()["has_heading_outline"] is True


class TestSplitPreviewHeadingOutline:
    def test_omitted_pattern_type_anchors_on_the_outline(self, client, project):
        _write_outline(project, _MIXED_CASE_BOOK)
        rv = client.post("/api/project/proj1/split/preview", json={})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["pattern_used"] == "headings"
        assert data["heading_outline"]["applied"] is True
        assert data["heading_outline"]["selected"] == "h2"
        assert "dropped" in data
        assert "ledger" in data
        assert "warnings" in data
        names = [ch["name"] for ch in data["chapters"]]
        assert "The Lazy Snail" in names
        assert any(d["label"] == "Contents" for d in data["dropped"])

    def test_heading_level_override_is_honored(self, client, project):
        headings = []
        for i in range(1, 9):
            headings.append((2, f"Scene {i}"))
            if i % 2:
                headings.append((3, f"Chapter {i}"))
        _write_outline(project, headings)

        auto = client.post("/api/project/proj1/split/preview", json={}).get_json()
        assert auto["heading_outline"]["selected"] == "h2"

        forced = client.post(
            "/api/project/proj1/split/preview",
            json={"pattern_type": "headings", "heading_level": "h3"},
        )
        assert forced.status_code == 200
        data = forced.get_json()
        assert data["heading_outline"]["selected"] == "h3"
        assert "explicitly requested" in data["heading_outline"]["reason"]
        assert [ch["name"] for ch in data["chapters"]] == [
            f"Chapter {i}" for i in (1, 3, 5, 7)
        ]

    def test_broken_sidecar_with_headings_is_400(self, client, project):
        (project / "source.txt").write_text(
            "Chapter I\n\n" + _BODY + "\n\nChapter II\n\n" + _BODY,
            encoding="utf-8",
        )
        (project / "headings.json").write_text("{not json", encoding="utf-8")
        rv = client.post(
            "/api/project/proj1/split/preview",
            json={"pattern_type": "headings"},
        )
        assert rv.status_code == 400
        assert "headings.json" in rv.get_json()["error"]

    def test_broken_sidecar_with_auto_warns_and_regexes(self, client, project):
        (project / "source.txt").write_text(
            "Chapter I\n\n" + _BODY + "\n\nChapter II\n\n" + _BODY,
            encoding="utf-8",
        )
        (project / "headings.json").write_text("{not json", encoding="utf-8")
        rv = client.post("/api/project/proj1/split/preview", json={"pattern_type": "auto"})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["heading_outline"] is None
        assert data["pattern_used"] != "headings"
        assert any("headings.json" in w for w in data["warnings"])

    def test_splitter_value_error_is_400_not_500(self, client, project):
        (project / "source.txt").write_text(
            "Chapter I\n\n" + _BODY + "\n\nChapter II\n\n" + _BODY,
            encoding="utf-8",
        )
        rv = client.post(
            "/api/project/proj1/split/preview",
            json={"pattern_type": "headings"},
        )
        assert rv.status_code == 400
        assert "heading outline" in rv.get_json()["error"]

    def test_garbage_heading_level_is_400(self, client, project):
        _write_outline(project, _MIXED_CASE_BOOK)
        rv = client.post(
            "/api/project/proj1/split/preview",
            json={"pattern_type": "headings", "heading_level": "foo"},
        )
        assert rv.status_code == 400
        assert "invalid heading level" in rv.get_json()["error"]


class TestSplitApplyClearsStale:
    def test_re_split_unlinks_leftover_chapter_files(self, client, project):
        _write_outline(project, _MIXED_CASE_BOOK)
        chapters_dir = project / "chapters"
        chapters_dir.mkdir()
        for i in range(1, 21):
            (chapters_dir / f"chapter_{i:02d}.txt").write_text("stale", encoding="utf-8")

        rv = client.post("/api/project/proj1/split", json={})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["pattern_used"] == "headings"
        written = sorted(p.name for p in chapters_dir.glob("chapter_*.txt"))
        assert written == [f"chapter_{i:02d}.txt" for i in range(1, data["chapter_count"] + 1)]
        assert not (chapters_dir / "chapter_20.txt").exists()
        assert data["chapter_count"] < 20
