"""Marks survive a re-evaluation that moves their finding.

The coded checkers write absolute character offsets into ``location``, and
:func:`issue_key` hashes ``location``, so an edit above a finding re-keys it.
Before carry-forward, one save in the chunk editor re-opened every triaged and
dismissed finding below the edit (horses-of-destiny chapter 17: 8 of 8 verdicts
and the one human dismissal).

The fake checker below writes findings in the dictionary evaluator's own format
(``'word': reason (found N time(s))`` at ``Character position N``) so the keys
move exactly as they do in a real book.
"""

from __future__ import annotations

import json
import re

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from src.models import Chunk, ChunkMetadata, ChunkStatus, EvalResult
import web_ui.evaluations as ev
from web_ui.evaluations import (
    append_feedback,
    append_triage,
    build_dismissed,
    build_triaged,
    evaluate_and_persist_chunk,
    finding_anchor,
    is_dismissed,
    is_triaged,
    issue_key,
    load_all_triage_by_chunk,
    load_chunk_evaluation,
    load_feedback_for_chunk,
)

CHUNK_ID = "chapter_01_chunk_000"
FLAGGED = ("Concord", "Vermont")


def _chunk(text: str) -> Chunk:
    return Chunk(
        id=CHUNK_ID, chapter_id="chapter_01", position=0,
        source_text="Source.", translated_text=text,
        metadata=ChunkMetadata(char_start=0, char_end=7, overlap_start=0,
                               overlap_end=0, paragraph_count=1, word_count=1),
        status=ChunkStatus.TRANSLATED,
    )


def _dictionary_findings(text: str, words=FLAGGED) -> list[NormalizedIssue]:
    """One finding per flagged word, fanned out per occurrence, like the real one."""
    out: list[NormalizedIssue] = []
    index = 0
    for word in words:
        positions = [m.start() for m in re.finditer(rf"\b{word}\b", text)]
        if not positions:
            continue
        raw = (
            f"Character position {positions[0]}" if len(positions) == 1
            else f"Character positions: {', '.join(str(p) for p in positions)}"
        )
        message = f"'{word}': English word in translation (found {len(positions)} time(s))"
        for pos in positions:
            out.append(NormalizedIssue(
                eval_name="dictionary", eval_version="1.1.0", issue_index=index,
                severity="error", message=message, suggestion=None,
                location=NormalizedLocation(
                    raw=raw, side="target", char_start=pos,
                    char_end=pos + len(word), match=word,
                ),
            ))
        index += 1
    return out


@pytest.fixture
def checker(monkeypatch):
    """Stand in for the coded evaluators; ``words`` can be swapped per test."""
    state = {"words": FLAGGED}

    def fake(chunk, *, glossary=None, blacklist=None, enabled_evals=None):
        if enabled_evals is not None and "dictionary" not in enabled_evals:
            return [], {"total_issues": 0}, []
        result = EvalResult(
            eval_name="dictionary", eval_version="1.1.0", target_id=chunk.id,
            target_type="chunk", passed=False, score=0.5, issues=[], metadata={},
        )
        return [result], {"total_issues": 0}, _dictionary_findings(
            chunk.translated_text, state["words"],
        )

    monkeypatch.setattr(ev, "run_coded_evaluators", fake)
    return state


def _stored(project_dir) -> list[dict]:
    return load_chunk_evaluation(project_dir, CHUNK_ID)["normalized_issues"]


def _finding(project_dir, word: str) -> dict:
    return next(i for i in _stored(project_dir) if i["location"]["match"] == word)


def _suppress(project_dir, issue: dict, word: str) -> None:
    append_triage(
        project_dir, CHUNK_ID, "dictionary", issue["issue_index"], "suppress",
        key=issue_key("dictionary", issue), confidence=0.99,
        reason="place name", term=word, model="m", run_id="r1",
    )


def _hidden(project_dir, issue: dict) -> bool:
    triaged = build_triaged(load_all_triage_by_chunk(project_dir).get(CHUNK_ID, []))
    return is_triaged(triaged, "dictionary", issue)


def _dismissed(project_dir, issue: dict) -> bool:
    by_key, by_index = build_dismissed(load_feedback_for_chunk(project_dir, CHUNK_ID))
    return is_dismissed(by_key, by_index, "dictionary", issue["issue_index"], issue)


BASE = "Los Morgan fueron a Concord y luego a Vermont."


def test_an_edit_above_the_findings_keeps_their_marks(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    concord = _finding(tmp_path, "Concord")
    vermont = _finding(tmp_path, "Vermont")
    _suppress(tmp_path, concord, "Concord")
    append_feedback(
        tmp_path, CHUNK_ID, "dictionary", vermont["issue_index"], "false_positive",
        message=vermont["message"], key=issue_key("dictionary", vermont),
    )

    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))

    new_concord = _finding(tmp_path, "Concord")
    new_vermont = _finding(tmp_path, "Vermont")
    # The precondition that made this a bug: both keys moved.
    assert issue_key("dictionary", new_concord) != issue_key("dictionary", concord)
    assert issue_key("dictionary", new_vermont) != issue_key("dictionary", vermont)
    assert _hidden(tmp_path, new_concord)
    assert _dismissed(tmp_path, new_vermont)

    carried = load_all_triage_by_chunk(tmp_path)[CHUNK_ID][-1]
    assert carried["carried_from"] == issue_key("dictionary", concord)
    assert carried["verdict"] == "suppress" and carried["confidence"] == 0.99
    assert carried["reason"] == "place name" and carried["run_id"] == "r1"
    mark = load_feedback_for_chunk(tmp_path, CHUNK_ID)[-1]
    assert mark["carried_from"] == issue_key("dictionary", vermont)
    assert mark["feedback_type"] == "false_positive"


def test_a_resolved_mark_is_not_carried(tmp_path, checker):
    """`resolved` says the text was fixed; a finding still standing was not."""
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    concord = _finding(tmp_path, "Concord")
    append_feedback(
        tmp_path, CHUNK_ID, "dictionary", concord["issue_index"], "resolved",
        message=concord["message"], key=issue_key("dictionary", concord),
    )

    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))

    assert len(load_feedback_for_chunk(tmp_path, CHUNK_ID)) == 1
    assert not _dismissed(tmp_path, _finding(tmp_path, "Concord"))


def test_an_unchanged_rerun_writes_nothing(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    _suppress(tmp_path, _finding(tmp_path, "Concord"), "Concord")

    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))

    assert len(load_all_triage_by_chunk(tmp_path)[CHUNK_ID]) == 1


def test_a_changed_occurrence_count_still_carries(tmp_path, checker):
    text = "Vermont y Concord y Vermont."
    evaluate_and_persist_chunk(tmp_path, _chunk(text))
    _suppress(tmp_path, _finding(tmp_path, "Vermont"), "Vermont")

    evaluate_and_persist_chunk(tmp_path, _chunk("Vermont y Concord."))

    vermont = _finding(tmp_path, "Vermont")
    assert "(found 1 time(s))" in vermont["message"]
    assert _hidden(tmp_path, vermont)


def test_a_word_the_edit_removed_carries_nothing(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    _suppress(tmp_path, _finding(tmp_path, "Concord"), "Concord")

    evaluate_and_persist_chunk(tmp_path, _chunk("Los Morgan fueron a Vermont."))

    assert len(load_all_triage_by_chunk(tmp_path)[CHUNK_ID]) == 1


def test_a_newer_mark_on_the_new_key_is_not_overwritten(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    old = _finding(tmp_path, "Concord")
    _suppress(tmp_path, old, "Concord")

    # The new key already carries a `keep` (say, a re-triage landed first).
    moved = "Al principio, " + BASE
    new_issue = _dictionary_findings(moved)[0].to_dict()
    append_triage(
        tmp_path, CHUNK_ID, "dictionary", 0, "keep",
        key=issue_key("dictionary", new_issue), confidence=0.9,
    )

    evaluate_and_persist_chunk(tmp_path, _chunk(moved))

    records = load_all_triage_by_chunk(tmp_path)[CHUNK_ID]
    assert len(records) == 2
    assert not _hidden(tmp_path, _finding(tmp_path, "Concord"))


def test_a_narrowed_rerun_leaves_other_evaluators_alone(tmp_path):
    """Only the evaluators that ran are re-paired, even when another's keys moved."""
    before = [i.to_dict() for i in _dictionary_findings(BASE)]
    after = [i.to_dict() for i in _dictionary_findings("Al principio, " + BASE)]
    concord = before[0]
    _suppress(tmp_path, concord, "Concord")

    assert ev._carry_marks_forward(tmp_path, CHUNK_ID, before, after, ["grammar"]) == 0
    assert len(load_all_triage_by_chunk(tmp_path)[CHUNK_ID]) == 1
    # The same lists with dictionary in the run do carry, so the filter is what held.
    assert ev._carry_marks_forward(tmp_path, CHUNK_ID, before, after, ["dictionary"]) == 1


def test_a_delete_and_an_add_of_one_term_carry_nothing(tmp_path):
    """Same group size, different members: the shifts disagree, so nothing moves.

    Deleting the marked occurrence's sentence pulls the other one up 120 places
    while the added occurrence lands far below; zipping in document order would
    hang the mark on the occurrence nobody ruled on.
    """

    def bl(pos: int, idx: int) -> dict:
        return {
            "eval_name": "blacklist", "issue_index": idx, "severity": "warning",
            "message": f"Blacklisted term 'x' found: 'x' at position {pos}",
            "location": {"raw": f"char {pos}", "side": "target", "char_start": pos},
        }

    marked, other = bl(100, 0), bl(500, 1)
    append_feedback(
        tmp_path, CHUNK_ID, "blacklist", 0, "false_positive",
        message=marked["message"], key=issue_key("blacklist", marked),
    )

    written = ev._carry_marks_forward(
        tmp_path, CHUNK_ID, [marked, other], [bl(380, 0), bl(800, 1)], ["blacklist"],
    )

    assert written == 0
    assert len(load_feedback_for_chunk(tmp_path, CHUNK_ID)) == 1


def test_a_later_resolved_on_the_old_key_is_not_carried(tmp_path, checker):
    """The latest record on a key is the standing one; a superseded dismissal stays put."""
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    concord = _finding(tmp_path, "Concord")
    for label in ("false_positive", "resolved"):
        append_feedback(
            tmp_path, CHUNK_ID, "dictionary", concord["issue_index"], label,
            message=concord["message"], key=issue_key("dictionary", concord),
        )

    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))

    assert len(load_feedback_for_chunk(tmp_path, CHUNK_ID)) == 2
    assert not _dismissed(tmp_path, _finding(tmp_path, "Concord"))


def test_a_mark_already_on_the_new_key_blocks_a_carried_dismissal(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    concord = _finding(tmp_path, "Concord")
    append_feedback(
        tmp_path, CHUNK_ID, "dictionary", concord["issue_index"], "false_positive",
        message=concord["message"], key=issue_key("dictionary", concord),
    )
    moved = "Al principio, " + BASE
    new_issue = _dictionary_findings(moved)[0].to_dict()
    append_feedback(
        tmp_path, CHUNK_ID, "dictionary", 0, "resolved",
        message=new_issue["message"], key=issue_key("dictionary", new_issue),
    )

    evaluate_and_persist_chunk(tmp_path, _chunk(moved))

    records = load_feedback_for_chunk(tmp_path, CHUNK_ID)
    assert len(records) == 2
    assert not any(r.get("carried_from") for r in records)


def test_a_verdict_survives_two_edits_in_a_row(tmp_path, checker):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    _suppress(tmp_path, _finding(tmp_path, "Concord"), "Concord")

    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))
    middle = _finding(tmp_path, "Concord")
    evaluate_and_persist_chunk(tmp_path, _chunk("Muy al principio, " + BASE))

    last = _finding(tmp_path, "Concord")
    assert _hidden(tmp_path, last)
    record = load_all_triage_by_chunk(tmp_path)[CHUNK_ID][-1]
    assert record["issue_key"] == issue_key("dictionary", last)
    assert record["carried_from"] == issue_key("dictionary", middle)


@pytest.mark.parametrize("label", sorted(ev.CARRIED_FEEDBACK_TYPES))
def test_every_carried_label_is_carried(tmp_path, checker, label):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    vermont = _finding(tmp_path, "Vermont")
    append_feedback(
        tmp_path, CHUNK_ID, "dictionary", vermont["issue_index"], label,
        message=vermont["message"], key=issue_key("dictionary", vermont),
    )

    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))

    mark = load_feedback_for_chunk(tmp_path, CHUNK_ID)[-1]
    assert mark["feedback_type"] == label
    assert mark["issue_key"] == issue_key("dictionary", _finding(tmp_path, "Vermont"))


def test_identical_findings_are_carried_only_when_the_count_matches(tmp_path):
    """Two findings with one anchor: zip in order if both survive, else skip."""

    def grammar(start: int, idx: int) -> dict:
        return {
            "eval_name": "grammar", "issue_index": idx, "severity": "warning",
            "message": "Error de concordancia. Context: '...un pura sangre...'",
            "location": {"raw": f"char {start}-{start + 7}", "side": "target",
                         "char_start": start, "match": "un pura"},
        }

    a, b = grammar(10, 0), grammar(50, 1)
    assert finding_anchor("grammar", a) == finding_anchor("grammar", b)
    for issue in (a, b):
        append_triage(
            tmp_path, CHUNK_ID, "grammar", issue["issue_index"], "suppress",
            key=issue_key("grammar", issue), confidence=0.95,
        )

    # One of the two went away: which one is unknowable, so nothing moves.
    written = ev._carry_marks_forward(
        tmp_path, CHUNK_ID, [a, b], [grammar(20, 0)], ["grammar"],
    )
    assert written == 0

    # Both moved: carried in document order.
    a2, b2 = grammar(20, 0), grammar(60, 1)
    written = ev._carry_marks_forward(tmp_path, CHUNK_ID, [a, b], [a2, b2], ["grammar"])
    assert written == 2
    records = load_all_triage_by_chunk(tmp_path)[CHUNK_ID]
    by_new = {r["issue_key"]: r["carried_from"] for r in records if r.get("carried_from")}
    assert by_new == {
        issue_key("grammar", a2): issue_key("grammar", a),
        issue_key("grammar", b2): issue_key("grammar", b),
    }


def test_an_edit_inside_the_grammar_context_drops_the_verdict(tmp_path):
    old = {
        "eval_name": "grammar", "issue_index": 0, "severity": "warning",
        "message": "Error de concordancia. Context: '...hijo de un pura sangre...'",
        "location": {"raw": "char 10-17", "side": "target", "char_start": 10},
    }
    new = dict(old, message="Error de concordancia. Context: '...nieto de un pura sangre...'",
               location={"raw": "char 11-18", "side": "target", "char_start": 11})
    append_triage(tmp_path, CHUNK_ID, "grammar", 0, "suppress",
                  key=issue_key("grammar", old), confidence=0.95)

    assert ev._carry_marks_forward(tmp_path, CHUNK_ID, [old], [new], ["grammar"]) == 0


def test_anchor_strips_blacklist_offsets():
    def bl(pos: int) -> dict:
        return {"severity": "warning",
                "message": f"Blacklisted term 'x' found: 'x' at position {pos}",
                "location": {"raw": f"char {pos}", "side": "target"}}

    assert finding_anchor("blacklist", bl(3)) == finding_anchor("blacklist", bl(40))


def test_a_sidecar_failure_does_not_fail_the_save(tmp_path, checker, monkeypatch):
    evaluate_and_persist_chunk(tmp_path, _chunk(BASE))
    _suppress(tmp_path, _finding(tmp_path, "Concord"), "Concord")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ev, "append_triage", boom)
    evaluate_and_persist_chunk(tmp_path, _chunk("Al principio, " + BASE))

    assert _finding(tmp_path, "Concord")["location"]["char_start"] > 20
