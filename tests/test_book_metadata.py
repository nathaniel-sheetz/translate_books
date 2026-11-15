"""Tests for src.book_metadata — the one metadata resolver every EPUB build uses."""

import json

import pytest

from src.book_metadata import (
    epub_kwargs, export_site_texts, load_book_metadata, load_site_metadata, normalize_forms,
    unknown_forms,
)


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


def test_non_list_subjects_are_ignored(tmp_path):
    _write(tmp_path / "project.json", {"spanish_title": "Bambi", "subjects": 5})
    _write(tmp_path / "ingest_report.json", {"metadata": "not an object"})
    meta = load_book_metadata(tmp_path)
    assert meta["subjects"] == [] and meta["title"] == "Bambi"


def test_epub_kwargs_override_wins_and_blanks_become_none(tmp_path):
    _write(tmp_path / "project.json", {"spanish_title": "Bambi", "author": "Salten"})
    kwargs = epub_kwargs(tmp_path, title="Override", author=None, language="")
    assert kwargs["title"] == "Override"
    assert kwargs["author"] == "Salten"
    assert kwargs["language"] == "es"
    assert kwargs["translator"] is None
    assert kwargs["subjects"] is None


def test_normalize_forms_orders_dedupes_and_drops_unknown():
    assert normalize_forms(["III/IV", "ii", "II", "Form 7"]) == ["II", "III/IV"]
    assert normalize_forms("v/vi") == ["V/VI"]
    assert normalize_forms(None) == []
    assert unknown_forms(["I", "Form 7"]) == ["Form 7"]


def test_site_metadata_blank_when_unset(tmp_path):
    assert load_site_metadata(tmp_path) == {"forms": [], "adaptations": "", "parent_notes": ""}


def test_site_metadata_reads_project_json(tmp_path):
    _write(tmp_path / "project.json",
           {"forms": ["II", "I", "bogus"], "adaptations": "Notas.", "parent_notes": "Cuidado."})
    assert load_site_metadata(tmp_path) == {
        "forms": ["I", "II"], "adaptations": "Notas.", "parent_notes": "Cuidado."}


def test_site_fields_stay_out_of_epub_kwargs(tmp_path):
    _write(tmp_path / "project.json", {"spanish_title": "Bambi", "forms": ["I"],
                                       "adaptations": "A", "parent_notes": "P"})
    kwargs = epub_kwargs(tmp_path)
    assert not {"forms", "adaptations", "parent_notes"} & set(kwargs)


def _site_book(tmp_path, slug="bambi"):
    book_dir = tmp_path / "site" / "libros" / slug
    book_dir.mkdir(parents=True)
    (book_dir / "libro.toml").write_text('titulo = "Bambi"\n', encoding="utf-8")
    return book_dir


def test_export_site_texts_writes_both_files_into_the_slug_folder(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / "project.json", {"spanish_title": "Bambi", "adaptations": "- Sin abreviar.",
                                   "parent_notes": "**Muerte.** Cap. II."})
    book_dir = _site_book(tmp_path)
    result = export_site_texts(proj, tmp_path / "site")
    assert result["site_slug"] == "bambi"
    assert [(f["file"], f["action"]) for f in result["files"]] == [
        ("adaptaciones.md", "created"), ("padres.md", "created")]
    assert (book_dir / "adaptaciones.md").read_text(encoding="utf-8") == "- Sin abreviar.\n"
    assert (book_dir / "padres.md").read_text(encoding="utf-8") == "**Muerte.** Cap. II.\n"


def test_export_site_texts_reports_unchanged_and_updated(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / "project.json", {"spanish_title": "Bambi", "adaptations": "A", "parent_notes": "P"})
    book_dir = _site_book(tmp_path)
    (book_dir / "adaptaciones.md").write_bytes(b"A\r\n")  # a CRLF checkout of the same text
    (book_dir / "padres.md").write_text("old\n", encoding="utf-8")
    actions = {f["file"]: f["action"] for f in export_site_texts(proj, tmp_path / "site")["files"]}
    assert actions == {"adaptaciones.md": "unchanged", "padres.md": "updated"}
    assert (book_dir / "padres.md").read_text(encoding="utf-8") == "P\n"


def test_export_site_texts_blank_field_leaves_the_file_alone(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / "project.json", {"spanish_title": "Bambi", "adaptations": "A"})
    book_dir = _site_book(tmp_path)
    (book_dir / "padres.md").write_text("escrito a mano\n", encoding="utf-8")
    files = export_site_texts(proj, tmp_path / "site")["files"]
    assert files[1]["action"] == "skipped"
    assert (book_dir / "padres.md").read_text(encoding="utf-8") == "escrito a mano\n"


def test_export_site_texts_dry_run_writes_nothing(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / "project.json", {"spanish_title": "Bambi", "adaptations": "A", "parent_notes": "P"})
    book_dir = _site_book(tmp_path)
    result = export_site_texts(proj, tmp_path / "site", dry_run=True)
    assert [f["action"] for f in result["files"]] == ["created", "created"]
    assert not (book_dir / "adaptaciones.md").exists()


def test_export_site_texts_uses_site_slug_and_needs_the_folder(tmp_path):
    proj = tmp_path / "proj"
    _write(proj / "project.json", {"spanish_title": "Bambi", "site_slug": "bambi-vida",
                                   "adaptations": "A"})
    _site_book(tmp_path)  # folder "bambi", not "bambi-vida"
    with pytest.raises(FileNotFoundError, match="bambi-vida"):
        export_site_texts(proj, tmp_path / "site")
