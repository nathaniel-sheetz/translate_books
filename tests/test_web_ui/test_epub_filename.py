"""Tests for ``web_ui.app._epub_filename``.

Regression cover for a Windows-only data-loss bug: the build-epub route derived
its output filename with ``Path(title).name``, which strips directory components
but leaves ``:`` intact. On NTFS a colon is the alternate-data-stream separator,
so a book titled "Bambi: Una vida en el bosque" wrote a complete 2.8MB EPUB into
a hidden stream on a 0-byte file named ``Bambi``. Neither ``epub_status`` nor
``download_epub`` can see a stream -- both use ``glob("*.epub")`` -- so the build
reported success while the UI went on serving the previous build indefinitely.

The colon case is the one that actually bit; the rest of the reserved set is
covered because any of them would fail the same way (or raise OSError).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import _epub_filename, app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "bambi"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


class TestColonIsNeverWrittenLiterally:
    """The bug that motivated the helper."""

    def test_spanish_subtitle_colon_becomes_dash(self):
        assert (
            _epub_filename("Bambi: Una vida en el bosque", "bambi")
            == "Bambi- Una vida en el bosque.epub"
        )

    def test_result_has_no_stream_separator(self):
        name = _epub_filename("Bambi: Una vida en el bosque", "bambi")
        assert ":" not in name

    def test_written_file_is_visible_to_glob(self, tmp_path):
        """The real end-to-end property: the build lands where readers look."""
        name = _epub_filename("Bambi: Una vida en el bosque", "bambi")
        (tmp_path / name).write_bytes(b"PK\x03\x04")
        assert [p.name for p in tmp_path.glob("*.epub")] == [name]


class TestReservedCharacters:
    @pytest.mark.parametrize("char", ['<', '>', ':', '"', '|', '?', '*'])
    def test_each_reserved_char_collapses_to_dash(self, char):
        assert _epub_filename(f"A{char}B", "proj") == "A-B.epub"

    def test_control_characters_are_replaced(self):
        assert _epub_filename("A\x01B", "proj") == "A-B.epub"

    def test_ordinary_punctuation_is_preserved(self):
        assert _epub_filename("Ivanhoe, or The Knight's Tale!", "proj") == (
            "Ivanhoe, or The Knight's Tale!.epub"
        )

    def test_accented_characters_are_preserved(self):
        assert _epub_filename("El Campeón de Maravilla", "proj") == (
            "El Campeón de Maravilla.epub"
        )


class TestDriveSpecNotMistakenForAPath:
    """Sanitize before pathlib, or Windows eats the first character.

    ``Path("R: The Movie").name`` is ``" The Movie"`` -- pathlib reads a leading
    ``<letter>:`` as a drive spec. Substituting the colon first avoids it.
    """

    def test_single_letter_prefix_is_preserved(self):
        assert _epub_filename("R: The Movie", "proj") == "R- The Movie.epub"

    def test_two_character_title_with_colon(self):
        assert _epub_filename("A:B", "proj") == "A-B.epub"


class TestResultIsAlwaysOneContainedComponent:
    """The title is user input, so the result must never escape the project dir."""

    @pytest.mark.parametrize("title", [
        "a/b",
        "../../etc/passwd",
        "C:\\Windows\\system32\\evil",
        "sub/dir/book",
    ])
    def test_no_separator_survives(self, title):
        result = _epub_filename(title, "proj")
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result

    @pytest.mark.parametrize("title", ["../../etc/passwd", "C:\\Windows\\evil"])
    def test_join_stays_inside_the_project_dir(self, title, tmp_path):
        target = tmp_path / _epub_filename(title, "proj")
        assert target.resolve().parent == tmp_path.resolve()


class TestTrailingCharacters:
    """Windows strips trailing dots/spaces, so we must strip them first."""

    def test_trailing_period_removed(self):
        assert _epub_filename("The End.", "proj") == "The End.epub"

    def test_trailing_space_removed(self):
        assert _epub_filename("The End ", "proj") == "The End.epub"

    def test_leading_dot_is_preserved(self):
        assert _epub_filename(".NET in Action", "proj") == ".NET in Action.epub"


class TestFallbackToProjectId:
    def test_empty_title_falls_back(self):
        assert _epub_filename("", "bambi-a-life-in-the-woods") == (
            "bambi-a-life-in-the-woods.epub"
        )

    def test_dots_only_title_falls_back(self):
        # Dots are not reserved; rstrip(". ") empties a dots-only stem.
        # Reserved-only titles like "???" become "---.epub", not the fallback.
        assert _epub_filename("...", "proj") == "proj.epub"

    def test_reserved_only_title_does_not_fall_back(self):
        assert _epub_filename("???", "proj") == "---.epub"

    def test_whitespace_only_title_falls_back(self):
        assert _epub_filename("   ", "proj") == "proj.epub"


class TestNormalTitlesUnchanged:
    """The common case must not regress into slugs -- users see this filename."""

    def test_plain_title_keeps_its_capitalisation_and_spaces(self):
        assert _epub_filename("Understood Betsy", "understood-betsy") == (
            "Understood Betsy.epub"
        )


class TestBuildEpubRouteUsesHelper:
    """The route, not just the helper, must refuse to write a colon."""

    def test_colon_title_returns_glob_visible_filename(
        self, client, project, monkeypatch
    ):
        import src.epub_builder as eb_module

        def fake_build(*, output_path, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"PK\x03\x04")
            return SimpleNamespace(path=output_path, included=["chapter_01"])

        monkeypatch.setattr(eb_module, "build_epub_from_chunks", fake_build)

        chunks_dir = project / "chunks"
        chunks_dir.mkdir()
        (chunks_dir / "chapter_01_chunk_000.json").write_text(
            json.dumps({
                "id": "chapter_01_chunk_000",
                "chapter_id": "chapter_01",
                "position": 0,
                "source_text": "Hello.",
                "translated_text": "Hola.",
            }),
            encoding="utf-8",
        )

        rv = client.post(
            "/api/project/bambi/build-epub",
            json={"title": "Bambi: Una vida en el bosque", "author": "Salten"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert ":" not in data["filename"]
        assert data["filename"] == "Bambi- Una vida en el bosque.epub"
        assert [p.name for p in project.glob("*.epub")] == [data["filename"]]
