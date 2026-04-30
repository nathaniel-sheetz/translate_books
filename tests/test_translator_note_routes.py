"""Tests for the /api/project/<id>/translator-note routes."""

import json
import sys
import time
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

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


class TestGetTranslatorNote:
    def test_defaults_when_no_file(self, client, project, monkeypatch):
        # Ensure template fallback path is used
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "heading" in data
        assert "body" in data
        assert data["heading"] == ""

    def test_default_template_missing(self, client, project, monkeypatch):
        # Point both template paths at non-existent files so the loader falls
        # all the way through to "" body.
        import web_ui.app as app_module
        monkeypatch.setattr(
            app_module,
            "_TRANSLATOR_NOTE_TEMPLATE_PATH",
            project / "does-not-exist.txt",
        )
        monkeypatch.setattr(
            app_module,
            "_TRANSLATOR_NOTE_TEMPLATE_EXAMPLE_PATH",
            project / "does-not-exist.example.txt",
        )
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data == {"heading": "", "body": ""}

    def test_falls_back_to_example_when_user_file_missing(
        self, client, project, monkeypatch, tmp_path
    ):
        import web_ui.app as app_module
        example = tmp_path / "example.txt"
        example.write_text("From example", encoding="utf-8")
        monkeypatch.setattr(
            app_module,
            "_TRANSLATOR_NOTE_TEMPLATE_PATH",
            tmp_path / "missing.txt",
        )
        monkeypatch.setattr(
            app_module,
            "_TRANSLATOR_NOTE_TEMPLATE_EXAMPLE_PATH",
            example,
        )
        data = client.get("/api/project/p1/translator-note").get_json()
        assert data["body"] == "From example"

    def test_user_file_takes_precedence_over_example(
        self, client, project, monkeypatch, tmp_path
    ):
        import web_ui.app as app_module
        user = tmp_path / "user.txt"
        user.write_text("From user", encoding="utf-8")
        example = tmp_path / "example.txt"
        example.write_text("From example", encoding="utf-8")
        monkeypatch.setattr(
            app_module, "_TRANSLATOR_NOTE_TEMPLATE_PATH", user
        )
        monkeypatch.setattr(
            app_module, "_TRANSLATOR_NOTE_TEMPLATE_EXAMPLE_PATH", example
        )
        data = client.get("/api/project/p1/translator-note").get_json()
        assert data["body"] == "From user"

    def test_returns_persisted_values(self, client, project):
        (project / "translator_note.json").write_text(
            json.dumps({"heading": "H", "body": "B"}),
            encoding="utf-8",
        )
        rv = client.get("/api/project/p1/translator-note")
        data = rv.get_json()
        assert data == {"heading": "H", "body": "B"}

    def test_corrupt_json_renamed_and_defaults(self, client, project):
        bad = project / "translator_note.json"
        bad.write_text("{ not json", encoding="utf-8")
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 200
        # Original file is renamed to .bak.<unix-ts>
        assert not bad.exists()
        backups = list(project.glob("translator_note.json.bak.*"))
        assert len(backups) == 1

    def test_project_not_found(self, client, tmp_path, monkeypatch):
        import web_ui.app as app_module
        monkeypatch.setattr(
            app_module, "_get_projects_dir", lambda: tmp_path / "nope"
        )
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 404

    def test_path_traversal_blocked(self, client, project):
        rv = client.get("/api/project/..%2Fevil/translator-note")
        # Either 400 (caught by safe_id) or 404 (Flask doesn't route).
        assert rv.status_code in (400, 404)

    def test_get_unsafe_id_returns_400(self, client, project):
        # `...` survives Flask routing but is rejected by _safe_id.
        rv = client.get("/api/project/.../translator-note")
        assert rv.status_code == 400

    def test_load_note_oserror_on_read_returns_defaults(
        self, client, project, monkeypatch
    ):
        # Create the file so .exists() is True, then force open() to raise OSError.
        (project / "translator_note.json").write_text("{}", encoding="utf-8")
        import builtins
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("translator_note.json") and (
                len(args) == 0 or "r" in str(args[0])
            ):
                raise OSError("simulated read failure")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["heading"] == ""
        # body comes from template fallback (could be non-empty if example present)
        assert "body" in data

    def test_load_note_rename_failure_still_returns_defaults(
        self, client, project, monkeypatch
    ):
        # Corrupt JSON + rename failure -> should still return defaults gracefully.
        bad = project / "translator_note.json"
        bad.write_text("{ not json", encoding="utf-8")
        from pathlib import Path as _Path
        real_rename = _Path.rename

        def fake_rename(self, target):
            if str(self).endswith("translator_note.json"):
                raise OSError("simulated rename failure")
            return real_rename(self, target)

        monkeypatch.setattr(_Path, "rename", fake_rename)
        rv = client.get("/api/project/p1/translator-note")
        assert rv.status_code == 200
        # Bad file remains because rename failed.
        assert bad.exists()
        data = rv.get_json()
        assert data["heading"] == ""


class TestPostTranslatorNote:
    def test_round_trip(self, client, project):
        rv = client.post(
            "/api/project/p1/translator-note",
            json={"heading": "Hello", "body": "World"},
        )
        assert rv.status_code == 200
        assert rv.get_json() == {"ok": True}

        on_disk = json.loads(
            (project / "translator_note.json").read_text(encoding="utf-8")
        )
        assert on_disk == {"heading": "Hello", "body": "World"}

        rv = client.get("/api/project/p1/translator-note")
        assert rv.get_json() == {"heading": "Hello", "body": "World"}

    def test_null_coerced_to_string(self, client, project):
        rv = client.post(
            "/api/project/p1/translator-note",
            json={"heading": None, "body": None},
        )
        assert rv.status_code == 200
        on_disk = json.loads(
            (project / "translator_note.json").read_text(encoding="utf-8")
        )
        assert on_disk == {"heading": "", "body": ""}

    def test_body_too_large(self, client, project):
        rv = client.post(
            "/api/project/p1/translator-note",
            json={"heading": "h", "body": "x" * 200_000},
        )
        assert rv.status_code == 400
        assert "100KB" in rv.get_json()["error"]

    def test_post_unsafe_id_returns_400(self, client, project):
        rv = client.post(
            "/api/project/.../translator-note",
            json={"heading": "h", "body": "b"},
        )
        assert rv.status_code == 400

    def test_post_project_not_found(self, client, tmp_path, monkeypatch):
        import web_ui.app as app_module
        monkeypatch.setattr(
            app_module, "_get_projects_dir", lambda: tmp_path / "nope"
        )
        rv = client.post(
            "/api/project/p1/translator-note",
            json={"heading": "h", "body": "b"},
        )
        assert rv.status_code == 404


class TestBuildEpubPersistsNote:
    def test_build_route_writes_note_to_disk(self, client, project):
        # The build itself will fail (no chunks), but the note save runs first.
        client.post(
            "/api/project/p1/build-epub",
            json={"translator_heading": "X", "translator_note": "Y"},
        )
        on_disk = json.loads(
            (project / "translator_note.json").read_text(encoding="utf-8")
        )
        assert on_disk == {"heading": "X", "body": "Y"}

    def test_build_route_rejects_oversize_note(self, client, project):
        rv = client.post(
            "/api/project/p1/build-epub",
            json={
                "translator_heading": "h",
                "translator_note": "x" * 200_000,
            },
        )
        assert rv.status_code == 400
        # No file should be written
        assert not (project / "translator_note.json").exists()
