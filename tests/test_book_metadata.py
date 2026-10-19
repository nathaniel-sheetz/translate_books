"""Tests for src.book_metadata — the one metadata resolver every EPUB build uses."""

import json

from src.book_metadata import epub_kwargs, load_book_metadata


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_empty_project_resolves_blanks(tmp_path):
    meta = load_book_metadata(tmp_path)
    assert meta["title"] == ""
    assert meta["author"] == ""
    assert meta["language"] == "es"
    assert meta["subjects"] == []
    assert meta["front_matter"] is None
    assert meta["edition_notice_body"] == ""


def test_spanish_title_beats_project_title_and_harness_title(tmp_path):
    _write(tmp_path / "project.json", {"title": "Old", "spanish_title": "Bambi", "author": "Salten"})
    _write(tmp_path / ".harness" / "config.json", {"title": "Bambi: A Life", "author": "Other"})
    meta = load_book_metadata(tmp_path)
    assert meta["title"] == "Bambi"
    assert meta["author"] == "Salten"


def test_harness_config_is_the_fallback(tmp_path):
    _write(tmp_path / ".harness" / "config.json",
           {"title": "The Story-Book", "author": "Fabre", "language_code": "es"})
    meta = load_book_metadata(tmp_path)
    assert meta["title"] == "The Story-Book"
    assert meta["author"] == "Fabre"


def test_ingest_report_is_the_last_fallback(tmp_path):
    _write(tmp_path / "ingest_report.json", {"metadata": {"title": "Horses", "creator": "Anne"}})
    meta = load_book_metadata(tmp_path)
    assert (meta["title"], meta["author"]) == ("Horses", "Anne")


def test_optional_fields_and_sidecars(tmp_path):
    _write(tmp_path / "project.json", {
        "spanish_title": "Bambi", "subtitle": "Una vida en el bosque",
        "translator": "T. Name", "subjects": ["Nature stories", " ", "Deer -- Fiction"],
        "publication_date": "2026-10-19", "front_matter": {"title_page": True},
        "target_lang_code": "es",
    })
    _write(tmp_path / "translator_note.json", {"heading": "Nota", "body": "Hola."})
    _write(tmp_path / "edition_notice.json", {"heading": "Créditos", "body": "Texto."})
    meta = load_book_metadata(tmp_path)
    assert meta["subtitle"] == "Una vida en el bosque"
    assert meta["subjects"] == ["Nature stories", "Deer -- Fiction"]
    assert meta["date"] == "2026-10-19"
    assert meta["front_matter"] == {"title_page": True}
    assert (meta["translator_note_heading"], meta["translator_note_body"]) == ("Nota", "Hola.")
    assert (meta["edition_notice_heading"], meta["edition_notice_body"]) == ("Créditos", "Texto.")


def test_cover_spec_subtitle_is_not_used(tmp_path):
    """Opt-in only: a cover's subtitle never reaches a book that did not ask for it."""
    _write(tmp_path / "cover" / "spec.json", {"title": "Bambi", "subtitle": "Una vida"})
    assert load_book_metadata(tmp_path)["subtitle"] == ""


def test_corrupt_json_is_treated_as_missing(tmp_path):
    (tmp_path / "project.json").write_text("{not json", encoding="utf-8")
    assert load_book_metadata(tmp_path)["title"] == ""


def test_epub_kwargs_override_wins_and_blanks_become_none(tmp_path):
    _write(tmp_path / "project.json", {"spanish_title": "Bambi", "author": "Salten"})
    kwargs = epub_kwargs(tmp_path, title="Override", author=None, language="")
    assert kwargs["title"] == "Override"
    assert kwargs["author"] == "Salten"
    assert kwargs["language"] == "es"
    assert kwargs["translator"] is None
    assert kwargs["subjects"] is None
