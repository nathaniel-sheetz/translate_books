"""Tests for src.kdp.facts and src.kdp.gutenberg — what is known before drafting."""

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from src.kdp import facts as kdp_facts
from src.kdp import gutenberg

FIXTURES = Path(__file__).parent / "fixtures" / "kdp"
TODAY = date(2026, 10, 19)


@pytest.mark.parametrize("url, expected", [
    ("https://www.gutenberg.org/cache/epub/72577/pg72577-images.html", 72577),
    ("https://www.gutenberg.org/files/3048/3048-h/3048-h.htm", 3048),
    ("https://www.gutenberg.org/ebooks/1342", 1342),
    ("https://www.gutenberg.org/epub/72577/pg72577.epub", 72577),
    ("https://example.com/book/12", None),
    (None, None),
])
def test_gutenberg_id(url, expected):
    assert gutenberg.gutenberg_id(url) == expected


def test_parse_rdf():
    record = gutenberg.parse_rdf((FIXTURES / "gutenberg_72577.rdf").read_text(encoding="utf-8"))
    assert record["title"] == "Bambi: A life in the woods"
    assert record["lcsh"] == ["Nature stories", "Deer -- Fiction"]
    assert record["lcc"] == ["PT"]
    assert "Nature/Gardening/Animals" in record["bookshelves"]
    assert record["source_edition_year"] == 1928
    assert "automatically generated" not in record["summary"]
    roles = {a["role"]: a["death"] for a in record["agents"]}
    assert roles == {"author": 1945, "introduction": 1933, "illustrator": 1974, "translator": 1961}


def test_rights_flags():
    record = gutenberg.parse_rdf((FIXTURES / "gutenberg_72577.rdf").read_text(encoding="utf-8"))
    flags = gutenberg.rights_flags(record, today=TODAY)
    codes = sorted(f["code"] for f in flags)
    # Salten 1945 and Galsworthy 1933 are past 70 but inside Mexico's 100;
    # Wiese 1974 and Chambers 1961 are inside 70.
    assert codes == ["life_plus_70", "life_plus_70", "mexico_life_plus_100", "mexico_life_plus_100"]
    assert any("2032" in f["detail"] for f in flags)


def test_rights_flags_us_term_and_unknown_death():
    record = {"agents": [{"role": "author", "name": "A", "death": None}], "source_edition_year": 1935}
    codes = sorted(f["code"] for f in gutenberg.rights_flags(record, today=TODAY))
    assert codes == ["unknown_death", "us_term"]
    assert gutenberg.rights_flags(None) == []


def test_fetch_rdf_reads_the_cache(tmp_path):
    shutil.copy(FIXTURES / "gutenberg_72577.rdf", tmp_path / "gutenberg_72577.rdf")
    record, error = gutenberg.fetch_rdf(72577, tmp_path)
    assert error is None and record["lcc"] == ["PT"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "book"
    (project / ".harness" / "images").mkdir(parents=True)
    (project / ".harness" / "kdp").mkdir(parents=True)
    (project / "images").mkdir()
    (project / "chunks").mkdir()
    shutil.copy(FIXTURES / "gutenberg_72577.rdf", project / ".harness" / "kdp" / "gutenberg_72577.rdf")
    (project / "project.json").write_text(json.dumps({
        "spanish_title": "Bambi: Una vida en el bosque", "author": "Felix Salten",
        "translator": "Ana Perez", "source_title": "Bambi",
        "gutenberg_url": "https://www.gutenberg.org/cache/epub/72577/pg72577-images.html",
    }), encoding="utf-8")
    (project / ".harness" / "config.json").write_text(json.dumps(
        {"locale": "mx", "provider": "anthropic", "model": "claude-sonnet-5"}), encoding="utf-8")
    (project / "cover").mkdir()
    (project / "cover" / "spec.json").write_text(json.dumps(
        {"title": "Bambi", "subtitle": "Una vida en el bosque"}), encoding="utf-8")
    for name in ("cover.jpg", "i001.jpg", "i002.png", "i003.jpg"):
        (project / "images" / name).write_bytes(b"x")
    (project / "chunks" / "chapter_01_chunk_001.json").write_text(
        json.dumps({"translated_text": "Uno dos tres."}), encoding="utf-8")
    annotations = [
        {"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote", "content": "a", "timestamp": "1", "origin": "gutenberg"},
        {"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote", "content": "b", "timestamp": "2", "origin": "footnote_pass"},
        {"chapter_id": "chapter_01", "es_idx": 3, "type": "footnote", "content": "c", "timestamp": "3"},
        {"chapter_id": "chapter_01", "es_idx": 4, "type": "word_choice", "content": "d", "timestamp": "4"},
        {"chapter_id": "chapter_01", "es_idx": 3, "removed": True, "timestamp": "5"},
    ]
    (project / "annotations.jsonl").write_text(
        "\n".join(json.dumps(a) for a in annotations) + "\n", encoding="utf-8")
    ledger = [
        {"action": "apply", "image": "cover.jpg", "mode": "cover", "created": False},
        {"action": "apply", "image": "i001.jpg", "mode": "translate", "model": "gpt-image"},
        {"action": "apply", "image": "i002.png", "mode": "translate"},
        {"action": "revert", "image": "i002.png", "mode": "translate"},
        {"action": "backfill", "image": "i003.jpg"},
    ]
    (project / ".harness" / "images" / "images.jsonl").write_text(
        "\n".join(json.dumps(r) for r in ledger) + "\n", encoding="utf-8")
    return project


def test_gather(tmp_path, monkeypatch):
    monkeypatch.setattr(kdp_facts, "load_defaults", lambda: {
        "translator": "Ana M. Pérez", "publisher": "Libros",
        "descriptors": {"translated": "(Traducido)"},
        "edition_notice": {"heading": "Créditos", "lines": [
            "Título original: {source_title}", "Publicado originalmente en {original_year}.",
            "{edition_additions}", "{publisher}"]},
        "_source": "test",
    })
    project = _project(tmp_path)
    facts = kdp_facts.gather(project, today=TODAY)

    assert facts["titles"]["cover_subtitle"] == "Una vida en el bosque"
    assert facts["source"]["gutenberg_id"] == 72577
    assert facts["catalog"]["lcsh"] == ["Nature stories", "Deer -- Fiction"]
    assert facts["footnotes"] == {"total": 2, "imported_from_source": 1, "editorial": 1,
                                  "by_origin": {"gutenberg": 1, "footnote_pass": 1}}
    images = facts["images"]
    assert images["new_cover"] and not images["cover_made_from_nothing"]
    assert images["translated_lettering"] == 1  # i002 was reverted, i003 only backfilled
    assert images["count"] == 3 and images["models"] == ["gpt-image"]
    assert facts["language"]["dialect"] == "mexican_spanish"
    assert facts["size"]["translated_words"] == 3
    assert facts["edition"]["descriptor"] == "(Traducido)"
    assert facts["edition"]["additions"] == (
        "Esta edición incluye una traducción nueva al español de México, 1 nota del traductor, "
        "una portada nueva y 1 ilustración con el texto traducido al español.")
    # original_year is unknown, so its line is dropped from the notice draft.
    assert facts["edition"]["notice_draft"]["body"].split("\n\n") == [
        "Título original: Bambi", facts["edition"]["additions"], "Libros"]
    needs = {n["field"] for n in facts["needs_input"]}
    assert needs == {"original_year", "translator"}
    assert (project / ".harness" / "kdp" / "facts.json").exists()


def test_descriptor_key_thresholds():
    assert kdp_facts.descriptor_key({"editorial": 9}, {"new_illustrations": 9}) == "translated"
    assert kdp_facts.descriptor_key({"editorial": 10}, {}) == "translated+annotated"
    assert kdp_facts.descriptor_key({"editorial": 60}, {"new_illustrations": 12}) == \
        "translated+annotated+illustrated"


def test_edition_additions_names_the_house_variety():
    footnotes, images = {"editorial": 3}, {"new_cover": True}
    assert kdp_facts.edition_additions(footnotes, images, "mexican_spanish").startswith(
        "Esta edición incluye una traducción nueva al español de México, 3 notas")
    assert kdp_facts.edition_additions(
        footnotes, images, "mexican_spanish", "español latino",
    ).startswith("Esta edición incluye una traducción nueva al español latino,")


def test_authorship_measures_drafts_and_saves(tmp_path):
    project = tmp_path / "book"
    (project / "chunks").mkdir(parents=True)
    (project / ".harness" / "translate").mkdir(parents=True)
    (project / "chunks" / "chapter_01_chunk_000.json").write_text(json.dumps(
        {"translated_text": "uno dos tres cuatro cinco seis siete ocho nueve diez"}), encoding="utf-8")
    (project / ".harness" / "translate" / "chapter_01_chunk_000.draft.txt").write_text(
        "uno dos TRES cuatro cinco seis siete ocho nueve diez", encoding="utf-8")
    rows = [{"corrected_es": "a"}, {"corrected_es": "b", "from_finding": {"mode": "edit"}},
            {"source": "judge:dialogue"}, {"source": "manual"}, {"status": "skipped"}]
    (project / "corrections_applied.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    record = kdp_facts._authorship(project)
    assert record["changed_words"] == 1 and record["changed_fraction"] == 0.1
    assert record["reader_saves_typed"] == 2 and record["reader_saves_from_finding"] == 1
    assert record["model_fixes"] == 1
