"""Tests for the book page's pipeline phases (``web_ui.book_phases``).

Each phase is read off files the harness already writes: chunks for
translation, ``evaluations/`` for the checks and the judges, triage from the
pass's ledger (or derived as "nothing left to triage" without one), the book
review's state file (or its report), the image-pass ledger and the EPUB in the
project directory.
"""

from __future__ import annotations

import json
import os

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from src.triage.ledger import record_chapters
from web_ui.book_phases import (
    PHASE_KEYS,
    book_phases,
    book_triage_phase,
    clear_phase_cache,
)
from web_ui.evaluations import merge_judge_result, save_chunk_evaluation

SENTENCES = {
    "chapter_01": ["El gato duerme.", "El perro ladra."],
    "chapter_02": ["La casa es grande.", "El jardin es verde."],
    "chapter_03": ["El rio corre.", "La luna brilla."],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_phase_cache()
    yield
    clear_phase_cache()


@pytest.fixture
def book(tmp_path):
    """Three translated chapters, one chunk each, aligned; nothing run on them yet."""
    proj = tmp_path / "book"
    (proj / "chunks").mkdir(parents=True)
    (proj / "alignments").mkdir()
    for n, (ch, sentences) in enumerate(SENTENCES.items(), start=1):
        chunk_id = f"{ch}_chunk_000"
        (proj / "chunks" / f"{chunk_id}.json").write_text(json.dumps({
            "id": chunk_id,
            "chapter_id": ch,
            "source_text": "x",
            "translated_text": " ".join(sentences),
            "translated_at": f"2026-10-0{n}T12:00:00",
        }), encoding="utf-8")
        (proj / "alignments" / f"{ch}.json").write_text(json.dumps({
            "chapter_id": ch,
            "alignments": [
                {"es_idx": i, "en_idx": i, "es": s, "en": "x", "chunk_id": chunk_id}
                for i, s in enumerate(sentences)
            ],
        }), encoding="utf-8")
    return proj


def _phase(result, key):
    return next(p for p in result["phases"] if p["key"] == key)


def _code(proj, chapter, issues=()):
    save_chunk_evaluation(proj, f"{chapter}_chunk_000", results=[], aggregated={},
                          normalized_issues=list(issues),
                          enabled_evals=["dictionary", "grammar"])


def _judge(proj, chapter, name, when="2026-10-10T09:00:00"):
    merge_judge_result(proj, f"{chapter}_chunk_000", name, {
        "eval_name": name, "eval_version": "1.0.0", "issues": [], "executed_at": when,
    })


def _dictionary_finding(word="duerme", start=8):
    return NormalizedIssue(
        eval_name="dictionary", eval_version="1.0.0", issue_index=0, severity="warning",
        message=f"'{word}': not in dictionary", suggestion=None,
        location=NormalizedLocation(raw=f"char {start}", side="target", char_start=start,
                                    char_end=start + len(word), match=word),
    )


def _finish(proj):
    """Every phase run: coded, all three judges, a review, an image pass, an EPUB."""
    for ch in SENTENCES:
        _code(proj, ch)
        for name in ("dialogue", "address", "editorial"):
            _judge(proj, ch, name)
    (proj / ".harness" / "book_review").mkdir(parents=True)
    (proj / ".harness" / "book_review" / "state.json").write_text(
        json.dumps({"kind": "signals", "reviewed_at": "2026-10-24T23:01:06"}), encoding="utf-8")
    (proj / "images").mkdir()
    (proj / "images" / "cover.jpg").write_bytes(b"jpg")
    (proj / ".harness" / "images").mkdir(parents=True)
    (proj / ".harness" / "images" / "images.jsonl").write_text(
        json.dumps({"ts": "2026-10-12T18:36:07-04:00", "action": "apply", "image": "cover.jpg"})
        + "\n", encoding="utf-8")
    (proj / "Libro.epub").write_bytes(b"epub")


def test_every_phase_done(book):
    _finish(book)
    result = book_triage_phase(book)
    assert [p["key"] for p in result["phases"]] == list(PHASE_KEYS)
    assert {p["key"]: p["state"] for p in result["phases"]} == {k: "done" for k in PHASE_KEYS}
    assert result["done"] == result["total"] == 9
    assert result["missing"] == []
    assert _phase(result, "translation")["date"] == "2026-10-03"
    assert _phase(result, "editorial")["date"] == "2026-10-10"
    assert _phase(result, "book_review")["date"] == "2026-10-24"
    assert _phase(result, "images")["date"] == "2026-10-12"


def test_nothing_run_yet(book):
    result = book_phases(book)
    states = {p["key"]: p["state"] for p in result["phases"]}
    assert states["translation"] == "done"
    for key in ("checks", "dialogue", "address", "editorial", "book_review", "epub"):
        assert states[key] == "none"
    assert _phase(result, "checks")["owed"] == list(SENTENCES)


def test_partial_judges_list_the_chapters_owed(book):
    for ch in SENTENCES:
        _code(book, ch)
    _judge(book, "chapter_01", "editorial")
    _judge(book, "chapter_03", "editorial")
    editorial = _phase(book_phases(book), "editorial")
    assert editorial["state"] == "partial"
    assert (editorial["n"], editorial["N"]) == (2, 3)
    assert editorial["owed"] == ["chapter_02"]
    assert "editorial" in book_phases(book)["missing"]


def test_a_judge_shell_is_not_a_coded_run(book):
    # merge_judge_result writes `results: []` into the file it creates when a
    # judge runs before the coded evaluators; that must not read as coded.
    _judge(book, "chapter_01", "dialogue")
    checks = _phase(book_phases(book), "checks")
    assert checks["state"] == "none"
    assert _phase(book_phases(book), "dialogue")["n"] == 1


def test_untranslated_chapter_owes_translation(book):
    path = book / "chunks" / "chapter_02_chunk_000.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["translated_text"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    translation = _phase(book_phases(book), "translation")
    assert translation["state"] == "partial"
    assert translation["owed"] == ["chapter_02"]


def test_triage_is_unknown_until_computed(book):
    for ch in SENTENCES:
        _code(book, ch)
    result = book_phases(book)
    assert _phase(result, "triage")["state"] == "unknown"
    # Counted among the phases (8: no images here), but not reported missing
    # while unknown.
    assert result["total"] == 8 and "triage" not in result["missing"]

    book_triage_phase(book)
    assert _phase(book_phases(book), "triage")["state"] == "done"


def test_chapter_with_nothing_left_to_triage_counts_as_done(book):
    # chapter_01 has a dictionary finding nobody has ruled on; chapter_02 has
    # none at all and no _triage.jsonl row either, and is still triaged;
    # chapter_03 was never coded, so there was nothing to triage yet.
    _code(book, "chapter_01", [_dictionary_finding()])
    _code(book, "chapter_02")
    triage = _phase(book_triage_phase(book), "triage")
    assert triage["state"] == "partial"
    assert triage["owed"] == ["chapter_01", "chapter_03"]


def test_no_images_is_not_applicable(book):
    result = book_phases(book)
    assert _phase(result, "images")["state"] == "na"
    assert "images" not in result["missing"]
    assert result["total"] == 8


def test_a_cover_alone_still_owes_the_image_pass(book):
    (book / "images").mkdir()
    (book / "images" / "cover.jpg").write_bytes(b"jpg")
    images = _phase(book_phases(book), "images")
    assert images["state"] == "none"
    assert images["count"] == 1


def test_an_unreadable_image_ledger_reads_as_not_run(book):
    (book / "images").mkdir()
    (book / "images" / "cover.jpg").write_bytes(b"jpg")
    (book / ".harness" / "images").mkdir(parents=True)
    (book / ".harness" / "images" / "images.jsonl").write_bytes(bytes([0xFF, 0xFE]) + b" not utf-8\n")
    assert _phase(book_phases(book), "images")["state"] == "none"


def test_book_review_from_state_file(book):
    (book / ".harness" / "book_review").mkdir(parents=True)
    (book / ".harness" / "book_review" / "state.json").write_text(
        json.dumps({"reviewed_at": "2026-10-24T23:01:06"}), encoding="utf-8")
    review = _phase(book_phases(book), "book_review")
    assert (review["state"], review["date"]) == ("done", "2026-10-24")


def test_book_review_from_an_older_report(book):
    (book / "reports").mkdir()
    (book / "reports" / "book_review_2026-09-02.md").write_text("x", encoding="utf-8")
    (book / "reports" / "book_review_signals_2026-09-20.md").write_text("x", encoding="utf-8")
    (book / "reports" / "annotations_20261001_063015.md").write_text("x", encoding="utf-8")
    review = _phase(book_phases(book), "book_review")
    assert (review["state"], review["date"]) == ("done", "2026-09-20")


def test_cache_invalidates_when_an_evaluation_changes(book):
    for ch in SENTENCES:
        _code(book, ch)
    assert _phase(book_phases(book), "dialogue")["n"] == 0
    assert book_phases(book) is not None   # served from the cache

    _judge(book, "chapter_02", "dialogue")
    assert _phase(book_phases(book), "dialogue")["n"] == 1


def test_cache_invalidates_cached_triage_too(book):
    for ch in SENTENCES:
        _code(book, ch)
    book_triage_phase(book)
    assert _phase(book_phases(book), "triage")["state"] == "done"

    _code(book, "chapter_01", [_dictionary_finding()])
    # The files moved, so the cached triage no longer describes them.
    assert _phase(book_phases(book), "triage")["state"] == "unknown"


def test_epub_date_is_its_mtime(book):
    epub = book / "Libro.epub"
    epub.write_bytes(b"epub")
    stamp = 1_792_000_000  # 2026-10-14 (UTC)
    os.utime(epub, (stamp, stamp))
    epub_phase = _phase(book_phases(book), "epub")
    assert epub_phase["state"] == "done"
    assert epub_phase["date"].startswith("2026-10-")


# --- triage from the pass's own ledger ---------------------------------------

def _no_derivation(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("a book with a triage ledger must not derive triage")
    monkeypatch.setattr("src.triage.findings.collect_chapter", refuse)


def test_triage_reads_the_ledger_without_deriving_it(book, monkeypatch):
    _no_derivation(monkeypatch)
    for ch in SENTENCES:
        _code(book, ch)
    record_chapters(book, ["chapter_01", "chapter_02"], "2099-01-01T00:00:00", complete=True)
    result = book_phases(book)
    triage = _phase(result, "triage")
    assert triage["state"] == "partial"
    assert triage["owed"] == ["chapter_03"]
    assert triage["date"] == "2099-01-01"
    assert "triage" in result["missing"]
    # The Proceso tab's fetch answers the same thing, just as cheaply.
    assert _phase(book_triage_phase(book), "triage") == triage


def test_a_checker_run_after_the_stamp_makes_the_chapter_owed(book, monkeypatch):
    _no_derivation(monkeypatch)
    for ch in SENTENCES:
        _code(book, ch)
    record_chapters(book, list(SENTENCES), "2099-01-01T00:00:00", complete=True)
    assert _phase(book_phases(book), "triage")["state"] == "done"

    # Stamped before the checkers last ran (the evaluations are dated now).
    (book / ".harness" / "triage" / "state.json").write_text(json.dumps({"complete": True, "chapters": {
        ch: "2000-01-01T00:00:00" for ch in SENTENCES}}), encoding="utf-8")
    triage = _phase(book_phases(book), "triage")
    assert triage["state"] == "none"
    assert triage["owed"] == list(SENTENCES)


def test_a_stamped_chapter_never_coded_is_still_owed(book, monkeypatch):
    _no_derivation(monkeypatch)
    record_chapters(book, ["chapter_01"], "2099-01-01T00:00:00", complete=True)
    assert _phase(book_phases(book), "triage")["state"] == "none"


def test_without_a_ledger_triage_is_still_derived(book):
    for ch in SENTENCES:
        _code(book, ch)
    assert _phase(book_phases(book), "triage")["state"] == "unknown"
    assert _phase(book_triage_phase(book), "triage")["state"] == "done"


def test_an_incomplete_ledger_is_not_read(book):
    """Stamps from a chapter-scoped run alone leave the book on the derived check."""
    for ch in SENTENCES:
        _code(book, ch)
    record_chapters(book, ["chapter_01"], "2099-01-01T00:00:00")
    assert _phase(book_phases(book), "triage")["state"] == "unknown"
    assert _phase(book_triage_phase(book), "triage")["state"] == "done"
