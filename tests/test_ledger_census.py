"""Tests for scripts/ledger_census.py — the Phase 0 ledger count and audit export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.ledger_census as census


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8",
    )


def _reader(before="El gato.", after="El gatito.", **kw):
    row = {
        "project_id": "book", "chapter_id": "ch01", "es_idx": 0,
        "original_es": before, "corrected_es": after, "en_reference": "The cat.",
        "timestamp": "2026-05-01T10:00:00", "chunk_id": "ch01_chunk_000",
        "applied_at": "2026-05-01T10:05:00", "status": "applied",
    }
    row.update(kw)
    return row


def _judge():
    return {
        "chunk_id": "ch01_chunk_000", "chapter_id": "ch01", "es_idx": None,
        "original_es": "—Hola —dijo.", "corrected_es": "—Hola —dijo—.",
        "source": "judge:dialogue", "rule": "inciso-punctuation",
        "applied_at": "2026-06-01T00:00:00",
    }


def _write_alignment(project: Path, chapter: str, n: int) -> None:
    path = project / "alignments" / f"{chapter}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"es_idx": i, "en_idx": i, "es": f"es {i}", "en": f"en {i}"} for i in range(n)]
    path.write_text(json.dumps({"chapter_id": chapter, "alignments": rows}), encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "projects"
    book = root / "book"
    _write_jsonl(book / "corrections_applied.jsonl", [
        _reader(),
        _reader(),  # saved twice before Apply: one edit, two rows
        _reader(before="El perro.", after="El perrito.", status="skipped"),
        _reader(before="La vaca.", after="La vaquita.", verified_by="native"),
        _judge(),
    ])
    _write_alignment(book, "ch01", 1500)
    _write_alignment(book, "ch02", 500)
    _write_jsonl(book / "retranslations.jsonl", [{"new_translation": "x"}, {"new_translation": "y"}])

    _write_jsonl(
        root / ".published" / "other-book" / "corrections_applied.jsonl",
        [_reader(before="Uno.", after="Una.")],
    )
    # None of these is counted: a parked book, a snapshot, a copy nested in a book.
    _write_jsonl(root / ".backburner" / "parked-book" / "corrections_applied.jsonl", [_reader()])
    _write_jsonl(root / ".published" / "book.bak-migration" / "corrections_applied.jsonl", [_reader()])
    _write_jsonl(book / ".harness" / "archive" / "corrections_applied.jsonl", [_reader()])
    return root


def test_discovery_finds_top_level_and_grouped_books_only(root):
    found = [p.relative_to(root).as_posix() for p in census.discover_projects(root)]
    assert found == [".published/other-book", "book"]


def test_counts_separate_repeats_skips_and_automated_rows(root):
    stats, _ = census.census_project(root, root / "book")
    assert stats["reader_rows"] == 4
    assert stats["skipped"] == 1
    assert stats["duplicates"] == 1
    assert stats["unique_edits"] == 2
    assert stats["automated_rows"] == 1
    assert stats["aligned_rows"] == 2000
    assert stats["per_1k"] == 1.0
    assert stats["retranslations"] == 2
    assert stats["native"] == 1


def test_a_book_without_alignments_has_no_rate(root):
    stats, _ = census.census_project(root, root / ".published" / "other-book")
    assert stats["aligned_rows"] == 0
    assert stats["per_1k"] is None


def test_total_rate_leaves_out_books_without_alignments(root):
    stats = [census.census_project(root, p)[0] for p in census.discover_projects(root)]
    totals = census.summarize(stats)
    assert totals["unique_edits"] == 3
    # other-book's edit has no denominator, so it must not inflate the rate.
    assert totals["per_1k"] == 1.0


def test_unparseable_ledger_lines_are_counted_not_fatal(root):
    ledger = root / "book" / "corrections_applied.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8") + "{not json\n\n", encoding="utf-8")
    stats, _ = census.census_project(root, root / "book")
    assert stats["malformed_lines"] == 1
    assert stats["reader_rows"] == 4


def test_export_carries_the_triple_and_defaults_verified_by(root):
    _, rows = census.census_project(root, root / "book")
    by_before = {r["es_before"]: r for r in rows}
    assert set(by_before) == {"El gato.", "La vaca."}
    cat = by_before["El gato."]
    assert cat["project_id"] == "book"
    assert cat["en"] == "The cat."
    assert cat["es_after"] == "El gatito."
    assert cat["verified_by"] == "self"
    assert by_before["La vaca."]["verified_by"] == "native"


def test_audit_id_is_stable_and_distinct(root):
    first = [r["audit_id"] for r in census.census_project(root, root / "book")[1]]
    again = [r["audit_id"] for r in census.census_project(root, root / "book")[1]]
    assert first == again
    assert len(set(first)) == len(first)


def test_main_writes_the_report_and_the_export(root, tmp_path, capsys):
    report = tmp_path / "out" / "census.json"
    export = tmp_path / "out" / "audit.jsonl"
    rc = census.main([
        "--projects-root", str(root), "--json", str(report), "--export", str(export),
    ])
    assert rc == 0
    assert "TOTAL" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["totals"]["projects"] == 2
    assert len(export.read_text(encoding="utf-8").splitlines()) == 3


def test_project_filter_matches_a_grouped_slug(root, tmp_path):
    report = tmp_path / "one.json"
    rc = census.main([
        "--projects-root", str(root), "--project", "other-book", "--json", str(report),
    ])
    assert rc == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert [p["path"] for p in data["projects"]] == [".published/other-book"]


def test_unknown_project_exits_nonzero(root):
    assert census.main(["--projects-root", str(root), "--project", "nope"]) == 1


def test_project_repeats_and_every_slug_must_exist(root, tmp_path):
    report = tmp_path / "two.json"
    rc = census.main([
        "--projects-root", str(root), "--project", "book", "--project", "other-book",
        "--json", str(report),
    ])
    assert rc == 0
    assert json.loads(report.read_text(encoding="utf-8"))["totals"]["projects"] == 2
    assert census.main([
        "--projects-root", str(root), "--project", "book", "--project", "nope",
    ]) == 1


# --- --freeze ---------------------------------------------------------------


def _read(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _with_chunks(
    root: Path, tmp_path: Path,
    log_slug: str = "book", log_prompt: str = "Translate:\nThe cat.\nThe cow.\n",
) -> None:
    """Give `book` a chunk with a history log, and one whose log is gone."""
    log = tmp_path / "history" / "t1.json"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({
        "metadata": {
            "timestamp": "2026-04-30T09:00:00", "model": "m", "call_type": "translation",
            "chunk_id": "ch01_chunk_000", "project_slug": log_slug,
        },
        "prompt": log_prompt,
        "response": "El gato. La vaca flaca.",
    }), encoding="utf-8")
    chunks = root / "book" / "chunks"
    chunks.mkdir()
    (chunks / "ch01_chunk_000.json").write_text(json.dumps({
        "id": "ch01_chunk_000", "chapter_id": "ch01", "position": 0,
        "source_text": "The cat. The cow.", "translated_text": "El gatito. La vaquita.",
        "last_llm_log": str(log),
    }), encoding="utf-8")
    (chunks / "ch01_chunk_001.json").write_text(json.dumps({
        "id": "ch01_chunk_001", "chapter_id": "ch01", "position": 1,
        "source_text": "More.", "translated_text": "Más.",
        "last_llm_log": str(tmp_path / "history" / "gone.json"),
    }), encoding="utf-8")


def _freeze(root: Path, out: Path, *projects: str) -> int:
    argv = ["--projects-root", str(root), "--freeze", str(out)]
    for slug in projects:
        argv += ["--project", slug]
    return census.main(argv)


def test_freeze_writes_a_self_contained_snapshot(root, tmp_path):
    _with_chunks(root, tmp_path)
    out = tmp_path / "exam" / "2026-09-14"
    assert _freeze(root, out, "book") == 0

    edits = {e["es_before"]: e for e in _read(out / "book" / "edits.jsonl")}
    assert edits["El gato."]["before_in_original"] is True
    # "La vaca." had become "La vaca flaca." before the reader saw it.
    assert edits["La vaca."]["before_in_original"] is False

    chunks = _read(out / "book" / "chunks.jsonl")
    assert chunks[0]["original_translation"] == "El gato. La vaca flaca."
    assert chunks[0]["translation_at_freeze"] == "El gatito. La vaquita."
    assert chunks[0]["source_text"] == "The cat. The cow."
    assert chunks[0]["model"] == "m"
    assert chunks[1]["original_translation"] is None

    (book,) = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["books"]
    assert (book["edits"], book["edits_before_in_original"]) == (2, 1)
    assert (book["chunks"], book["chunks_missing_original"]) == (2, 1)
    assert book["sha256"]["edits.jsonl"] == census._sha256(out / "book" / "edits.jsonl")


def test_a_log_from_a_copy_of_the_book_is_still_the_original(root, tmp_path):
    # A chapter translated in an A/B copy and carried over names the copy's slug.
    _with_chunks(root, tmp_path, log_slug="book-ab")
    out = tmp_path / "exam"
    assert _freeze(root, out, "book") == 0
    chunk = _read(out / "book" / "chunks.jsonl")[0]
    assert chunk["original_translation"] == "El gato. La vaca flaca."
    assert chunk["log_project_slug"] == "book-ab"


def test_a_log_for_other_source_text_is_not_an_original(root, tmp_path):
    _with_chunks(root, tmp_path, log_prompt="Translate:\nSomething else entirely.\n")
    out = tmp_path / "exam"
    assert _freeze(root, out, "book") == 0
    assert _read(out / "book" / "chunks.jsonl")[0]["original_translation"] is None


def test_freeze_never_writes_into_an_existing_snapshot(root, tmp_path):
    out = tmp_path / "exam"
    (out / "book").mkdir(parents=True)
    (out / "book" / "edits.jsonl").write_text("old\n", encoding="utf-8")
    assert _freeze(root, out, "book") == 1
    assert (out / "book" / "edits.jsonl").read_text(encoding="utf-8") == "old\n"


def test_freeze_needs_a_project(root, tmp_path):
    with pytest.raises(SystemExit):
        _freeze(root, tmp_path / "exam")


def test_a_failed_freeze_leaves_nothing_behind(root, tmp_path):
    # other-book sorts first and freezes cleanly; book then fails on a bad chunk.
    _with_chunks(root, tmp_path)
    (root / "book" / "chunks" / "ch01_chunk_002.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "exam" / "2026-09-14"
    assert _freeze(root, out, "book", "other-book") == 1
    assert not out.exists()
    assert list(out.parent.iterdir()) == []


def test_freeze_fills_an_existing_empty_directory(root, tmp_path):
    out = tmp_path / "exam"
    out.mkdir()
    assert _freeze(root, out, "book") == 0
    assert (out / "manifest.json").exists()
    # The staging directory was renamed into place, not left beside it.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".exam.")] == []


def test_a_slug_shared_by_two_books_blocks_export_and_freeze(root, tmp_path):
    _write_jsonl(root / ".drafts" / "other-book" / "corrections_applied.jsonl", [_reader()])
    export = tmp_path / "audit.jsonl"
    out = tmp_path / "exam"
    assert census.main(["--projects-root", str(root), "--export", str(export)]) == 1
    assert not export.exists()
    assert _freeze(root, out, "other-book") == 1
    assert not out.exists()
    # The census alone keys rows by path, so it still runs.
    assert census.main(["--projects-root", str(root)]) == 0


@pytest.mark.parametrize("doc", [
    ["not", "an", "object"],
    {"metadata": "nope", "prompt": 42, "response": "El gato."},
    {"metadata": {"chunk_id": "ch01_chunk_000"}, "prompt": "The cat. The cow.", "response": ["El gato."]},
])
def test_a_malformed_log_is_no_original(root, tmp_path, doc):
    _with_chunks(root, tmp_path)
    (tmp_path / "history" / "t1.json").write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "exam"
    assert _freeze(root, out, "book") == 0
    assert _read(out / "book" / "chunks.jsonl")[0]["original_translation"] is None


def test_a_native_resave_of_an_edit_counts_as_native(root):
    ledger = root / ".published" / "other-book" / "corrections_applied.jsonl"
    _write_jsonl(ledger, [
        _reader(before="Uno.", after="Una."),
        _reader(before="Uno.", after="Una.", verified_by="native"),
    ])
    stats, rows = census.census_project(root, ledger.parent)
    assert (stats["unique_edits"], stats["duplicates"], stats["native"]) == (1, 1, 1)
    assert rows[0]["verified_by"] == "native"


def test_export_status_prefers_an_applied_copy_and_keeps_unstamped_null(root):
    ledger = root / ".published" / "other-book" / "corrections_applied.jsonl"
    legacy = _reader(before="Uno.", after="Una.")
    del legacy["status"]
    older = _reader(before="Dos.", after="Doce.")
    del older["status"]
    _write_jsonl(ledger, [legacy, older, _reader(before="Dos.", after="Doce.")])
    _, rows = census.census_project(root, ledger.parent)
    assert {r["es_before"]: r["status"] for r in rows} == {"Uno.": None, "Dos.": "applied"}
