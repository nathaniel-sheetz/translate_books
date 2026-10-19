"""Tests for the orphaned-mark re-key script.

What matters is the refusal set: the script appends verdicts onto findings it
matched by word or message alone, so every case where that match could be the
wrong finding must leave the record where it is.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.rekey_orphaned_marks as rekey  # noqa: E402
from scripts.rekey_orphaned_marks import discover_projects, rekey_project  # noqa: E402
from web_ui.evaluations import (  # noqa: E402
    append_feedback,
    append_triage,
    build_dismissed,
    build_triaged,
    is_dismissed,
    is_triaged,
    issue_key,
    load_all_feedback_by_chunk,
    load_all_triage_by_chunk,
)

CHUNK = "chapter_01_chunk_000"


def dictionary(word: str, pos: int, index: int = 0, count: int = 1) -> dict:
    return {
        "eval_name": "dictionary", "issue_index": index, "severity": "error",
        "message": f"'{word}': English word in translation (found {count} time(s))",
        "location": {"raw": f"Character position {pos}", "side": "target",
                     "char_start": pos, "match": word},
    }


def grammar(match: str, pos: int, index: int = 0, rule_id=None) -> dict:
    issue = {
        "eval_name": "grammar", "issue_index": index, "severity": "warning",
        "message": f"Error de concordancia. Context: '...{match}...'",
        "location": {"raw": f"char {pos}-{pos + len(match)}", "side": "target",
                     "char_start": pos, "match": match},
    }
    if rule_id:
        issue["rule_id"] = rule_id
    return issue


def project(tmp_path, issues) -> Path:
    proj = tmp_path / "projects" / "book"
    (proj / "evaluations").mkdir(parents=True)
    (proj / "evaluations" / f"{CHUNK}.json").write_text(
        json.dumps({"chunk_id": CHUNK, "normalized_issues": issues}), encoding="utf-8",
    )
    return proj


def suppress(proj, issue, term, rule_id=None):
    name = issue["eval_name"]
    append_triage(proj, CHUNK, name, issue["issue_index"], "suppress",
                  key=issue_key(name, issue), confidence=0.97, term=term,
                  rule_id=rule_id, reason="name", run_id="r1")


def hidden(proj, issue) -> bool:
    triaged = build_triaged(load_all_triage_by_chunk(proj).get(CHUNK, []))
    return is_triaged(triaged, issue["eval_name"], issue)


def test_a_moved_word_is_rekeyed(tmp_path):
    now = dictionary("Concord", 120)
    proj = project(tmp_path, [now])
    suppress(proj, dictionary("Concord", 90), "Concord")

    stats = rekey_project(proj, write=True)

    assert stats["triage_rekeyed"] == 1
    assert hidden(proj, now)
    record = load_all_triage_by_chunk(proj)[CHUNK][-1]
    assert record["carried_from"] == issue_key("dictionary", dictionary("Concord", 90))
    assert record["reason"] == "name" and record["run_id"] == "r1"


def test_report_only_writes_nothing(tmp_path):
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), "Concord")

    stats = rekey_project(proj, write=False)

    assert stats["triage_rekeyed"] == 1
    assert len(load_all_triage_by_chunk(proj)[CHUNK]) == 1


def test_a_second_run_writes_nothing(tmp_path):
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), "Concord")

    rekey_project(proj, write=True)
    stats = rekey_project(proj, write=True)

    assert stats["triage_rekeyed"] == 0
    assert len(load_all_triage_by_chunk(proj)[CHUNK]) == 2


def test_a_word_no_longer_flagged_is_left_alone(tmp_path):
    proj = project(tmp_path, [dictionary("Iowa", 10)])
    suppress(proj, dictionary("Concord", 90), "Concord")

    stats = rekey_project(proj, write=True)

    assert stats["triage_no_match"] == 1 and stats["triage_rekeyed"] == 0


def test_a_finding_with_its_own_verdict_gains_no_second_one(tmp_path):
    now = dictionary("Concord", 120)
    proj = project(tmp_path, [now])
    suppress(proj, dictionary("Concord", 90), "Concord")
    append_triage(proj, CHUNK, "dictionary", 0, "keep",
                  key=issue_key("dictionary", now), confidence=0.9, term="Concord")

    stats = rekey_project(proj, write=True)

    assert stats["triage_target_has_record"] == 1
    assert not hidden(proj, now)


def test_grammar_ambiguity_is_refused(tmp_path):
    a, b = grammar("un pura", 10, 0), grammar("un pura", 80, 1)
    proj = project(tmp_path, [a, b])
    suppress(proj, grammar("un pura", 5), "un pura")

    stats = rekey_project(proj, write=True)

    assert stats["triage_ambiguous"] == 1
    assert not hidden(proj, a) and not hidden(proj, b)


def test_grammar_rule_disambiguates_when_both_sides_have_one(tmp_path):
    right = grammar("un pura", 10, 0, rule_id="AGREEMENT")
    wrong = grammar("un pura", 80, 1, rule_id="OTHER")
    proj = project(tmp_path, [right, wrong])
    suppress(proj, grammar("un pura", 5, rule_id="AGREEMENT"), "un pura",
             rule_id="AGREEMENT")

    rekey_project(proj, write=True)

    assert hidden(proj, right) and not hidden(proj, wrong)


def test_two_orphans_claiming_one_finding_are_both_refused(tmp_path):
    now = dictionary("Concord", 120)
    proj = project(tmp_path, [now])
    suppress(proj, dictionary("Concord", 90), "Concord")
    suppress(proj, dictionary("Concord", 60, count=2), "Concord")

    stats = rekey_project(proj, write=True)

    assert stats["triage_contested"] == 2 and stats["triage_rekeyed"] == 0


def test_a_dismissal_rekeys_on_its_message(tmp_path):
    now = dictionary("Vermont", 300, count=3)
    proj = project(tmp_path, [now])
    old = dictionary("Vermont", 200, count=4)
    append_feedback(proj, CHUNK, "dictionary", 0, "false_positive",
                    message=old["message"], key=issue_key("dictionary", old))

    stats = rekey_project(proj, write=True)

    assert stats["feedback_rekeyed"] == 1
    by_key, by_index = build_dismissed(load_all_feedback_by_chunk(proj)[CHUNK])
    assert is_dismissed(by_key, by_index, "dictionary", 0, now)


def test_a_resolved_mark_is_never_rekeyed(tmp_path):
    now = dictionary("Vermont", 300)
    proj = project(tmp_path, [now])
    old = dictionary("Vermont", 200)
    append_feedback(proj, CHUNK, "dictionary", 0, "resolved",
                    message=old["message"], key=issue_key("dictionary", old))

    stats = rekey_project(proj, write=True)

    assert stats["feedback_rekeyed"] == 0 and stats["feedback_not_carryable"] == 1
    by_key, by_index = build_dismissed(load_all_feedback_by_chunk(proj)[CHUNK])
    assert not is_dismissed(by_key, by_index, "dictionary", 0, now)


def test_discovery_finds_a_book_with_only_triage(tmp_path):
    proj = project(tmp_path, [])
    suppress(proj, dictionary("Concord", 90), "Concord")

    assert discover_projects(tmp_path / "projects") == [proj]


def test_a_record_written_after_the_cutoff_is_left_alone(tmp_path):
    """Carry-forward drops some verdicts on purpose; the script must not undo that."""
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), "Concord")

    stats = rekey_project(proj, write=True, before=datetime(2000, 1, 1))

    assert stats["triage_after_cutoff"] == 1 and stats["triage_rekeyed"] == 0
    assert len(load_all_triage_by_chunk(proj)[CHUNK]) == 1

    stats = rekey_project(proj, write=True, before=datetime(9999, 1, 1))
    assert stats["triage_rekeyed"] == 1


def test_a_second_run_does_not_report_the_carried_record_as_orphaned(tmp_path):
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), "Concord")

    rekey_project(proj, write=True)
    stats = rekey_project(proj, write=False)

    assert stats["triage_already_carried"] == 1
    assert stats["triage_orphaned"] == 0 and stats["triage_target_has_record"] == 0


def test_a_rule_only_the_record_has_does_not_block_the_match(tmp_path):
    now = grammar("un pura", 10)
    proj = project(tmp_path, [now])
    suppress(proj, grammar("un pura", 5, rule_id="AGREEMENT"), "un pura",
             rule_id="AGREEMENT")

    assert rekey_project(proj, write=True)["triage_rekeyed"] == 1
    assert hidden(proj, now)


def test_a_record_with_no_term_is_never_matched(tmp_path):
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), None)

    stats = rekey_project(proj, write=True)

    assert stats["triage_no_match"] == 1 and stats["triage_rekeyed"] == 0


def test_a_chunk_with_no_evaluation_is_skipped(tmp_path):
    proj = project(tmp_path, [])
    (proj / "evaluations" / f"{CHUNK}.json").unlink()
    suppress(proj, dictionary("Concord", 90), "Concord")

    assert not rekey_project(proj, write=True)
    assert len(load_all_triage_by_chunk(proj)[CHUNK]) == 1


def _false_positive(proj, issue):
    append_feedback(proj, CHUNK, "dictionary", issue["issue_index"], "false_positive",
                    message=issue["message"], key=issue_key("dictionary", issue))


def test_a_dismissal_naming_two_findings_is_refused(tmp_path):
    proj = project(tmp_path, [dictionary("Vermont", 300, 0), dictionary("Vermont", 400, 1)])
    _false_positive(proj, dictionary("Vermont", 200))

    stats = rekey_project(proj, write=True)

    assert stats["feedback_ambiguous"] == 1 and stats["feedback_rekeyed"] == 0


def test_a_dismissal_onto_a_marked_finding_is_refused(tmp_path):
    now = dictionary("Vermont", 300)
    proj = project(tmp_path, [now])
    _false_positive(proj, dictionary("Vermont", 200))
    append_feedback(proj, CHUNK, "dictionary", 0, "resolved",
                    message=now["message"], key=issue_key("dictionary", now))

    stats = rekey_project(proj, write=True)

    assert stats["feedback_target_has_record"] == 1 and stats["feedback_rekeyed"] == 0


def test_two_dismissals_claiming_one_finding_are_both_refused(tmp_path):
    proj = project(tmp_path, [dictionary("Vermont", 300)])
    _false_positive(proj, dictionary("Vermont", 200, count=4))
    _false_positive(proj, dictionary("Vermont", 100, count=2))

    stats = rekey_project(proj, write=True)

    assert stats["feedback_contested"] == 2 and stats["feedback_rekeyed"] == 0


def test_a_slug_under_a_group_directory_resolves(tmp_path):
    proj = tmp_path / "projects" / ".published" / "book"
    (proj / "evaluations").mkdir(parents=True)
    (proj / "evaluations" / "_triage.jsonl").write_text("", encoding="utf-8")

    assert rekey.resolve_project(tmp_path / "projects", "book") == proj


def test_main_reports_without_writing_and_skips_missing_slugs(tmp_path, monkeypatch, capsys):
    proj = project(tmp_path, [dictionary("Concord", 120)])
    suppress(proj, dictionary("Concord", 90), "Concord")
    monkeypatch.setattr(rekey, "REPO_ROOT", tmp_path)

    # Today's record is newer than the default cutoff; widen it to see it re-keyed.
    assert rekey.main(["--project", "book", "--project", "nosuch",
                       "--before", "9999-01-01"]) == 0

    out = capsys.readouterr().out
    assert "REPORT ONLY" in out
    assert "skip (missing)" in out and "nosuch" in out
    assert "triage_rekeyed: 1" in out
    assert len(load_all_triage_by_chunk(proj)[CHUNK]) == 1


def test_main_rejects_a_bad_cutoff(capsys):
    with pytest.raises(SystemExit) as exc:
        rekey.main(["--before", "last tuesday"])
    assert exc.value.code == 2
    assert "--before" in capsys.readouterr().err
