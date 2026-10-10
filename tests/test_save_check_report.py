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


def test_the_model_s_answers_are_counted_per_model(tmp_path, monkeypatch, capsys):
    from src import save_check_model

    project_dir = tmp_path / "a-book"
    (project_dir / "alignments").mkdir(parents=True)
    (project_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps({"alignments": _rows("El gato se sentaron.")}, ensure_ascii=False), encoding="utf-8",
    )

    def verdict(flagged, model="gemma-4-31b", threshold=7.5):
        return save_check_model.Verdict(
            flagged=flagged, model=model, profile=model, prompt="v1", backend="llama-server",
            seconds=1.0, score=9.1 if flagged else -3.0, threshold=threshold)

    for answer in (verdict(True), verdict(False), verdict(False), verdict(False, "qwen3.8-27b", 4.0)):
        save_check_model.append_readout(project_dir, answer, chapter_id="chapter_01", es_idx=0)
    save_check.append_warning(
        project_dir, chapter_id="chapter_01", es_idx=0, path="reader", en="",
        es_before="El gato se sentó.", es_after="El gato se sentaron.",
        hits=[{"rule": "model", "text": "sentaron"}], model={"model": "gemma-4-31b", "score": 9.1},
    )

    monkeypatch.setattr(sys, "argv", ["save_check_report.py", "--projects-dir", str(tmp_path), "--list"])
    assert report.main() == 0
    out = capsys.readouterr().out
    assert "model:sentaron  (gemma-4-31b +9.1)" in out
    counted = {line.split()[0]: line.split()[1:] for line in out.splitlines()
               if line.startswith(("gemma-4-31b", "qwen3.8-27b"))}
    assert counted == {"gemma-4-31b": ["7.5", "3", "1", "33.3%", "1.00"],
                       "qwen3.8-27b": ["4", "1", "0", "0.0%", "1.00"]}


def test_a_model_that_has_flagged_nothing_is_still_reported(tmp_path, monkeypatch, capsys):
    from src import save_check_model

    monkeypatch.setattr(sys, "argv", ["save_check_report.py", "--projects-dir", str(tmp_path)])
    assert report.main() == 0
    assert "No save-check warnings have been logged." in capsys.readouterr().out

    project_dir = tmp_path / "a-book"
    project_dir.mkdir()
    answer = save_check_model.Verdict(
        flagged=False, model="gemma-4-31b", profile="gemma-4-31b", prompt="v1", backend="llama-server",
        seconds=1.0, score=-3.0, threshold=7.5)
    for _ in range(2):
        save_check_model.append_readout(project_dir, answer, chapter_id="chapter_01", es_idx=0)
    now = datetime.now().isoformat()
    _write_rows(project_dir / "corrections.jsonl", [{"timestamp": now}, {"timestamp": now}])

    assert report.main() == 0
    out = capsys.readouterr().out
    assert "No save-check warnings" not in out
    assert "2 saves across 1 book(s), 0 warnings in 0 book(s)" in out
    (row,) = [line.split()[1:] for line in out.splitlines() if line.startswith("gemma-4-31b")]
    assert row == ["7.5", "2", "0", "0.0%", "1.00"]
