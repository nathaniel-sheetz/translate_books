"""The flagged word must reach the UI as a field, not as a substring.

``Issue.term`` exists so a finding has a stable handle on the word it is about.
Before it, the word survived only inside the message (``'Sigfridos': Unknown
word...``) and every consumer had to regex it back out.

``NormalizedIssue`` is the half of this that is easy to get wrong: it is the
view that reaches ``normalized_issues[]``, the reader and the dashboard, and it
silently dropped ``rule_id``/``category`` for the whole of their first release.
Dropping ``term`` the same way would leave the ignore list with nothing to key
on, so the fan-out is pinned here too.
"""

from datetime import datetime

import pytest

from src.evaluators.dictionary_eval import DictionaryEvaluator
from src.evaluators.location_normalizer import NormalizedIssue, fan_out_issues
from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    EvalResult,
    Issue,
    IssueLevel,
)


@pytest.fixture
def chunk():
    return Chunk(
        id="chapter_01_chunk_000",
        chapter_id="chapter_01",
        position=1,
        source_text="This is a test.",
        translated_text="Esta es una preuba con Sigfridos.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=6,
        ),
        status=ChunkStatus.TRANSLATED,
    )


class TestDictionaryEvaluator:
    def test_flagged_word_lands_on_the_issue(self, chunk):
        chunk.translated_text = "Esta es una preuba con errores."
        result = DictionaryEvaluator().evaluate(chunk, {})
        flagged = [i for i in result.issues if i.term]
        assert flagged, "every dictionary finding is about a specific word"
        for issue in flagged:
            # The term is the authority; the message merely quotes it.
            assert issue.message.startswith("'" + issue.term + "'")

    def test_term_matches_the_english_word_it_names(self, chunk):
        chunk.translated_text = "Esta es una sentence con errores."
        result = DictionaryEvaluator().evaluate(chunk, {})
        assert "sentence" in {i.term for i in result.issues}


class TestIssueModel:
    def test_term_defaults_to_none(self):
        """Additive and optional: evaluations stored before it still parse."""
        issue = Issue(severity=IssueLevel.WARNING, message="m")
        assert issue.term is None

    def test_legacy_payload_still_validates(self):
        issue = Issue.model_validate(
            {"severity": "warning", "message": "m", "location": "char 1"}
        )
        assert issue.term is None
        assert issue.rule_id is None


class TestFanOut:
    def _result(self, issue):
        return EvalResult(
            eval_name="dictionary",
            eval_version="1.0.0",
            target_id="chapter_01_chunk_000",
            target_type="chunk",
            passed=False,
            score=0.9,
            issues=[issue],
            metadata={},
            executed_at=datetime.now(),
        )

    def test_identity_survives_onto_the_normalized_view(self, chunk):
        issue = Issue(
            severity=IssueLevel.WARNING,
            message="'Sigfridos': Unknown word (found 1 time(s))",
            location="Character position 23",
            term="Sigfridos",
            rule_id="SOME_RULE",
            category="TYPOS",
        )
        out = fan_out_issues(self._result(issue), chunk)
        assert out
        for ni in out:
            assert ni.term == "Sigfridos"
            assert ni.rule_id == "SOME_RULE"
            assert ni.category == "TYPOS"

    def test_identity_survives_serialization(self, chunk):
        issue = Issue(
            severity=IssueLevel.WARNING,
            message="'Sigfridos': Unknown word (found 1 time(s))",
            location="Character position 23",
            term="Sigfridos",
        )
        data = fan_out_issues(self._result(issue), chunk)[0].to_dict()
        assert data["term"] == "Sigfridos"
        assert "rule_id" in data and "category" in data

    def test_every_fanned_out_location_shares_the_term(self, chunk):
        """One Issue, several positions -- an ignore must clear all of them,
        which only works if they all name the same term."""
        chunk.translated_text = "Sigfridos y luego Sigfridos otra vez aqui."
        issue = Issue(
            severity=IssueLevel.WARNING,
            message="'Sigfridos': Unknown word (found 2 time(s))",
            location="Character positions: 0, 18",
            term="Sigfridos",
        )
        out = fan_out_issues(self._result(issue), chunk)
        assert len(out) == 2
        assert {ni.term for ni in out} == {"Sigfridos"}

    def test_defaults_are_none_for_evaluators_without_a_word(self, chunk):
        issue = Issue(
            severity=IssueLevel.WARNING,
            message="Translation is 50% shorter than expected",
        )
        out = fan_out_issues(self._result(issue), chunk)
        for ni in out:
            assert ni.term is None
            assert ni.rule_id is None


class TestNormalizedIssueDefaults:
    def test_constructible_without_the_new_fields(self):
        """Positional construction elsewhere in the codebase must not break."""
        ni = NormalizedIssue(
            eval_name="length",
            eval_version="1.0.0",
            issue_index=0,
            severity="warning",
            message="m",
            suggestion=None,
            location=None,
        )
        assert ni.term is None
        assert ni.rule_id is None
        assert ni.category is None
