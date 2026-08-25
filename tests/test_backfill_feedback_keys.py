"""Tests for the feedback-key backfill script.

The script rewrites ``_feedback.jsonl`` in place, so the things worth pinning
are the ones that decide whether a mark keeps meaning what the human meant:
which marks it refuses to key, and that the rewrite cannot lose a record.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_feedback_keys import (  # noqa: E402
    backfill_project,
    discover_projects,
    eval_ran_at,
    resolve_project,
)
from web_ui.evaluations import issue_key  # noqa: E402


ISSUE_A = {
    "severity": "warning",
    "message": "'Bothon': Unknown word (found 1 time(s))",
    "location": "Character position 7551",
}
ISSUE_B = {
    "severity": "warning",
    "message": "'Coucy': Unknown word (found 1 time(s))",
    "location": "Character position 9102",
}

EVAL_RAN = "2026-08-11T15:00:00.000000"
BEFORE_RUN = "2026-08-05T21:35:17.064932"
AFTER_RUN = "2026-08-12T09:00:00.000000"


def make_project(tmp_path, records, eval_at=EVAL_RAN, issues=None):
    """A project directory holding one evaluation and the given feedback lines."""
    proj = tmp_path / "a-book"
    evaluations = proj / "evaluations"
    evaluations.mkdir(parents=True)

    evaluation = {
        "chunk_id": "chapter_01_chunk_000",
        "results": [
            {
                "eval_name": "dictionary",
                "issues": issues if issues is not None else [ISSUE_A, ISSUE_B],
            }
        ],
        "eval_runs": {"dictionary": {"at": eval_at, "text_sha": "abc"}},
    }
    (evaluations / "chapter_01_chunk_000.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    (evaluations / "_feedback.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return proj


def record(ts=AFTER_RUN, index=0, eval_name="dictionary", **extra):
    base = {
        "ts": ts,
        "chunk_id": "chapter_01_chunk_000",
        "eval_name": eval_name,
        "issue_index": index,
        "feedback_type": "false_positive",
    }
    base.update(extra)
    return base


def lines(proj):
    path = proj / "evaluations" / "_feedback.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestStalenessGuard:
    def test_mark_newer_than_the_run_is_keyed(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=AFTER_RUN, index=0)])
        _, stats = backfill_project(proj, write=True)

        assert stats["keyed"] == 1
        assert lines(proj)[0]["issue_key"] == issue_key("dictionary", ISSUE_A)

    def test_mark_older_than_the_run_is_refused(self, tmp_path):
        """The index still resolves -- that is exactly the danger. The re-run
        rewrote the list, so slot 0 is no longer the finding that was marked,
        and keying it would freeze the intruder's hash in place."""
        proj = make_project(tmp_path, [record(ts=BEFORE_RUN, index=0)])
        _, stats = backfill_project(proj, write=True)

        assert stats["stale"] == 1
        assert stats["keyed"] == 0

        written = lines(proj)[0]
        assert written["issue_key"] is None
        assert written["key_skipped_stale"] is True
        assert written["key_unresolved"] is True

    def test_refused_mark_keeps_its_index_for_positional_matching(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=BEFORE_RUN, index=1)])
        backfill_project(proj, write=True)
        assert lines(proj)[0]["issue_index"] == 1

    def test_already_keyed_records_are_left_alone(self, tmp_path):
        existing = record(ts=BEFORE_RUN, issue_key="deadbeefdeadbeef")
        proj = make_project(tmp_path, [existing])
        _, stats = backfill_project(proj, write=True)

        assert stats["already_keyed"] == 1
        assert lines(proj)[0]["issue_key"] == "deadbeefdeadbeef"

    def test_dangling_index_is_still_unresolved_not_stale(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=AFTER_RUN, index=99)])
        _, stats = backfill_project(proj, write=True)

        assert stats["unresolved"] == 1
        assert stats["stale"] == 0
        assert "key_skipped_stale" not in lines(proj)[0]


class TestEvalRanAt:
    def test_coded_evaluator_uses_eval_runs(self):
        evaluation = {"eval_runs": {"grammar": {"at": EVAL_RAN}}}
        assert eval_ran_at(evaluation, "grammar") == EVAL_RAN

    def test_judge_uses_its_own_executed_at(self):
        evaluation = {"judges": {"address": {"executed_at": EVAL_RAN}}}
        assert eval_ran_at(evaluation, "address") == EVAL_RAN

    def test_judge_without_executed_at_falls_back_to_judges_at(self):
        evaluation = {"judges": {"address": {}}, "judges_at": EVAL_RAN}
        assert eval_ran_at(evaluation, "address") == EVAL_RAN

    def test_eval_runs_wins_over_judges_at(self):
        evaluation = {
            "eval_runs": {"address": {"at": EVAL_RAN}},
            "judges": {"address": {}},
            "judges_at": AFTER_RUN,
        }
        assert eval_ran_at(evaluation, "address") == EVAL_RAN

    def test_unknown_evaluator_is_none(self):
        """None must mean 'no evidence of staleness', so the mark is keyed --
        not 'refuse', which would leave every judge mark unkeyed forever."""
        assert eval_ran_at({"eval_runs": {}}, "grammar") is None

    def test_unknown_run_time_does_not_block_keying(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=BEFORE_RUN)], eval_at=None)
        _, stats = backfill_project(proj, write=True)
        assert stats["keyed"] == 1


class TestRewriteIsLossless:
    def test_every_record_survives(self, tmp_path):
        records = [record(ts=AFTER_RUN, index=i % 2) for i in range(6)]
        proj = make_project(tmp_path, records)
        backfill_project(proj, write=True)
        assert len(lines(proj)) == 6

    def test_no_temp_file_is_left_behind(self, tmp_path):
        proj = make_project(tmp_path, [record()])
        backfill_project(proj, write=True)
        assert list((proj / "evaluations").glob("*.tmp")) == []

    def test_backup_is_written_once_and_never_overwritten(self, tmp_path):
        proj = make_project(tmp_path, [record()])
        backup = proj / "evaluations" / "_feedback.jsonl.bak"

        backfill_project(proj, write=True)
        pristine = backup.read_text(encoding="utf-8")
        assert "issue_key" not in pristine

        backfill_project(proj, write=True)
        assert backup.read_text(encoding="utf-8") == pristine

    def test_report_mode_changes_nothing(self, tmp_path):
        proj = make_project(tmp_path, [record()])
        path = proj / "evaluations" / "_feedback.jsonl"
        before = path.read_text(encoding="utf-8")

        _, stats = backfill_project(proj, write=False)

        assert stats["keyed"] == 1
        assert path.read_text(encoding="utf-8") == before
        assert not (proj / "evaluations" / "_feedback.jsonl.bak").exists()

    def test_unparseable_line_is_preserved_verbatim(self, tmp_path):
        proj = make_project(tmp_path, [record()])
        path = proj / "evaluations" / "_feedback.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8") + "{not json at all\n", encoding="utf-8"
        )

        out, stats = backfill_project(proj, write=True)

        assert stats["malformed_preserved"] == 1
        assert out[-1] == "{not json at all"

    def test_rerun_is_idempotent(self, tmp_path):
        proj = make_project(tmp_path, [record()])
        backfill_project(proj, write=True)
        first = lines(proj)

        _, stats = backfill_project(proj, write=True)

        assert stats["already_keyed"] == 1
        assert stats["keyed"] == 0
        assert lines(proj) == first


class TestProjectDiscovery:
    def _corpus(self, tmp_path, names):
        root = tmp_path / "projects"
        for name in names:
            (root / name / "evaluations").mkdir(parents=True)
            (root / name / "evaluations" / "_feedback.jsonl").write_text(
                "", encoding="utf-8"
            )
        return root

    def test_finds_top_level_and_hidden_group_projects(self, tmp_path):
        root = self._corpus(tmp_path, ["a-book", ".published/b-book"])
        found = {p.name for p in discover_projects(root)}
        assert found == {"a-book", "b-book"}

    def test_excludes_bak_snapshots(self, tmp_path):
        """projects/.backburner/the-little-duke.bak-footnote-migration is a copy
        of another book; counting its marks double-counts the same labels."""
        root = self._corpus(
            tmp_path, ["a-book", ".backburner/a-book.bak-some-migration"]
        )
        assert [p.name for p in discover_projects(root)] == ["a-book"]

    def test_resolves_a_slug_inside_a_hidden_group(self, tmp_path):
        root = self._corpus(tmp_path, [".published/b-book"])
        assert resolve_project(root, "b-book") == root / ".published" / "b-book"

    def test_prefers_a_top_level_slug(self, tmp_path):
        root = self._corpus(tmp_path, ["b-book", ".published/b-book"])
        assert resolve_project(root, "b-book") == root / "b-book"

    def test_unknown_slug_falls_back_to_the_plain_join(self, tmp_path):
        root = self._corpus(tmp_path, ["a-book"])
        assert resolve_project(root, "nosuch") == root / "nosuch"
