"""Tests for the dictionary-mark replay measurement.

This script is what decides whether the tokenizer and morphology changes were
safe to make, and its recall guard is the only thing standing between a wider
morphological fallback and a silently lost real defect. A mark joined to the
wrong token, or a ``still_flagged`` that answers the wrong question, would make
that guard report 0 while defects were being swallowed.

Covered here: the mark-to-token join, the two token-level predicates, and
``replay_project``'s morphology off/on wiring. The full corpus replay is not --
it reads the real ``projects/`` tree and is run by hand.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay_dictionary_marks import (  # noqa: E402
    collect_labeled_terms,
    load_marks,
    removal_bucket,
    replay_project,
    still_flagged,
)
from src.evaluators.dictionary_eval import DictionaryEvaluator  # noqa: E402
from src.models import Chunk, ChunkMetadata, ChunkStatus  # noqa: E402
from web_ui.evaluations import issue_key  # noqa: E402


# A modern finding, carrying the token in `term`.
MONTON = {
    "severity": "warning",
    "message": "'montoncito': Unknown word (not in Spanish or English dictionary) (found 2 time(s))",
    "location": "Character position 120",
    "term": "montoncito",
}
# A legacy finding with `term: null`, where the token has to come back out of
# the message via issue_term's _QUOTED_TERM_RE.
LEGACY = {
    "severity": "warning",
    "message": "'nivea': Unknown word (not in Spanish or English dictionary) (found 1 time(s))",
    "location": "Character position 300",
    "term": None,
}

EVAL_RAN = "2026-08-11T15:00:00.000000"
BEFORE_RUN = "2026-08-05T21:35:17.064932"
AFTER_RUN = "2026-08-12T09:00:00.000000"


@pytest.fixture(scope="module")
def evaluator():
    return DictionaryEvaluator()


def make_project(tmp_path, records, issues, eval_at=EVAL_RAN):
    proj = tmp_path / "a-book"
    evaluations = proj / "evaluations"
    evaluations.mkdir(parents=True)

    evaluation = {
        "chunk_id": "chapter_01_chunk_000",
        "results": [{"eval_name": "dictionary", "issues": issues}],
        "eval_runs": {"dictionary": {"at": eval_at, "text_sha": "abc"}},
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
        "eval_name": "dictionary",
        "issue_index": index,
        "feedback_type": feedback,
    }
    if key is not None:
        rec["issue_key"] = key
    return rec


class TestLoadMarks:
    def test_keyed_record_is_read_as_a_key(self, tmp_path):
        proj = make_project(tmp_path, [record(index=3, key="abc123")], [MONTON])
        marks = load_marks(proj)
        assert marks["chapter_01_chunk_000"] == [
            ("key", "abc123", "false_positive", AFTER_RUN)
        ]

    def test_unkeyed_record_falls_back_to_the_index(self, tmp_path):
        proj = make_project(tmp_path, [record(index=3)], [MONTON])
        assert load_marks(proj)["chapter_01_chunk_000"][0][:2] == ("index", 3)

    def test_other_evaluators_are_ignored(self, tmp_path):
        other = record(index=0)
        other["eval_name"] = "grammar"
        proj = make_project(tmp_path, [other], [MONTON])
        assert load_marks(proj) == {}


class TestCollectLabeledTerms:
    def test_term_field_is_the_join_key(self, tmp_path):
        proj = make_project(
            tmp_path, [record(index=0, key=issue_key("dictionary", MONTON))], [MONTON]
        )
        rows, _ = collect_labeled_terms(proj)
        assert rows == [("montoncito", "false_positive")]

    def test_legacy_null_term_is_recovered_from_the_message(self, tmp_path):
        """Rows written before Issue.term existed still carry the token, quoted
        at the head of the message -- which is the whole reason this join needs
        no lexicon, unlike the grammar one."""
        proj = make_project(
            tmp_path,
            [record(index=0, key=issue_key("dictionary", LEGACY), feedback="resolved")],
            [LEGACY],
        )
        rows, _ = collect_labeled_terms(proj)
        assert rows == [("nivea", "resolved")]

    def test_key_wins_over_a_moved_index(self, tmp_path):
        """The mark was made when montoncito sat at index 1; a re-run moved it
        to 0. Positionally it would now score the wrong word."""
        proj = make_project(
            tmp_path,
            [record(index=1, key=issue_key("dictionary", MONTON))],
            [MONTON, LEGACY],
        )
        rows, stats = collect_labeled_terms(proj)
        assert [r[0] for r in rows] == ["montoncito"]
        assert stats["mark_stale_index"] == 0

    def test_unkeyed_mark_older_than_the_run_is_dropped(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=BEFORE_RUN, index=0)], [MONTON])
        rows, stats = collect_labeled_terms(proj)
        assert rows == []
        assert stats["mark_stale_index"] == 1

    def test_unkeyed_mark_newer_than_the_run_is_used(self, tmp_path):
        proj = make_project(tmp_path, [record(ts=AFTER_RUN, index=0)], [MONTON])
        rows, stats = collect_labeled_terms(proj)
        assert [r[0] for r in rows] == ["montoncito"]
        assert stats["mark_stale_index"] == 0

    def test_two_identities_for_one_finding_dedupe(self, tmp_path):
        proj = make_project(
            tmp_path,
            [
                record(index=0, feedback="false_positive"),
                record(
                    index=0,
                    key=issue_key("dictionary", MONTON),
                    feedback="resolved",
                ),
            ],
            [MONTON],
        )
        rows, _ = collect_labeled_terms(proj)
        assert rows == [("montoncito", "resolved")]

    def test_other_labels_are_not_ground_truth(self, tmp_path):
        proj = make_project(
            tmp_path, [record(index=0, feedback="bad_message")], [MONTON]
        )
        rows, _ = collect_labeled_terms(proj)
        assert rows == []


class TestStillFlagged:
    """The recall guard's predicate. A false "yes" hides a lost defect."""

    def test_a_real_typo_is_still_flagged(self, evaluator):
        assert still_flagged(evaluator, "nivea")
        assert still_flagged(evaluator, "grandisimo")

    def test_a_word_the_fix_accepts_is_reported_as_gone(self, evaluator):
        assert not still_flagged(evaluator, "montoncito")
        assert not still_flagged(evaluator, "vámonos")

    def test_the_stored_surface_form_goes_through_the_tokenizer(self, evaluator):
        """A mark recorded against "_sí_" names a token that no longer exists.

        Checking the stored string directly would call it still-flagged, since
        "_sí_" is not a dictionary word; running the tokenizer first is what
        makes the guard see the underscore fix at all.
        """
        assert not still_flagged(evaluator, "_sí_")

    def test_an_english_word_counts_as_flagged(self, evaluator):
        """It is reported as an ERROR rather than a WARNING, but it is still a
        finding, and dropping it would be a lost defect all the same."""
        assert still_flagged(evaluator, "horse")


class TestRemovalBucket:
    def test_underscore_and_markers_are_read_off_the_token(self, evaluator):
        assert removal_bucket(evaluator, "_sí_") == "markdown_underscore"

    @pytest.mark.parametrize("term", ["FOOTNOTE", "CAPTION", "IMAGE"])
    def test_every_blanked_marker_gets_its_own_bucket(self, evaluator, term):
        """The bucket is reachable only because ``still_flagged`` agrees that
        the evaluator no longer emits these; the two are one decision."""
        assert not still_flagged(evaluator, term)
        assert removal_bucket(evaluator, term) == "blanked_marker"

    def test_morphology_is_attributed_only_when_the_raw_lookup_rejects(self, evaluator):
        # Raw dictionary lookup fails on "montoncito"; only the fallback saves it.
        assert removal_bucket(evaluator, "montoncito") == "morphology"
        # "casa" was never rejected by anything, so nothing is credited.
        assert removal_bucket(evaluator, "casa") == "other"


class TestReplayProject:
    def test_morphology_off_on_is_how_the_volume_numbers_are_made(
        self, tmp_path, evaluator
    ):
        """replay_project is what the CHANGELOG volume numbers come from.

        still_flagged tests the token; this tests that evaluate() is actually
        called with apply_morphology off then on. A renamed context key would
        make both counts identical and the fallback look like a no-op.
        """
        proj = make_project(tmp_path, [], [MONTON])
        chunks = proj / "chunks"
        chunks.mkdir()
        chunk = Chunk(
            id="chapter_01_chunk_000",
            chapter_id="chapter_01",
            position=0,
            source_text="He saw a little pile of stones.",
            translated_text="Vio un montoncito de piedras.",
            metadata=ChunkMetadata(
                char_start=0,
                char_end=30,
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=5,
            ),
            status=ChunkStatus.TRANSLATED,
        )
        (chunks / "chapter_01_chunk_000.json").write_text(
            chunk.model_dump_json(), encoding="utf-8"
        )

        stats = replay_project(proj, evaluator)
        assert stats["replayed"] == 1
        assert stats["replay_no_morphology"] >= 1
        assert stats["replay_with_morphology"] == 0
        assert stats["replay_current"] == stats["replay_with_morphology"]
