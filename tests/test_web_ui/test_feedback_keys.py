"""Tests for content-keyed feedback marks.

Marks used to be keyed by ``(eval_name, issue_index)`` -- a POSITION in the
evaluator's issue list. Re-running an evaluator rewrites that list, so a mark
silently re-pointed at whatever finding took the slot. These tests pin the
replacement: a content hash that survives a re-run, with positional matching
kept as a fallback so records written before the key existed still work.
"""

import json

from web_ui.evaluations import (
    append_feedback,
    build_dismissed,
    is_dismissed,
    issue_key,
    load_feedback_for_chunk,
)


RAW_ISSUE = {
    "severity": "warning",
    "message": "'Bothon': Unknown word (found 1 time(s))",
    "location": "Character position 7551",
    "suggestion": "Possible misspelling.",
}


class TestIssueKey:
    def test_stable_across_calls(self):
        assert issue_key("dictionary", RAW_ISSUE) == issue_key("dictionary", RAW_ISSUE)

    def test_normalized_and_raw_shapes_agree(self):
        """A mark is written from the raw issue but read back against a
        normalized one; if they hashed differently the mark would never match."""
        normalized = {
            "severity": RAW_ISSUE["severity"],
            "message": RAW_ISSUE["message"],
            "location": {"raw": RAW_ISSUE["location"], "char_start": 7551},
        }
        assert issue_key("dictionary", normalized) == issue_key("dictionary", RAW_ISSUE)

    def test_same_issue_under_different_evaluators_differs(self):
        assert issue_key("dictionary", RAW_ISSUE) != issue_key("grammar", RAW_ISSUE)

    def test_new_identity_fields_do_not_perturb_the_key(self):
        """``rule_id``/``category``/``term`` are carried on the issue but are
        NOT part of the key, so adding them cannot dangle a stored mark.

        This is the whole migration: 1,028 marks were stamped with keys derived
        from four fields. If a later field ever joined the hash, every one of
        them would stop matching the finding it labels -- silently, since a
        mark that matches nothing looks exactly like a finding nobody marked.
        """
        enriched = dict(
            RAW_ISSUE, rule_id="MORFOLOGIK_RULE_ES", category="TYPOS", term="Bothon"
        )
        assert issue_key("dictionary", enriched) == issue_key("dictionary", RAW_ISSUE)

    def test_changed_message_changes_key(self):
        other = dict(RAW_ISSUE, message="something else")
        assert issue_key("dictionary", other) != issue_key("dictionary", RAW_ISSUE)

    def test_field_boundaries_cannot_be_forged(self):
        """Concatenating on a separator that could appear in content would let
        two different findings collide."""
        a = {"severity": "warning", "message": "ab", "location": "c"}
        b = {"severity": "warning", "message": "a", "location": "bc"}
        assert issue_key("grammar", a) != issue_key("grammar", b)

    def test_suggestion_is_not_part_of_the_key(self):
        """LanguageTool reorders its replacement list between versions; keying on
        it would break marks for no semantic reason."""
        other = dict(RAW_ISSUE, suggestion="Consider: something else")
        assert issue_key("dictionary", other) == issue_key("dictionary", RAW_ISSUE)


class TestAppendFeedbackRecordsKey:
    def test_key_is_written(self, tmp_path):
        key = issue_key("dictionary", RAW_ISSUE)
        path = append_feedback(
            tmp_path, "ch01_chunk_000", "dictionary", 0, "false_positive", key=key
        )
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["issue_key"] == key
        # issue_index is still recorded, as provenance only.
        assert record["issue_index"] == 0

    def test_key_is_optional(self, tmp_path):
        path = append_feedback(tmp_path, "ch01_chunk_000", "length", 0, "resolved")
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["issue_key"] is None


class TestDismissalMatching:
    def _records(self, tmp_path, chunk_id="ch01_chunk_000"):
        return load_feedback_for_chunk(tmp_path, chunk_id)

    def test_content_key_survives_reindexing(self, tmp_path):
        """The bug this replaces: an evaluator re-run drops an earlier finding,
        every later index shifts by one, and the mark follows the position."""
        append_feedback(
            tmp_path,
            "ch01_chunk_000",
            "dictionary",
            3,
            "false_positive",
            key=issue_key("dictionary", RAW_ISSUE),
        )
        by_key, by_index = build_dismissed(self._records(tmp_path))

        # Same finding, now at index 1 after a re-run: still dismissed.
        assert is_dismissed(by_key, by_index, "dictionary", 1, RAW_ISSUE)

    def test_a_different_finding_at_the_old_index_is_not_dismissed(self, tmp_path):
        append_feedback(
            tmp_path,
            "ch01_chunk_000",
            "dictionary",
            3,
            "false_positive",
            key=issue_key("dictionary", RAW_ISSUE),
        )
        by_key, by_index = build_dismissed(self._records(tmp_path))

        intruder = dict(RAW_ISSUE, message="'Coucy': Unknown word (found 1 time(s))")
        assert not is_dismissed(by_key, by_index, "dictionary", 3, intruder)

    def test_legacy_record_without_key_falls_back_to_index(self, tmp_path):
        append_feedback(tmp_path, "ch01_chunk_000", "grammar", 2, "false_positive")
        by_key, by_index = build_dismissed(self._records(tmp_path))

        assert is_dismissed(by_key, by_index, "grammar", 2, RAW_ISSUE)
        assert not is_dismissed(by_key, by_index, "grammar", 5, RAW_ISSUE)

    def test_all_four_labels_count_as_dismissal(self, tmp_path):
        """The four types are tuning signal; for display they are equivalent."""
        for i, label in enumerate(
            ["false_positive", "bad_message", "missing_context_gap", "resolved"]
        ):
            issue = dict(RAW_ISSUE, message=f"finding {i}")
            append_feedback(
                tmp_path,
                "ch01_chunk_000",
                "dialogue",
                i,
                label,
                key=issue_key("dialogue", issue),
            )
        by_key, by_index = build_dismissed(self._records(tmp_path))
        for i in range(4):
            issue = dict(RAW_ISSUE, message=f"finding {i}")
            assert is_dismissed(by_key, by_index, "dialogue", i, issue)

    def test_byte_identical_findings_share_one_dismissal(self, tmp_path):
        """Two findings identical in severity, message and location hash the
        same, so dismissing one dismisses both.

        Deliberate, and a behavior change from the positional scheme, where
        ``enumerate`` kept them distinct. A judge that emits a duplicate row for
        a repeated excerpt is describing the same defect twice, and marking it
        twice is busywork; the cost is that a genuine second occurrence sharing
        an identical location string cannot be marked separately. Pinned here so
        that trade stays a decision rather than an accident -- reversing it means
        adding an occurrence ordinal to the key, which invalidates every mark
        already stamped.
        """
        twin = dict(RAW_ISSUE)
        append_feedback(
            tmp_path,
            "ch01_chunk_000",
            "dialogue",
            0,
            "false_positive",
            key=issue_key("dialogue", RAW_ISSUE),
        )
        by_key, by_index = build_dismissed(self._records(tmp_path))

        assert is_dismissed(by_key, by_index, "dialogue", 0, RAW_ISSUE)
        assert is_dismissed(by_key, by_index, "dialogue", 1, twin)

    def test_missing_issue_still_matches_positionally(self, tmp_path):
        """Callers that cannot supply the issue object must not silently lose
        legacy dismissals."""
        append_feedback(tmp_path, "ch01_chunk_000", "grammar", 4, "resolved")
        by_key, by_index = build_dismissed(self._records(tmp_path))
        assert is_dismissed(by_key, by_index, "grammar", 4, None)
