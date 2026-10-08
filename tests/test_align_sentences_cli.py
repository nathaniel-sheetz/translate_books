"""CLI helpers for scripts/align_sentences.py."""

import json
import sys
from pathlib import Path

import pytest

from scripts import align_sentences as cli
from scripts.align_sentences import _print_coverage_gaps
from src.annotations import reanchor, store

CH = "chapter_01"


def test_print_coverage_gaps_silent_when_empty(capsys):
    _print_coverage_gaps({"gaps": []})
    _print_coverage_gaps({})
    assert capsys.readouterr().out == ""


def test_print_coverage_gaps_reports_each_run(capsys):
    _print_coverage_gaps({
        "chunk_id": "chapter_01_chunk_000",
        "gaps": [
            {
                "position": "tail",
                "en_start": 45,
                "en_end": 47,
                "sentences": 3,
                "chars": 749,
                "preview": "Richard was bidden to greet them…",
                "chunk_id": "chapter_01_chunk_000",
            },
        ],
    })
    out = capsys.readouterr().out
    assert "COVERAGE GAPS: 1" in out
    assert "chapter_01_chunk_000 tail" in out
    assert "749 chars" in out
    assert "EN 45-47" in out
    assert "Richard was bidden" in out


# main() moves a chapter's reader notes when, and only when, the run rewrites
# the project's own alignment. The aligner itself is stubbed.


def _write_alignment(path: Path, sentences: list[str]) -> None:
    rows = [{"es_idx": i, "en_idx": i, "es": es, "en": "en"} for i, es in enumerate(sentences)]
    path.write_text(
        json.dumps({"chapter_id": CH, "alignments": rows}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def book(tmp_path, monkeypatch):
    """A project with a two-chunk chapter, an alignment, and a note on "El perro."
    at row 1. The stubbed aligner writes one new sentence above it."""
    project = tmp_path / "book"
    (project / "chunks").mkdir(parents=True)
    (project / "alignments").mkdir()
    for pos in (0, 1):
        (project / "chunks" / f"{CH}_chunk_{pos:03d}.json").write_text("{}", encoding="utf-8")
    _write_alignment(project / "alignments" / f"{CH}.json", ["El gato.", "El perro."])
    store.append_record(project, {
        "project_id": "book",
        "chapter_id": CH,
        "es_idx": 1,
        "sub_id": "u1",
        "type": "word_choice",
        "content": "nota",
        "es_text": "El perro.",
        "timestamp": "2026-09-12T15:00:00",
    })

    def fake_align(chunk_paths, project_id, chapter_id, source_lang, target_lang, output_path):
        _write_alignment(Path(output_path), ["Nueva.", "El gato.", "El perro."])
        return {
            "en_count": 3, "es_count": 3, "high_confidence_pct": 100.0,
            "avg_similarity": 0.9, "alignments": [],
        }

    monkeypatch.setattr(cli, "align_chapter_chunks", fake_align)
    return project


def _run(monkeypatch, capsys, book, *extra):
    chunks = sorted(str(p) for p in (book / "chunks").glob("*.json"))
    monkeypatch.setattr(sys, "argv", ["align_sentences.py", *chunks, *map(str, extra)])
    cli.main()
    return capsys.readouterr().out


def _note_rows(book):
    return [r["es_idx"] for r in store.load_active(book, chapter_id=CH)]


def test_the_default_output_moves_the_notes(book, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, book)
    assert _note_rows(book) == [2]
    assert "Annotations: 1 moved, 0 orphaned" in out


def test_an_explicit_path_to_the_projects_alignment_moves_them_too(book, monkeypatch, capsys):
    _run(monkeypatch, capsys, book, "--output", book / "alignments" / f"{CH}.json")
    assert _note_rows(book) == [2]


def test_a_copy_written_elsewhere_leaves_the_notes_alone(book, tmp_path, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, book, "--output", tmp_path / "copy.json")
    assert (tmp_path / "copy.json").exists()
    assert _note_rows(book) == [1]
    assert "Annotations:" not in out


def test_a_failed_reanchor_is_said_out_loud(book, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(reanchor, "reanchor_chapter", boom)

    out = _run(monkeypatch, capsys, book)

    assert "Written to:" in out
    assert "re-anchor FAILED" in out
    assert _note_rows(book) == [1]
