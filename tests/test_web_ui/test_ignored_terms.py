"""Tests for the per-book ignore list.

A dismissal is one human judgment about one finding. An ignore is one judgment
about a *term*, applied to every finding that names it, for the whole book --
written from the reader, cleared only from the dashboard.

Two properties carry most of the risk and are pinned hardest here:

* **Grammar is keyed on ``(rule_id, term)``, never on the term alone.** The
  words reviewers actually ignore in grammar findings are function words (``el``
  occurs 1412x in one book), so a word-only entry would silence every present
  and future rule on that token.
* **The match is accent-sensitive**, unlike ``Glossary.matches_word``. Folding
  accents would make ignoring ``nivea`` also silence the accented spelling, and
  a missing tilde is one of the few genuine Spanish typos this evaluator catches.
"""

import json

import pytest

from src.models import IgnoredTerm, IgnoredTerms
from src.utils.file_io import load_ignored_terms, save_ignored_terms
from web_ui.evaluations import (
    IgnoreHits,
    count_ignored_hits,
    issue_key,
    is_ignored,
    issue_term,
    load_project_ignored_terms,
)


def _dict_issue(word, position=10):
    """A dictionary finding in the shape ``normalized_issues[]`` stores."""
    return {
        "eval_name": "dictionary",
        "issue_index": 0,
        "severity": "warning",
        "message": (
            "'" + word + "': Unknown word (not in Spanish or English dictionary)"
            " (found 1 time(s))"
        ),
        "location": {
            "raw": "Character position " + str(position),
            "side": "target",
            "char_start": position,
            "char_end": position + len(word),
        },
        "term": word,
        "rule_id": None,
    }


def _grammar_issue(word, rule_id):
    return {
        "eval_name": "grammar",
        "issue_index": 0,
        "severity": "warning",
        "message": "Se escribe con tilde si equivale a todavia.",
        "location": {
            "raw": "char 42-45",
            "side": "target",
            "char_start": 42,
            "char_end": 45,
        },
        "term": word,
        "rule_id": rule_id,
    }


class TestMatching:
    def test_dictionary_matches_on_the_word(self):
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Pecquigny", eval_name="dictionary")])
        assert ig.matches("dictionary", "Pecquigny")

    def test_case_insensitive(self):
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Pecquigny", eval_name="dictionary")])
        assert ig.matches("dictionary", "PECQUIGNY")
        assert ig.matches("dictionary", "pecquigny")

    def test_accent_sensitive(self):
        """An unaccented entry must not silence the accented word."""
        ig = IgnoredTerms(terms=[IgnoredTerm(term="nivea", eval_name="dictionary")])
        assert ig.matches("dictionary", "nivea")
        assert not ig.matches("dictionary", "nívea")

    def test_no_plural_expansion(self):
        """Unlike the glossary, which generates plurals for curated terms."""
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Atlas", eval_name="dictionary")])
        assert not ig.matches("dictionary", "Atlases")
        ig2 = IgnoredTerms(
            terms=[IgnoredTerm(term="épeira", eval_name="dictionary")]
        )
        assert not ig2.matches("dictionary", "épeiras")

    def test_scoped_to_its_evaluator(self):
        ig = IgnoredTerms(terms=[IgnoredTerm(term="aun", eval_name="dictionary")])
        assert not ig.matches("grammar", "aun", rule_id="AUN")

    def test_empty_and_missing_terms_never_match(self):
        ig = IgnoredTerms(terms=[IgnoredTerm(term="x", eval_name="dictionary")])
        assert not ig.matches("dictionary", None)
        assert not ig.matches("dictionary", "")
        assert not ig.matches("dictionary", "   ")

    def test_empty_list_matches_nothing(self):
        assert not IgnoredTerms().matches("dictionary", "anything")


class TestGrammarIsKeyedOnThePair:
    def test_suppresses_only_its_own_rule(self):
        ig = IgnoredTerms(
            terms=[IgnoredTerm(term="aun", eval_name="grammar", rule_id="AUN")]
        )
        assert ig.matches("grammar", "aun", rule_id="AUN")
        assert not ig.matches("grammar", "aun", rule_id="COMMA_ADVERB")
        assert not ig.matches("grammar", "aun", rule_id="MAYUSCULAS_INICIO_FRASE")

    def test_finding_without_a_rule_id_is_never_ignored(self):
        """Evaluations written before rule ids were persisted stay visible."""
        ig = IgnoredTerms(
            terms=[IgnoredTerm(term="aun", eval_name="grammar", rule_id="AUN")]
        )
        assert not ig.matches("grammar", "aun", rule_id=None)
        assert not is_ignored(ig, "grammar", _grammar_issue("aun", None))

    def test_entry_without_a_rule_id_cannot_catch_all_rules(self):
        """A malformed entry must not degrade into a word-only grammar ignore."""
        ig = IgnoredTerms(terms=[IgnoredTerm(term="el", eval_name="grammar")])
        assert not ig.matches("grammar", "el", rule_id="EL_TILDE")


class TestIssueTermRecovery:
    def test_prefers_the_stored_field(self):
        issue = {"term": "Deum", "message": "'Other': x"}
        assert issue_term("dictionary", issue) == "Deum"

    def test_recovers_from_the_message_on_the_legacy_corpus(self):
        """Every evaluation written before Issue.term has ``term: null``."""
        legacy = dict(_dict_issue("Sigfridos"))
        legacy["term"] = None
        assert issue_term("dictionary", legacy) == "Sigfridos"

    def test_no_recovery_for_grammar(self):
        """LanguageTool messages are Spanish prose, never a quoted token."""
        legacy = dict(_grammar_issue("aun", "AUN"))
        legacy["term"] = None
        assert issue_term("grammar", legacy) is None

    @pytest.mark.parametrize(
        "word",
        [
            # Modern words with an internal apostrophe.
            "d'Artagnan", "O'Brien", "pa'l", "Nag's",
            # Legacy rows: the old tokenizer's character class included the
            # apostrophe, so it swallowed the quote marks around a word.
            "'Victory'", "'Sí'", "despacio'", "d'oïl_",
        ],
    )
    def test_an_apostrophe_in_the_word_does_not_end_the_term(self, word):
        """The delimiter is ``': ``, not the first apostrophe.

        Splitting on the apostrophe failed two ways at once. A word with one
        inside truncated to its first letter (``d'oïl_`` -> ``d``), which is
        then discarded as a single character; a word with one on its *edge*
        made the pattern fail outright, so ``issue_term`` returned None and the
        finding fell out of every term-keyed join without a trace. Both shapes
        are on disk in the corpus today.
        """
        legacy = dict(_dict_issue(word))
        legacy["term"] = None
        assert issue_term("dictionary", legacy) == word

    def test_the_term_stops_at_the_first_delimiter(self):
        """Non-greedy: a reason string that grows a ``': `` of its own must not
        be swallowed into the term."""
        legacy = {
            "term": None,
            "message": "'casa': Unknown word: see 'nota': below (found 1 time(s))",
        }
        assert issue_term("dictionary", legacy) == "casa"

    def test_recovers_from_the_english_word_message_too(self):
        """The other message shape this evaluator emits."""
        legacy = {
            "term": None,
            "message": "'pa'l': English word in translation (found 2 time(s))",
        }
        assert issue_term("dictionary", legacy) == "pa'l"

    def test_legacy_dictionary_finding_is_ignorable_without_a_rerun(self):
        ig = IgnoredTerms(
            terms=[IgnoredTerm(term="Sigfridos", eval_name="dictionary")]
        )
        legacy = dict(_dict_issue("Sigfridos"))
        legacy["term"] = None
        assert is_ignored(ig, "dictionary", legacy)


class TestIsIgnored:
    def test_ignoring_an_apostrophe_word_does_not_ignore_its_first_letter(self):
        """The half of the truncated parse that changes what a reader sees.

        ``issue_term`` keys the ignore list as well as the replay join, so a
        human ignoring ``pa'l`` on a legacy finding used to record ``pa`` --
        and then every ``pa`` in the book went quiet, book-wide, from one
        click. The corpus has no apostrophe in any ignore list yet, so this
        pins the behavior before it can be introduced rather than after.
        """
        ig = IgnoredTerms(terms=[IgnoredTerm(term="pa", eval_name="dictionary")])
        legacy = dict(_dict_issue("pa'l"))
        legacy["term"] = None
        assert not is_ignored(ig, "dictionary", legacy)

        exact = IgnoredTerms(terms=[IgnoredTerm(term="pa'l", eval_name="dictionary")])
        assert is_ignored(exact, "dictionary", legacy)

    def test_none_list_ignores_nothing(self):
        assert not is_ignored(None, "dictionary", _dict_issue("x"))

    def test_none_issue_ignores_nothing(self):
        ig = IgnoredTerms(terms=[IgnoredTerm(term="x", eval_name="dictionary")])
        assert not is_ignored(ig, "dictionary", None)


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "ignored_terms.json"
        save_ignored_terms(
            IgnoredTerms(
                terms=[
                    IgnoredTerm(
                        term="Pecquigny",
                        eval_name="dictionary",
                        added_from="chapter_04_chunk_001",
                        note="French place name",
                    ),
                    IgnoredTerm(term="aun", eval_name="grammar", rule_id="AUN"),
                ]
            ),
            path,
        )
        loaded = load_ignored_terms(path)
        assert [t.term for t in loaded.terms] == ["Pecquigny", "aun"]
        assert loaded.terms[0].note == "French place name"
        assert loaded.terms[1].rule_id == "AUN"

    def test_written_as_readable_utf8(self, tmp_path):
        path = tmp_path / "ignored_terms.json"
        save_ignored_terms(
            IgnoredTerms(
                terms=[IgnoredTerm(term="Vámonos", eval_name="dictionary")]
            ),
            path,
        )
        assert "Vámonos" in path.read_text(encoding="utf-8")

    def test_removal_is_a_real_delete_not_a_tombstone(self, tmp_path):
        """Unlike ``_feedback.jsonl``, this file is rewritten wholesale."""
        path = tmp_path / "ignored_terms.json"
        save_ignored_terms(
            IgnoredTerms(
                terms=[
                    IgnoredTerm(term="a", eval_name="dictionary"),
                    IgnoredTerm(term="b", eval_name="dictionary"),
                ]
            ),
            path,
        )
        kept = [t for t in load_ignored_terms(path).terms if t.term != "a"]
        save_ignored_terms(IgnoredTerms(terms=kept), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert [t["term"] for t in data["terms"]] == ["b"]

    def test_project_loader_returns_none_when_absent(self, tmp_path):
        assert load_project_ignored_terms(tmp_path) is None

    def test_project_loader_degrades_on_corruption(self, tmp_path):
        """A malformed file must mean nothing is ignored, not a blank queue."""
        (tmp_path / "ignored_terms.json").write_text("{not json", encoding="utf-8")
        assert load_project_ignored_terms(tmp_path) is None

    def test_an_unknown_schema_version_is_refused(self, tmp_path):
        """Loading a newer schema lossily would persist the loss on the next write.

        Pydantic drops unknown fields, and every add and remove rewrites this
        file wholesale, so a silently-downgraded load is a one-way door. The
        write routes turn this refusal into a 409.
        """
        path = tmp_path / "ignored_terms.json"
        path.write_text(
            json.dumps({
                "version": 2,
                "terms": [{"term": "Deum", "eval_name": "dictionary"}],
                "future_field": "would be dropped on rewrite",
            }),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="version"):
            load_ignored_terms(path)
        assert load_project_ignored_terms(tmp_path) is None

    def test_version_1_is_still_accepted(self, tmp_path):
        path = tmp_path / "ignored_terms.json"
        save_ignored_terms(
            IgnoredTerms(terms=[IgnoredTerm(term="Deum", eval_name="dictionary")]),
            path,
        )
        assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
        assert [t.term for t in load_ignored_terms(path).terms] == ["Deum"]


class TestIdentity:
    def test_case_folds_the_term(self):
        a = IgnoredTerm(term="Pecquigny", eval_name="dictionary")
        b = IgnoredTerm(term="  pecquigny  ", eval_name="dictionary")
        assert a.identity() == b.identity()

    def test_rule_separates_grammar_entries(self):
        a = IgnoredTerm(term="aun", eval_name="grammar", rule_id="AUN")
        b = IgnoredTerm(term="aun", eval_name="grammar", rule_id="AUN2")
        assert a.identity() != b.identity()


class TestCountIgnoredHits:
    """The count is split ``(live, dismissed)``.

    ``live`` is what clearing the entry restores; ``dismissed`` is the findings
    that name the same term but already carry a feedback label, which the
    dismissal keeps hidden whether or not the entry survives. Keeping them apart
    is what stops a ``0`` from reading as "this term went quiet" when the real
    story is "you dismissed all of them by hand first".
    """

    def _project(self, tmp_path, issues, feedback=()):
        ev = tmp_path / "evaluations"
        ev.mkdir(parents=True)
        (ev / "chapter_01_chunk_000.json").write_text(
            json.dumps(
                {"chunk_id": "chapter_01_chunk_000", "normalized_issues": issues}
            ),
            encoding="utf-8",
        )
        if feedback:
            (ev / "_feedback.jsonl").write_text(
                "".join(json.dumps(f) + "\n" for f in feedback),
                encoding="utf-8",
            )
        return tmp_path

    def test_counts_live_findings_per_entry(self, tmp_path):
        proj = self._project(
            tmp_path,
            [_dict_issue("Deum", 10), _dict_issue("Deum", 40), _dict_issue("Laudamus", 70)],
        )
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Deum", eval_name="dictionary")])
        hits = count_ignored_hits(proj, ig)
        assert hits[("dictionary", "deum", None)] == IgnoreHits(live=2, dismissed=0)

    def test_entry_that_hides_nothing_reports_zero(self, tmp_path):
        proj = self._project(tmp_path, [_dict_issue("Deum")])
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Vexin", eval_name="dictionary")])
        assert count_ignored_hits(proj, ig)[("dictionary", "vexin", None)] == IgnoreHits()

    def test_already_dismissed_findings_land_in_the_second_slot(self, tmp_path):
        """The five-little-peppers case: dismissed by hand, ignored afterwards.

        The dismissal got there first, so clearing the entry restores nothing --
        but the term is not dormant, and a bare ``0`` would say it was.
        """
        dismissed_here = _dict_issue("Mirandy", 10)
        proj = self._project(
            tmp_path,
            [dismissed_here, _dict_issue("Mirandy", 40)],
            feedback=[
                {
                    "chunk_id": "chapter_01_chunk_000",
                    "eval_name": "dictionary",
                    "issue_key": issue_key("dictionary", dismissed_here),
                    "feedback_type": "resolved",
                }
            ],
        )
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Mirandy", eval_name="dictionary")])
        hits = count_ignored_hits(proj, ig)
        assert hits[("dictionary", "mirandy", None)] == IgnoreHits(live=1, dismissed=1)

    def test_a_term_dismissed_everywhere_still_reports_its_dismissals(self, tmp_path):
        issue = _dict_issue("Dicky", 10)
        proj = self._project(
            tmp_path,
            [issue],
            feedback=[
                {
                    "chunk_id": "chapter_01_chunk_000",
                    "eval_name": "dictionary",
                    "issue_key": issue_key("dictionary", issue),
                    "feedback_type": "false_positive",
                }
            ],
        )
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Dicky", eval_name="dictionary")])
        assert count_ignored_hits(proj, ig)[("dictionary", "dicky", None)] == IgnoreHits(
            live=0, dismissed=1
        )

    def test_stale_chunks_are_counted_in_neither_slot(self, tmp_path):
        ev = tmp_path / "evaluations"
        ev.mkdir(parents=True)
        (ev / "chapter_01_chunk_000.json").write_text(
            json.dumps(
                {
                    "chunk_id": "chapter_01_chunk_000",
                    "stale": True,
                    "normalized_issues": [_dict_issue("Deum")],
                }
            ),
            encoding="utf-8",
        )
        ig = IgnoredTerms(terms=[IgnoredTerm(term="Deum", eval_name="dictionary")])
        assert count_ignored_hits(tmp_path, ig)[("dictionary", "deum", None)] == IgnoreHits()

    def test_empty_list_is_a_no_op(self, tmp_path):
        assert count_ignored_hits(tmp_path, IgnoredTerms()) == {}
        assert count_ignored_hits(tmp_path, None) == {}
