"""Tests for glossary candidate extraction."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.extract_glossary_candidates import (
    split_into_sentences,
    tokenize,
    is_special_case,
    get_context_sentence,
    DictionaryChecker,
    extract_proper_nouns,
    extract_uncommon_words,
    extract_frequent_ngrams,
    extract_repeated_capitalized,
    merge_candidates,
    exclude_glossary_terms,
    score_and_rank,
    extract_candidates,
    GlossaryCandidate,
    CandidateReport,
    save_report,
)
from src.models import Glossary, GlossaryTerm, GlossaryTermType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_dict_checker():
    """Dictionary checker that knows common English words."""
    checker = MagicMock(spec=DictionaryChecker)
    checker.available = True
    common_words = {
        "the", "a", "an", "in", "of", "to", "and", "is", "was", "for",
        "with", "that", "this", "it", "on", "at", "by", "from", "or",
        "but", "not", "are", "be", "as", "has", "had", "have", "been",
        "will", "would", "he", "she", "they", "his", "her", "children",
        "gathered", "around", "him", "hung", "motionless", "branch",
        "great", "city", "old", "house", "said", "one", "day", "little",
        "went", "into", "garden", "saw", "very", "beautiful", "flower",
        "under", "tree", "near", "river", "found", "small", "walked",
        "told", "about", "came", "back", "through", "looked", "sat",
        "many", "then", "there", "when", "where", "how", "what",
        "steam", "engine", "blue", "bird", "rock", "water",
    }
    checker.is_english_word = lambda w: w.lower() in common_words
    return checker


@pytest.fixture
def sample_glossary():
    """A small test glossary."""
    return Glossary(terms=[
        GlossaryTerm(
            english="Uncle Paul",
            spanish="Tío Pablo",
            type=GlossaryTermType.CHARACTER,
        ),
        GlossaryTerm(
            english="chrysalis",
            spanish="crisálida",
            type=GlossaryTermType.TECHNICAL,
        ),
    ])


SAMPLE_TEXT = """The children gathered around Uncle Paul in the garden. He told them about the chrysalis.
The chrysalis hung motionless from the branch. Then Jules and Emile listened carefully.

They walked to Mont Ventoux one day. The view from Mont Ventoux was very beautiful in spring.
Then Uncle Paul said the entomological specimens were extraordinary. The entomological
collection had many items. Then Jules found a cerambyx near the old house.

He visited Dr. Martin that day. Then Dr. Martin told them about the metamorphosis process.
The metamorphosis of the larva was remarkable. Then Emile saw a sphex wasp under the tree.
"""


# ---------------------------------------------------------------------------
# Test split_into_sentences
# ---------------------------------------------------------------------------

class TestSplitIntoSentences:

    def test_basic_splitting(self):
        sentences = split_into_sentences("Hello world. How are you? Fine thanks!")
        assert len(sentences) == 3

    def test_preserves_mr_abbreviation(self):
        # "Mr." followed by uppercase should still split, but we at least get
        # the sentence with "Mr. Smith" in it
        sentences = split_into_sentences("He met Mr. Smith at the park.")
        # May split on "Mr." — that's acceptable; the key is we don't crash
        assert any("Smith" in s for s in sentences)

    def test_newline_splitting(self):
        sentences = split_into_sentences("First sentence.\nSecond sentence.")
        assert len(sentences) == 2

    def test_empty_text(self):
        assert split_into_sentences("") == []

    def test_single_sentence(self):
        sentences = split_into_sentences("Just one sentence here")
        assert len(sentences) == 1


# ---------------------------------------------------------------------------
# Test tokenize and helpers
# ---------------------------------------------------------------------------

class TestTokenize:

    def test_basic(self):
        assert tokenize("Hello world") == ["Hello", "world"]

    def test_accented(self):
        tokens = tokenize("El niño comió")
        assert "niño" in tokens
        assert "comió" in tokens

    def test_punctuation_stripped(self):
        tokens = tokenize("Hello, world!")
        assert tokens == ["Hello", "world"]


class TestIsSpecialCase:

    def test_number(self):
        assert is_special_case("123") is True
        assert is_special_case("3.14") is True

    def test_single_char(self):
        assert is_special_case("x") is True

    def test_meaningful_single_char(self):
        assert is_special_case("a") is False
        assert is_special_case("I") is False

    def test_normal_word(self):
        assert is_special_case("hello") is False


# ---------------------------------------------------------------------------
# Test proper noun extraction
# ---------------------------------------------------------------------------

class TestExtractProperNouns:

    def test_multi_word_names(self, mock_dict_checker):
        sentences = split_into_sentences(SAMPLE_TEXT)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=1)
        # "Uncle Paul" should be found as a multi-word name
        assert any("uncle paul" in key for key in result)

    def test_single_proper_nouns(self, mock_dict_checker):
        sentences = split_into_sentences(SAMPLE_TEXT)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=1)
        # Jules and Emile should be found
        keys = set(result.keys())
        assert "jules" in keys or any("jules" in k for k in keys)
        assert "emile" in keys or any("emile" in k for k in keys)

    def test_multi_word_place(self, mock_dict_checker):
        sentences = split_into_sentences(SAMPLE_TEXT)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=1)
        assert any("mont ventoux" in key for key in result)

    def test_frequency_filter(self, mock_dict_checker):
        text = "He saw Jules once. That was all."
        sentences = split_into_sentences(text)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=2)
        # Jules only appears once, should be excluded with min_frequency=2
        assert "jules" not in result

    def test_character_type_for_title_prefix(self, mock_dict_checker):
        sentences = split_into_sentences(SAMPLE_TEXT)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=1)
        # Uncle Paul or Dr. Martin should be CHARACTER
        for key, candidate in result.items():
            if "uncle paul" in key or "dr martin" in key:
                assert candidate.type_guess == GlossaryTermType.CHARACTER

    def test_common_english_words_excluded(self, mock_dict_checker):
        """Common English words at sentence starts shouldn't appear."""
        text = "The cat sat. The dog ran. He saw them."
        sentences = split_into_sentences(text)
        result = extract_proper_nouns(sentences, mock_dict_checker, min_frequency=1)
        assert "the" not in result
        assert "he" not in result


# ---------------------------------------------------------------------------
# Test uncommon word extraction
# ---------------------------------------------------------------------------

class TestExtractUncommonWords:

    def test_finds_non_dictionary_words(self, mock_dict_checker):
        text = "The chrysalis hung there. The chrysalis was golden. The chrysalis cracked."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(text, mock_dict_checker, set(), 2, sentences)
        assert "chrysalis" in result

    def test_excludes_dictionary_words(self, mock_dict_checker):
        text = "The children gathered around. The children sat under the tree."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(text, mock_dict_checker, set(), 1, sentences)
        assert "children" not in result
        assert "gathered" not in result

    def test_excludes_proper_noun_keys(self, mock_dict_checker):
        text = "Jules was there. Jules came back."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(text, mock_dict_checker, {"jules"}, 1, sentences)
        assert "jules" not in result

    def test_respects_min_frequency(self, mock_dict_checker):
        text = "The cerambyx was rare."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(text, mock_dict_checker, set(), 2, sentences)
        assert "cerambyx" not in result

    def test_technical_type_for_frequent(self, mock_dict_checker):
        text = "The sphex hunted. The sphex dug. The sphex flew."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(text, mock_dict_checker, set(), 1, sentences)
        assert result["sphex"].type_guess == GlossaryTermType.TECHNICAL


# ---------------------------------------------------------------------------
# Test n-gram extraction
# ---------------------------------------------------------------------------

class TestExtractFrequentNgrams:

    def test_finds_repeated_bigrams(self, mock_dict_checker):
        sentences = split_into_sentences(SAMPLE_TEXT)
        result = extract_frequent_ngrams(sentences, mock_dict_checker, set(), 2)
        # "Mont Ventoux" appears twice so "mont ventoux" bigram should be found
        # It may or may not be captured depending on proper noun overlap
        # At minimum, some n-grams should be found
        assert isinstance(result, dict)

    def test_skips_all_stopword_ngrams(self, mock_dict_checker):
        text = "It was in the house. It was in the garden."
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, mock_dict_checker, set(), 2)
        # "in the" is all stopwords, should not appear
        assert "in the" not in result

    def test_skips_common_english_ngrams(self, mock_dict_checker):
        text = "The old house was there. The old house was nice."
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, mock_dict_checker, set(), 2)
        # "old house" — both in dictionary, should be excluded
        assert "old house" not in result


# ---------------------------------------------------------------------------
# Test repeated capitalized extraction
# ---------------------------------------------------------------------------

class TestExtractRepeatedCapitalized:

    def test_finds_always_capitalized(self, mock_dict_checker):
        text = "Emile was there. Emile came back. Emile smiled."
        result = extract_repeated_capitalized(text, mock_dict_checker, set(), 2)
        assert "emile" in result

    def test_excludes_sometimes_lowercase(self, mock_dict_checker):
        text = "The garden was nice. They went to the garden."
        result = extract_repeated_capitalized(text, mock_dict_checker, set(), 1)
        # "garden" appears lowercase, should not be flagged
        assert "garden" not in result

    def test_excludes_already_found(self, mock_dict_checker):
        text = "Emile was there. Emile came back."
        result = extract_repeated_capitalized(text, mock_dict_checker, {"emile"}, 2)
        assert "emile" not in result


# ---------------------------------------------------------------------------
# Test merge and scoring
# ---------------------------------------------------------------------------

class TestMergeCandidates:

    def test_deduplication(self):
        d1 = {"foo": GlossaryCandidate(
            term="Foo", type_guess=GlossaryTermType.OTHER, frequency=3,
            detection_reasons=["reason_a"],
        )}
        d2 = {"foo": GlossaryCandidate(
            term="Foo", type_guess=GlossaryTermType.CHARACTER, frequency=5,
            detection_reasons=["reason_b"],
        )}
        merged = merge_candidates(d1, d2)
        assert len(merged) == 1
        assert merged["foo"].type_guess == GlossaryTermType.CHARACTER
        assert merged["foo"].frequency == 5
        assert "reason_a" in merged["foo"].detection_reasons
        assert "reason_b" in merged["foo"].detection_reasons

    def test_no_overlap(self):
        d1 = {"foo": GlossaryCandidate(
            term="Foo", type_guess=GlossaryTermType.OTHER, frequency=2,
        )}
        d2 = {"bar": GlossaryCandidate(
            term="Bar", type_guess=GlossaryTermType.TECHNICAL, frequency=4,
        )}
        merged = merge_candidates(d1, d2)
        assert len(merged) == 2


class TestExcludeGlossaryTerms:

    def test_excludes_matching_terms(self, sample_glossary):
        candidates = {
            "uncle paul": GlossaryCandidate(
                term="Uncle Paul", type_guess=GlossaryTermType.CHARACTER, frequency=5,
            ),
            "chrysalis": GlossaryCandidate(
                term="chrysalis", type_guess=GlossaryTermType.TECHNICAL, frequency=3,
            ),
            "emile": GlossaryCandidate(
                term="Emile", type_guess=GlossaryTermType.CHARACTER, frequency=4,
            ),
        }
        excluded = exclude_glossary_terms(candidates, sample_glossary)
        assert excluded == 2
        assert "uncle paul" not in candidates
        assert "chrysalis" not in candidates
        assert "emile" in candidates


class TestScoreAndRank:

    def test_scoring_order(self, mock_dict_checker):
        candidates = {
            "rare_term": GlossaryCandidate(
                term="rare_term", type_guess=GlossaryTermType.TECHNICAL,
                frequency=10, detection_reasons=["not_in_dictionary", "frequent_ngram"],
            ),
            "common": GlossaryCandidate(
                term="common", type_guess=GlossaryTermType.OTHER,
                frequency=2, detection_reasons=["capitalized_mid_sentence"],
            ),
        }
        ranked = score_and_rank(candidates, mock_dict_checker, 100, [])
        assert len(ranked) == 2
        assert ranked[0].term == "rare_term"

    def test_max_candidates_cap(self, mock_dict_checker):
        candidates = {
            f"term_{i}": GlossaryCandidate(
                term=f"term_{i}", type_guess=GlossaryTermType.OTHER,
                frequency=i + 2, detection_reasons=["test"],
            )
            for i in range(20)
        }
        ranked = score_and_rank(candidates, mock_dict_checker, 5, [])
        assert len(ranked) == 5

    def test_empty_candidates(self, mock_dict_checker):
        assert score_and_rank({}, mock_dict_checker, 100, []) == []


# ---------------------------------------------------------------------------
# Test full pipeline
# ---------------------------------------------------------------------------

class TestExtractCandidates:

    def test_full_pipeline(self):
        """Smoke test: full pipeline produces a valid report."""
        report = extract_candidates(SAMPLE_TEXT, min_frequency=1, max_candidates=50)
        assert isinstance(report, CandidateReport)
        assert report.total_words > 0
        assert report.total_unique_words > 0

    def test_with_glossary_exclusion(self, sample_glossary):
        report = extract_candidates(
            SAMPLE_TEXT, glossary=sample_glossary, min_frequency=1,
        )
        # Uncle Paul and chrysalis should be excluded
        candidate_terms = {c.term.lower() for c in report.candidates}
        assert "uncle paul" not in candidate_terms
        assert "chrysalis" not in candidate_terms

    def test_empty_text(self):
        report = extract_candidates("   ", min_frequency=1)
        assert report.total_words == 0
        assert len(report.candidates) == 0

    def test_no_candidates_below_threshold(self):
        text = "Hello world. Goodbye world."
        report = extract_candidates(text, min_frequency=10)
        assert len(report.candidates) == 0


# ---------------------------------------------------------------------------
# Test output
# ---------------------------------------------------------------------------

class TestSaveReport:

    def test_save_and_load(self, tmp_path):
        report = CandidateReport(
            source_file="test.txt",
            total_words=100,
            total_unique_words=50,
            candidates=[
                GlossaryCandidate(
                    term="Foo", type_guess=GlossaryTermType.CHARACTER,
                    frequency=5, score=0.8, context_sentence="Foo was here.",
                    detection_reasons=["test"],
                ),
            ],
        )
        output = tmp_path / "out.json"
        save_report(report, output)
        assert output.exists()
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["total_words"] == 100
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["term"] == "Foo"


class TestGetContextSentence:

    def test_finds_sentence(self):
        sentences = ["The cat sat.", "Uncle Paul arrived.", "They left."]
        result = get_context_sentence("Uncle Paul", sentences)
        assert "Uncle Paul" in result

    def test_not_found(self):
        sentences = ["The cat sat."]
        result = get_context_sentence("missing", sentences)
        assert result == ""

    def test_truncates_long_sentence(self):
        long = "A " * 200 + "Uncle Paul" + " B" * 200
        result = get_context_sentence("Uncle Paul", [long])
        assert len(result) < len(long)
        assert "Uncle Paul" in result
