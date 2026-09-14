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
