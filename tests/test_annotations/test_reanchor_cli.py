"""scripts/reanchor_annotations.py, driven through main(): a report by default,
and nothing written without ``--apply``."""

from __future__ import annotations

import sys

from scripts import reanchor_annotations as cli
from src.annotations import store

from tests.test_annotations.conftest import write_alignment, write_annotations

CH = "chapter_01"


def _note(es_idx, es_text=None, *, chapter=CH, sub_id="u1"):
    record = {
        "project_id": "testbook",
        "chapter_id": chapter,
        "es_idx": es_idx,
        "type": "word_choice",
        "content": "nota",
        "timestamp": "2026-09-12T15:00:00",
        "sub_id": sub_id,
    }
    if es_text is not None:
        record["es_text"] = es_text
    return record


def _align(project, sentences, chapter=CH):
    write_alignment(project, chapter, [(i, es, "en") for i, es in enumerate(sentences)])


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["reanchor_annotations.py", *map(str, argv)])
    rc = cli.main()
    return rc, capsys.readouterr().out


def _live(project, chapter=CH):
    return [(r["es_idx"], r.get("es_text")) for r in store.load_active(project, chapter_id=chapter)]


def test_a_dry_run_reports_the_move_and_writes_nothing(project, monkeypatch, capsys):
    _align(project, ["Nueva.", "El perro."])
    path = write_annotations(project, [_note(0, "El perro.")])
    before = path.read_bytes()

    rc, out = _run(monkeypatch, capsys, project)

    assert rc == 0
    assert "MOVE" in out and "0 -> 1" in out
    assert "1 to move" in out and "Dry run: nothing written" in out
    assert path.read_bytes() == before


def test_apply_moves_the_note(project, monkeypatch, capsys):
    _align(project, ["Nueva.", "El perro."])
    write_annotations(project, [_note(0, "El perro.")])

    rc, out = _run(monkeypatch, capsys, project, "--apply")

    assert rc == 0
    assert _live(project) == [(1, "El perro.")]
    # One tombstone and one row in its place.
    assert "Wrote 2 row(s)" in out


def test_a_note_with_no_saved_sentence_is_stamped_only_with_backfill(project, monkeypatch, capsys):
    _align(project, ["El gato.", "El perro."])
    write_annotations(project, [_note(1)])

    _, out = _run(monkeypatch, capsys, project, "--apply")
    assert "1 more have no saved sentence to check" in out
    assert _live(project) == [(1, None)]

    _, out = _run(monkeypatch, capsys, project, "--apply", "--backfill")
    assert "stamped with their current row" in out
    assert _live(project) == [(1, "El perro.")]


def test_chapter_limits_the_run_to_that_chapter(project, monkeypatch, capsys):
    _align(project, ["Nueva.", "El perro."])
    _align(project, ["Nueva.", "El gato."], chapter="chapter_02")
    write_annotations(project, [
        _note(0, "El perro."),
        _note(0, "El gato.", chapter="chapter_02"),
    ])

    _run(monkeypatch, capsys, project, "--apply", "--chapter", "chapter_02")

    assert _live(project) == [(0, "El perro.")]
    assert _live(project, "chapter_02") == [(1, "El gato.")]


def test_a_missing_project_is_an_error(tmp_path, monkeypatch, capsys):
    rc, _ = _run(monkeypatch, capsys, tmp_path / "no-such-book")
    assert rc == 1
