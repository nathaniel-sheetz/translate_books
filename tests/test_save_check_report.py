"""Tests for the save-check report: a warning's state and the count of saves."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import save_check_report as report
from src import save_check
from src.models import IgnoredTerm, IgnoredTerms


SPELLING = {
    "id": "w1", "chapter_id": "chapter_01", "es_idx": 0,
    "es_after": "El gatto se sentó.", "hits": [{"rule": "spelling", "text": "gatto"}],
}
MARK = {
    "id": "w2", "chapter_id": "chapter_01", "es_idx": 0,
    "es_after": "Vino,, tarde.", "hits": [{"rule": "doubled_mark", "text": ",,"}],
}


def _rows(*sentences):
    return [{"es_idx": n, "es": es} for n, es in enumerate(sentences)]


class TestWarningState:
    def test_open_while_its_sentence_stands(self):
        assert report.warning_state(SPELLING, {}, _rows("El gatto se sentó."), None) == "open"

    def test_fixed_once_the_flagged_word_is_gone(self):
        assert report.warning_state(SPELLING, {}, _rows("El gato se sentó."), None) == "fixed"

    def test_a_rewritten_sentence_that_keeps_the_word_is_not_fixed(self):
        rows = _rows("Otra frase.", "El gatto se sentó ayer.")
        assert report.warning_state(SPELLING, {}, rows, None) == "open"

    def test_a_mark_cannot_be_looked_for_so_a_rewrite_counts_as_fixed(self):
        assert report.warning_state(MARK, {}, _rows("Vino,, tarde."), None) == "open"
        assert report.warning_state(MARK, {}, _rows("Vino, y muy tarde,, ayer."), None) == "fixed"

    @pytest.mark.parametrize("outcome", ["dismissed", "ignored"])
    def test_an_answered_warning_keeps_its_answer(self, outcome):
        outcomes = {"w1": {"id": "w1", "outcome": outcome}}
        assert report.warning_state(SPELLING, outcomes, _rows("El gato se sentó."), None) == outcome

    def test_ignored_when_its_word_is_on_the_book_s_list(self):
        ignored = IgnoredTerms(terms=[IgnoredTerm(term="gatto", eval_name="dictionary")])
        assert report.warning_state(SPELLING, {}, _rows("El gatto se sentó."), ignored) == "ignored"


def _write_rows(path: Path, rows: list[dict]):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_count_saves_takes_manual_writes_from_since(tmp_path):
    _write_rows(tmp_path / "corrections.jsonl", [
        {"timestamp": "2026-10-01T09:00:00"}, {"timestamp": "2026-10-08T09:00:00"},
    ])
    _write_rows(tmp_path / "corrections_applied.jsonl", [
        {"timestamp": "2026-10-08T10:00:00"},
        # Written by a judge apply, not by hand.
        {"timestamp": "2026-10-08T11:00:00", "source": "dialogue"},
    ])
    _write_rows(tmp_path / "retranslations.jsonl", [{"timestamp": "2026-10-09T09:00:00"}])
    assert report.count_saves(tmp_path, "2026-10-05") == 3
    assert report.count_saves(tmp_path, "2026-01-01") == 4


def test_a_warning_carried_forward_is_counted_once(tmp_path, monkeypatch, capsys):
    project_dir = tmp_path / "a-book"
    (project_dir / "alignments").mkdir(parents=True)
    (project_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps({"alignments": _rows("El gatto se sentó ayer.")}, ensure_ascii=False),
        encoding="utf-8",
    )
    hits = [{"rule": "spelling", "text": "gatto"}]
    first = save_check.append_warning(
        project_dir, chapter_id="chapter_01", es_idx=0, path="reader",
        en="", es_before="El gato se sentó.", es_after="El gatto se sentó.", hits=hits,
    )
    save_check.append_warning(
        project_dir, chapter_id="chapter_01", es_idx=0, path="reader", en="",
        es_before="El gatto se sentó.", es_after="El gatto se sentó ayer.", hits=hits,
        carried_from=first,
    )
    now = datetime.now().isoformat()
    _write_rows(project_dir / "corrections.jsonl", [{"timestamp": now}, {"timestamp": now}])

    monkeypatch.setattr(sys, "argv", ["save_check_report.py", "--projects-dir", str(tmp_path), "--list"])
    assert report.main() == 0
    out = capsys.readouterr().out
    assert "2 saves across 1 book(s), 1 warnings in 1 book(s)" in out
    listed = [line for line in out.splitlines() if "spelling:gatto" in line]
    assert [line.split()[0] for line in listed] == ["open"]
