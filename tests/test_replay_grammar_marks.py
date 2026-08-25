"""Tests for the grammar-mark replay measurement.

This script decides which LanguageTool rules land in
``GrammarEvaluator.DEFAULT_IGNORE_RULES``, so a mark attributed to the wrong
rule does not just skew a report -- it can silence a rule that catches real
defects. These tests cover the join that does the attributing, not the
LanguageTool replay itself (which needs the JAR and is measured by hand).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay_grammar_marks import (  # noqa: E402
    collect_stored_marks,
    load_marks,
)
from web_ui.evaluations import issue_key  # noqa: E402


COMMA = {
    "severity": "warning",
    "message": "Coma tras el adverbio. Context: 'Sin embargo, el rey'",
    "location": "Character position 120",
}
TILDE = {
    "severity": "warning",
    "message": "Falta la tilde. Context: 'el mas alto'",
    "location": "Character position 300",
}

EVAL_RAN = "2026-08-11T15:00:00.000000"
BEFORE_RUN = "2026-08-05T21:35:17.064932"
AFTER_RUN = "2026-08-12T09:00:00.000000"


def make_project(tmp_path, records, issues, eval_at=EVAL_RAN):
    proj = tmp_path / "a-book"
    evaluations = proj / "evaluations"
    evaluations.mkdir(parents=True)

    evaluation = {
        "chunk_id": "chapter_01_chunk_000",
        "results": [{"eval_name": "grammar", "issues": issues}],
        "eval_runs": {"grammar": {"at": eval_at, "text_sha": "abc"}},
    }
    (evaluations / "chapter_01_chunk_000.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    (evaluations / "_feedback.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return proj


def record(ts=AFTER_RUN, index=None, key=None, feedback="false_positive"):
    rec = {
        "ts": ts,
        "chunk_id": "chapter_01_chunk_000",
        "eval_name": "grammar",
        "issue_index": index,
        "feedback_type": feedback,
    }
    if key is not None:
        rec["issue_key"] = key
    return rec


class TestLoadMarks:
    def test_keyed_record_is_read_as_a_key(self, tmp_path):
        proj = make_project(tmp_path, [record(index=3, key="abc123")], [COMMA])
        marks = load_marks(proj)
        assert marks["chapter_01_chunk_000"] == [
            ("key", "abc123", "false_positive", AFTER_RUN)
        ]

    def test_unkeyed_record_falls_back_to_the_index(self, tmp_path):
        proj = make_project(tmp_path, [record(index=3)], [COMMA])
        marks = load_marks(proj)
        assert marks["chapter_01_chunk_000"][0][:2] == ("index", 3)

    def test_record_without_a_chunk_id_is_skipped_not_fatal(self, tmp_path):
        bad = record(index=0)
        del bad["chunk_id"]
        proj = make_project(tmp_path, [bad, record(index=1)], [COMMA, TILDE])
        marks = load_marks(proj)
        assert sum(len(v) for v in marks.values()) == 1

    def test_other_evaluators_are_ignored(self, tmp_path):
        other = record(index=0)
        other["eval_name"] = "dictionary"
        proj = make_project(tmp_path, [other], [COMMA])
        assert load_marks(proj) == {}


class TestCollectStoredMarks:
    def test_key_wins_over_a_moved_index(self, tmp_path):
        """The mark was made when the comma finding sat at index 1; a re-run
        moved it to index 0. Positionally it would now score the tilde rule."""
        proj = make_project(
            tmp_path,
            [record(index=1, key=issue_key("grammar", COMMA))],
            [COMMA, TILDE],
        )
        rows, stale = collect_stored_marks(proj)

        assert stale == 0
        assert len(rows) == 1
        assert rows[0][0] == "Coma tras el adverbio."
        assert rows[0][1] == "false_positive"

    def test_unkeyed_mark_older_than_the_run_is_dropped(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=BEFORE_RUN, index=0)], [COMMA])
        rows, stale = collect_stored_marks(proj)

        assert rows == []
        assert stale == 1

    def test_unkeyed_mark_newer_than_the_run_is_used(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=AFTER_RUN, index=0)], [COMMA])
        rows, stale = collect_stored_marks(proj)

        assert stale == 0
        assert [r[0] for r in rows] == ["Coma tras el adverbio."]

    def test_a_keyed_mark_is_never_treated_as_stale(self, tmp_path):
        """A content key does not care when the evaluator last ran -- that is
        the whole reason for keying."""
        proj = make_project(
            tmp_path,
            [record(ts=BEFORE_RUN, index=99, key=issue_key("grammar", TILDE))],
            [COMMA, TILDE],
        )
        rows, stale = collect_stored_marks(proj)

        assert stale == 0
        assert [r[0] for r in rows] == ["Falta la tilde."]

    def test_dangling_index_is_dropped(self, tmp_path):
        proj = make_project(tmp_path, [record(index=99)], [COMMA])
        rows, stale = collect_stored_marks(proj)
        assert rows == []
        assert stale == 0

    def test_negative_index_is_dropped_not_wrapped(self, tmp_path):
        proj = make_project(tmp_path, [record(index=-1)], [COMMA, TILDE])
        rows, _ = collect_stored_marks(proj)
        assert rows == []

    def test_two_identities_for_one_finding_dedupe(self, tmp_path):
        """A record keyed by content and an older one keyed by position can name
        the same finding; counting both would double its label."""
        proj = make_project(
            tmp_path,
            [
                record(index=0, feedback="false_positive"),
                record(index=0, key=issue_key("grammar", COMMA), feedback="resolved"),
            ],
            [COMMA],
        )
        rows, _ = collect_stored_marks(proj)

        assert rows == [("Coma tras el adverbio.", "resolved")]

    def test_other_labels_are_not_ground_truth(self, tmp_path):
        """bad_message and missing_context_gap say the finding was real but
        poorly reported -- a different axis from precision."""
        proj = make_project(
            tmp_path, [record(index=0, feedback="bad_message")], [COMMA]
        )
        rows, _ = collect_stored_marks(proj)
        assert rows == []
