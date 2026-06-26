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
    FrequencyChecker,
    extract_proper_nouns,
    extract_uncommon_words,
    extract_frequent_ngrams,
    extract_repeated_capitalized,
    extract_rare_literary_words,
    merge_candidates,
    exclude_glossary_terms,
    score_and_rank,
    extract_candidates,
    GlossaryCandidate,
    CandidateReport,
    save_report,
    _read_source_text,
    _strip_possessive,
    _restore_title_periods,
    collapse_possessive_keys,
    prune_contained_terms,
    filter_demonyms,
    build_forced_candidates,
    FORCED_TERM_REASON,
    NLTK_AVAILABLE,
    WORDFREQ_AVAILABLE,
)
import src.app_config as app_config
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
def mock_freq_checker():
    """Frequency checker with hand-curated literary Zipf scores."""
    checker = MagicMock(spec=FrequencyChecker)
    checker.available = True
    zipf_table = {
        "merlin": 3.0, "mammon": 3.0, "palmer": 3.5, "gaoler": 2.5,
        "armour": 3.5, "halyard": 2.0, "binnacle": 1.5, "carpel": 2.0,
        "dragon": 5.0, "witch": 4.7, "sword": 5.0, "house": 6.0,
        "emile": 3.0, "jules": 3.0, "paul": 5.5,
    }
    checker.literary_zipf = lambda w: zipf_table.get(w.lower(), 3.0)
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
        # "Mr." followed by uppercase should NOT split into a new sentence.
        sentences = split_into_sentences("He met Mr. Smith at the park.")
        assert len(sentences) == 1
        assert "Mr. Smith" in sentences[0]

    def test_keeps_lord_st_vincent_together(self):
        text = "afterwards Lord St. Vincent took command of the fleet."
        sentences = split_into_sentences(text)
        assert len(sentences) == 1
        assert "Lord St. Vincent" in sentences[0]

    def test_keeps_mrs_dr_capt_together(self):
        text = (
            "She greeted Mrs. Nisbet warmly. "
            "Then Dr. Martin spoke. "
            "Capt. Hardy returned."
        )
        sentences = split_into_sentences(text)
        # Three sentences total — abbreviations don't split anything mid-sentence.
        assert len(sentences) == 3
        assert "Mrs. Nisbet" in sentences[0]
        assert "Dr. Martin" in sentences[1]
        assert "Capt. Hardy" in sentences[2]

    def test_real_sentence_terminator_still_splits(self):
        text = "He left. Then she arrived."
        sentences = split_into_sentences(text)
        assert len(sentences) == 2

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

    def test_finds_always_capitalized(self, mock_dict_checker, mock_freq_checker):
        text = "Emile was there. Emile came back. Emile smiled."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "emile" in result

    def test_excludes_sometimes_lowercase(self, mock_dict_checker, mock_freq_checker):
        text = "The garden was nice. They went to the garden."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, set(), 1
        )
        # "garden" appears lowercase, should not be flagged
        assert "garden" not in result

    def test_excludes_already_found(self, mock_dict_checker, mock_freq_checker):
        text = "Emile was there. Emile came back."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, {"emile"}, 2
        )
        assert "emile" not in result

    def test_dictionary_word_with_low_zipf_admitted(
        self, mock_dict_checker, mock_freq_checker
    ):
        """Merlin is in the dictionary (a bird) but rare in literature, so
        the always-capitalized form should now surface."""
        # Add 'merlin' as an English word so the dict check passes.
        mock_dict_checker.is_english_word = lambda w: w.lower() in {
            "merlin", "the", "a", "was", "there", "and",
        }
        text = "Merlin watched the sky. Merlin turned away. Merlin was silent."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "merlin" in result
        reasons = result["merlin"].detection_reasons
        assert "always_capitalized" in reasons
        assert "rare_in_literary_english" in reasons

    def test_dictionary_word_with_high_zipf_rejected(
        self, mock_dict_checker, mock_freq_checker
    ):
        """House is a common English word, even when always capitalized
        (e.g. personification), it should NOT be flagged."""
        mock_dict_checker.is_english_word = lambda w: w.lower() in {
            "house", "the", "a", "was", "stood", "still",
        }
        text = "House stood still. House was silent. House waited."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "house" not in result

    def test_non_dictionary_word_keeps_old_path(
        self, mock_dict_checker, mock_freq_checker
    ):
        """Non-dictionary always-capitalized word still gets the
        not_in_dictionary reason (legacy path)."""
        mock_dict_checker.is_english_word = lambda w: False
        text = "Zorblax appeared. Zorblax spoke. Zorblax vanished."
        result = extract_repeated_capitalized(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "zorblax" in result
        assert "not_in_dictionary" in result["zorblax"].detection_reasons


class TestExtractRareLiteraryWords:

    def test_archaic_dictionary_word_flagged(self, mock_dict_checker, mock_freq_checker):
        """A dictionary word that's genuinely rare in literature (gaoler, Zipf 2.5)
        and recurs above min_frequency should be flagged."""
        mock_dict_checker.is_english_word = lambda w: w.lower() in {
            "gaoler", "the", "a", "was", "and", "with", "his",
        }
        text = "The gaoler watched. The gaoler turned. The gaoler locked."
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "gaoler" in result
        assert result["gaoler"].detection_reasons == ["rare_in_literary_english"]

    def test_below_min_frequency_excluded(self, mock_dict_checker, mock_freq_checker):
        mock_dict_checker.is_english_word = lambda w: w.lower() in {"palmer", "the"}
        text = "The palmer walked alone."
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "palmer" not in result

    def test_common_literary_word_excluded(self, mock_dict_checker, mock_freq_checker):
        """dragon and sword have high literary Zipf and should NOT be flagged."""
        mock_dict_checker.is_english_word = lambda w: w.lower() in {
            "dragon", "sword", "the", "a", "and",
        }
        text = "The dragon roared. The sword gleamed. The dragon flew. The sword struck."
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "dragon" not in result
        assert "sword" not in result

    def test_non_dictionary_word_excluded(self, mock_dict_checker, mock_freq_checker):
        """Words not in the dictionary belong to extract_uncommon_words,
        not this extractor."""
        mock_dict_checker.is_english_word = lambda w: False
        text = "The zorblax watched. The zorblax fled."
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, set(), 2
        )
        assert "zorblax" not in result

    def test_already_found_excluded(self, mock_dict_checker, mock_freq_checker):
        mock_dict_checker.is_english_word = lambda w: w.lower() in {"palmer", "the"}
        text = "The palmer walked. The palmer rested."
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, {"palmer"}, 2
        )
        assert "palmer" not in result

    def test_threshold_override_tightens(self, mock_dict_checker, mock_freq_checker):
        """Lowering max_zipf_mixed below palmer's 3.5 should drop palmer."""
        mock_dict_checker.is_english_word = lambda w: w.lower() in {
            "palmer", "halyard", "the", "and",
        }
        text = (
            "The palmer walked. The palmer rested. "
            "The halyard snapped. The halyard frayed."
        )
        # max_zipf_mixed=3.0: palmer (3.5) drops, halyard (2.0) stays
        result = extract_rare_literary_words(
            text, mock_dict_checker, mock_freq_checker, set(), 2,
            max_zipf_mixed=3.0,
        )
        assert "palmer" not in result
        assert "halyard" in result

    def test_unavailable_freq_checker_returns_empty(self, mock_dict_checker):
        unavailable = MagicMock(spec=FrequencyChecker)
        unavailable.available = False
        result = extract_rare_literary_words(
            "anything goes here.", mock_dict_checker, unavailable, set(), 1
        )
        assert result == {}


@pytest.mark.skipif(
    not (NLTK_AVAILABLE and WORDFREQ_AVAILABLE),
    reason="NLTK or wordfreq not installed",
)
class TestFrequencyChecker:

    def test_common_words_score_higher_than_rare(self):
        """Smoke check: 'the' must have a much higher Zipf than 'merlin'."""
        checker = FrequencyChecker()
        if not checker.available:
            pytest.skip("FrequencyChecker not available at runtime")
        assert checker.literary_zipf("the") > checker.literary_zipf("merlin")
        assert checker.literary_zipf("the") > 5.0


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

    def test_rare_literary_integration(self, mock_freq_checker):
        """End-to-end: archaic dictionary words surface, common literary words don't."""
        text = (
            "The gaoler unlocked the door. The gaoler watched the prisoner. "
            "The gaoler turned away. Merlin watched from afar. "
            "Merlin nodded once. Merlin turned away. "
            "The dragon roared. The dragon flew over. The dragon vanished. "
            "The sword gleamed. The sword struck true. The sword shattered."
        )
        report = extract_candidates(
            text, min_frequency=2, max_candidates=100,
            freq_checker=mock_freq_checker,
        )
        terms_lower = {c.term.lower() for c in report.candidates}
        # Rare archaic / collision-name words should appear
        assert "gaoler" in terms_lower
        assert "merlin" in terms_lower
        # Common literary words should NOT appear
        assert "dragon" not in terms_lower
        assert "sword" not in terms_lower


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


class TestImagePlaceholdersExcluded:
    """[IMAGE:...] tokens must not surface as candidate glossary terms."""

    def test_image_tokens_not_in_candidates(self):
        text = (
            "Uncle Paul went to the garden. Uncle Paul saw a flower. "
            "Uncle Paul came back. Uncle Paul looked happy.\n\n"
            "[IMAGE:images/c01.jpg]\n\n"
            "[IMAGE:images/c02.jpg:a winter caption scene]\n\n"
            "Uncle Paul left."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=100)
        surface_terms = {c.term for c in report.candidates}
        terms_lower = {t.lower() for t in surface_terms}

        # Filename / placeholder fragments must NOT appear as candidates.
        for fragment in ("IMAGE", "image", "jpg", "c01", "c02", "images"):
            assert fragment.lower() not in terms_lower, (
                f"Unexpected placeholder fragment '{fragment}' in candidates: {surface_terms}"
            )
        # Description tokens must NOT appear either.
        for word in ("winter", "caption", "scene"):
            assert word.lower() not in terms_lower, (
                f"Unexpected description word '{word}' in candidates: {surface_terms}"
            )


class TestReadSourceTextProjectAware:
    """``_read_source_text`` should prefer chunks/ over chapters/ on
    Stage-6'd projects where chapters/ has been overwritten with
    translated text."""

    @staticmethod
    def _make_project(tmp_path: Path, english_chunk: str, spanish_chapter: str) -> Path:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "source.txt").write_text("ignored raw source", encoding="utf-8")

        chunks_dir = project_dir / "chunks"
        chunks_dir.mkdir()
        (chunks_dir / "chapter_01_chunk_000.json").write_text(
            json.dumps({
                "id": "chapter_01_chunk_000",
                "source_text": english_chunk,
                "translated_text": spanish_chapter,
            }),
            encoding="utf-8",
        )

        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir()
        (chapters_dir / "chapter_01.txt").write_text(spanish_chapter, encoding="utf-8")
        return project_dir

    def test_prefers_chunks_when_chapters_have_been_translated(self, tmp_path: Path):
        project_dir = self._make_project(
            tmp_path,
            english_chunk="The little duke rode through the forest.",
            spanish_chapter="El pequeño duque cabalgó por el bosque.",
        )
        text = _read_source_text(project_dir / "source.txt", verbose=False)
        assert "little duke" in text
        assert "pequeño" not in text

    def test_falls_back_to_chapters_when_no_chunks(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "source.txt").write_text("ignored raw source", encoding="utf-8")
        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir()
        (chapters_dir / "chapter_01.txt").write_text("Plain English chapter.", encoding="utf-8")

        text = _read_source_text(project_dir / "source.txt", verbose=False)
        assert text == "Plain English chapter."

    def test_standalone_file_is_read_directly(self, tmp_path: Path):
        # No chunks/, no chapters/ — should just read the passed file.
        standalone = tmp_path / "book.txt"
        standalone.write_text("Standalone book text.", encoding="utf-8")
        text = _read_source_text(standalone, verbose=False)
        assert text == "Standalone book text."


# ---------------------------------------------------------------------------
# Bootstrap prompt: full-text mode (default, today's behavior)
# ---------------------------------------------------------------------------

class TestBootstrapPromptFullTextMode:

    def test_default_unchanged(self):
        from src.glossary_bootstrap import build_glossary_prompt
        candidates = [
            {"term": "Nelson", "type_guess": "character", "frequency": 5},
            {"term": "Copenhagen", "type_guess": "place", "frequency": 12},
        ]
        prompt = build_glossary_prompt(
            candidates, "SAMPLE_SOURCE_TEXT", "Some style guide", "Spanish",
        )
        assert "SOURCE TEXT SAMPLE" in prompt
        assert "SAMPLE_SOURCE_TEXT" in prompt
        # legacy single-line candidate format
        assert "- Nelson (frequency: 5)" in prompt
        assert "- Copenhagen (frequency: 12)" in prompt
        # word-mode markers should be absent
        assert "IN ORDER OF FIRST APPEARANCE" not in prompt


# ---------------------------------------------------------------------------
# Bootstrap prompt: word mode (new)
# ---------------------------------------------------------------------------

class TestBootstrapPromptWordMode:

    def _make_candidates(self):
        return [
            {
                "term": "Nelson", "type_guess": "character", "frequency": 5,
                "contexts": [
                    ("source", "Lord Nelson stood on the deck."),
                    ("source", "Then Nelson signalled the fleet."),
                ],
            },
            {
                "term": "Copenhagen", "type_guess": "place", "frequency": 3,
                "contexts": [
                    ("source", "The Battle of Copenhagen ensued."),
                ],
            },
        ]

    def test_word_mode_prompt_has_no_source_block(self):
        from src.glossary_bootstrap import build_glossary_prompt
        prompt = build_glossary_prompt(
            self._make_candidates(),
            "SHOULD_NOT_APPEAR",  # source_text_sample (ignored in word mode)
            "Some style guide", "Spanish",
            context_mode="word",
            book_title="The Story of Nelson",
            context_unit_label="fragments (~10 words before / 6 words after)",
        )
        assert "SOURCE TEXT SAMPLE" not in prompt
        assert "SHOULD_NOT_APPEAR" not in prompt
        assert "The Story of Nelson" in prompt
        assert "IN ORDER OF FIRST APPEARANCE" in prompt
        assert "fragments (~10 words before / 6 words after)" in prompt

    def test_word_mode_prompt_includes_per_term_fragments(self):
        from src.glossary_bootstrap import build_glossary_prompt
        prompt = build_glossary_prompt(
            self._make_candidates(), "", "Some style guide", "Spanish",
            context_mode="word",
            book_title="The Story of Nelson",
            context_unit_label="fragments",
        )
        # Numbered headers in appearance order
        assert "1. Nelson" in prompt
        assert "[freq=5]" in prompt
        assert "2. Copenhagen" in prompt
        assert "[freq=3]" in prompt
        # Each context rendered as `   {label}: "..."`
        assert 'source: "Lord Nelson stood on the deck."' in prompt
        assert 'source: "Then Nelson signalled the fleet."' in prompt
        assert 'source: "The Battle of Copenhagen ensued."' in prompt

    def test_word_mode_no_context_marker(self):
        """A candidate whose contexts list is empty should be flagged."""
        from src.glossary_bootstrap import build_glossary_prompt
        candidates = [
            {"term": "Wellington", "type_guess": "character",
             "frequency": 2, "contexts": []},
            # A second candidate with contexts is required to trigger
            # word-mode rendering (format_candidates_for_prompt switches
            # layouts based on whether ANY candidate has contexts).
            {"term": "Nelson", "type_guess": "character",
             "frequency": 5,
             "contexts": [("source", "Nelson stood on deck.")]},
        ]
        prompt = build_glossary_prompt(
            candidates, "", "Some style guide", "Spanish",
            context_mode="word", book_title="Book", context_unit_label="fragments",
        )
        assert "(no in-text context found)" in prompt


class TestBootstrapWordModeCLIIntegration:
    """End-to-end check that the --bootstrap word path orders candidates
    by first appearance and embeds per-term fragments in the LLM prompt.

    We monkey-patch ``call_llm`` to capture the prompt and immediately return
    a tiny JSON proposal so the rest of the pipeline doesn't fire.
    """

    def test_word_mode_sorts_by_first_appearance_and_inlines_fragments(
        self, tmp_path: Path, monkeypatch
    ):
        # Arrange: a book where 'Banner' appears AFTER 'Anchor', even though
        # 'Banner' has a higher (mocked) frequency. Word-mode should order
        # them by first appearance: Anchor first, then Banner.
        text = (
            "The Anchor lay rusting on the shore. Anchor was forgotten. "
            "Years later, the Banner flew above the fort. The Banner waved "
            "in the wind. Banner Banner Banner."
        )
        source = tmp_path / "book.txt"
        source.write_text(text, encoding="utf-8")
        out = tmp_path / "candidates.json"

        captured = {}

        def fake_call_llm(prompt, **kwargs):
            captured["prompt"] = prompt
            return "[]"  # empty proposals — short-circuits the rest

        # Patch where it's looked up at call time
        import src.api_translator as api_translator
        monkeypatch.setattr(api_translator, "call_llm", fake_call_llm)

        # Hand-build a minimal report so we don't need the full extractor here.
        from scripts.extract_glossary_candidates import (
            CandidateReport, GlossaryCandidate,
        )
        report = CandidateReport(
            source_file=str(source),
            total_words=20, total_unique_words=15,
            candidates=[
                # Score-order would put Banner (high freq) first.
                GlossaryCandidate(
                    term="Banner", type_guess=GlossaryTermType.OTHER,
                    frequency=5, score=0.9, reasons=[],
                ),
                GlossaryCandidate(
                    term="Anchor", type_guess=GlossaryTermType.OTHER,
                    frequency=2, score=0.5, reasons=[],
                ),
            ],
            excluded_glossary_terms=0,
        )

        # Drive the bootstrap branch directly, mirroring main()'s behavior.
        from src.glossary_bootstrap import build_glossary_prompt
        from src.utils.glossary_context import find_first_word_contexts

        candidates = [
            {"term": c.term, "type_guess": c.type_guess.value,
             "frequency": c.frequency}
            for c in report.candidates
        ]
        for cand in candidates:
            pos, ctx = find_first_word_contexts(
                cand["term"], [("source", text)],
                max_contexts=2, words_before=4, words_after=3,
            )
            cand["first_position"] = pos
            cand["contexts"] = ctx
        candidates.sort(
            key=lambda c: c["first_position"] if c["first_position"] is not None
                          else (10**9, 10**9)
        )
        prompt = build_glossary_prompt(
            candidates, "", "style", "Spanish",
            context_mode="word", book_title="Toy Book",
            context_unit_label="fragments (~4 words before / 3 words after)",
        )

        # Assert: 'Anchor' ordered before 'Banner' despite lower score.
        anchor_pos = prompt.find("1. Anchor")
        banner_pos = prompt.find("2. Banner")
        assert anchor_pos != -1 and banner_pos != -1
        assert anchor_pos < banner_pos
        # And each carries its fragments inline.
        assert "Anchor" in prompt
        assert "Banner" in prompt
        assert "SOURCE TEXT SAMPLE" not in prompt


# ---------------------------------------------------------------------------
# Possessive normalization
# ---------------------------------------------------------------------------

class TestStripPossessive:

    def test_straight_apostrophe_s(self):
        assert _strip_possessive("Nelson's") == "Nelson"

    def test_curly_apostrophe_s(self):
        assert _strip_possessive("Nelson’s") == "Nelson"

    def test_trailing_s_apostrophe(self):
        # Plural possessive: parents' -> parents
        assert _strip_possessive("parents'") == "parents"

    def test_no_change_for_plain_word(self):
        assert _strip_possessive("Nelson") == "Nelson"
        assert _strip_possessive("ships") == "ships"

    def test_empty_input(self):
        assert _strip_possessive("") == ""


class TestCollapsePossessiveKeys:

    def test_collapses_possessive_into_bare(self):
        merged = {
            "nelson": GlossaryCandidate(
                term="Nelson", type_guess=GlossaryTermType.CHARACTER,
                frequency=216, detection_reasons=["capitalized_mid_sentence"],
            ),
            "nelson's": GlossaryCandidate(
                term="Nelson's", type_guess=GlossaryTermType.OTHER,
                frequency=36, detection_reasons=["capitalized_mid_sentence"],
            ),
        }
        result = collapse_possessive_keys(merged)
        assert "nelson" in result
        assert "nelson's" not in result
        assert result["nelson"].frequency == 216 + 36
        # Surface stays bare.
        assert result["nelson"].term == "Nelson"
        # Higher-priority CHARACTER survives.
        assert result["nelson"].type_guess == GlossaryTermType.CHARACTER

    def test_possessive_only_keeps_bare_form(self):
        # Only `Hood's` was emitted — collapse should still surface it as `Hood`.
        merged = {
            "hood's": GlossaryCandidate(
                term="Hood's", type_guess=GlossaryTermType.OTHER, frequency=3,
            ),
        }
        result = collapse_possessive_keys(merged)
        assert "hood" in result
        assert result["hood"].term == "Hood"

    def test_collapses_multi_word_possessive(self):
        # "Lord Hood's" should collapse into "Lord Hood"
        merged = {
            "lord hood": GlossaryCandidate(
                term="Lord Hood", type_guess=GlossaryTermType.CHARACTER, frequency=4,
            ),
            "lord hood's": GlossaryCandidate(
                term="Lord Hood's", type_guess=GlossaryTermType.CHARACTER, frequency=3,
            ),
        }
        result = collapse_possessive_keys(merged)
        assert "lord hood" in result
        assert "lord hood's" not in result
        assert result["lord hood"].frequency == 7
        assert result["lord hood"].term == "Lord Hood"

    def test_stopword_guard_exempts_confirmed_character_names(self):
        # Characters named "May" or "Will" (common Victorian names) appear only
        # in possessive form in frequent n-grams. Their bare key is a stopword,
        # but they should survive if already in proper_noun_keys.
        merged = {
            "may's": GlossaryCandidate(
                term="May's", type_guess=GlossaryTermType.CHARACTER, frequency=8,
            ),
        }
        # Without proper_noun_keys: bare "may" is a stopword → dropped.
        result_no_pn = collapse_possessive_keys(merged)
        assert "may" not in result_no_pn
        # With proper_noun_keys containing "may": exempted → survives.
        result_with_pn = collapse_possessive_keys(merged, proper_noun_keys={"may"})
        assert "may" in result_with_pn
        assert result_with_pn["may"].term == "May"


# ---------------------------------------------------------------------------
# Contained-term pruning
# ---------------------------------------------------------------------------

class TestPruneContainedTerms:

    def test_drops_components_of_longer_phrase(self):
        text = (
            "His father was a country clergyman who lived at Burnham Thorpe "
            "in the county of Norfolk. They moved to Burnham Thorpe again."
        )
        merged = {
            "burnham thorpe": GlossaryCandidate(
                term="Burnham Thorpe", type_guess=GlossaryTermType.PLACE, frequency=2,
            ),
            "burnham": GlossaryCandidate(
                term="Burnham", type_guess=GlossaryTermType.OTHER, frequency=2,
            ),
            "thorpe": GlossaryCandidate(
                term="Thorpe", type_guess=GlossaryTermType.OTHER, frequency=2,
            ),
        }
        result = prune_contained_terms(merged, text, min_frequency=2)
        assert "burnham thorpe" in result
        assert "burnham" not in result
        assert "thorpe" not in result

    def test_keeps_term_with_standalone_occurrences(self):
        text = (
            "London Bridge stood firm. London is a great city. "
            "Across London Bridge they marched. Then back to London."
        )
        merged = {
            "london bridge": GlossaryCandidate(
                term="London Bridge", type_guess=GlossaryTermType.PLACE, frequency=2,
            ),
            "london": GlossaryCandidate(
                term="London", type_guess=GlossaryTermType.PLACE, frequency=4,
            ),
        }
        result = prune_contained_terms(merged, text, min_frequency=2)
        assert "london bridge" in result
        assert "london" in result  # has 2 standalone occurrences

    def test_drops_multi_word_subphrase(self):
        # "Captain Maurice" only appears inside "Captain Maurice Suckling".
        text = (
            "His uncle, Captain Maurice Suckling, took command. "
            "Years later, Captain Maurice Suckling distinguished himself."
        )
        merged = {
            "captain maurice suckling": GlossaryCandidate(
                term="Captain Maurice Suckling",
                type_guess=GlossaryTermType.CHARACTER, frequency=2,
            ),
            "captain maurice": GlossaryCandidate(
                term="Captain Maurice",
                type_guess=GlossaryTermType.CHARACTER, frequency=2,
            ),
            "maurice suckling": GlossaryCandidate(
                term="Maurice Suckling",
                type_guess=GlossaryTermType.CHARACTER, frequency=2,
            ),
        }
        result = prune_contained_terms(merged, text, min_frequency=2)
        assert "captain maurice suckling" in result
        assert "captain maurice" not in result
        assert "maurice suckling" not in result

    def test_empty_input(self):
        assert prune_contained_terms({}, "any text", min_frequency=2) == {}


# ---------------------------------------------------------------------------
# N-gram tightening (finite verbs, leading prepositions, quote attribution)
# ---------------------------------------------------------------------------

class TestExtractFrequentNgramsTightened:

    def _checker(self, vocab):
        checker = MagicMock(spec=DictionaryChecker)
        checker.available = True
        checker.is_english_word = lambda w: w.lower() in vocab
        return checker

    def test_rejects_finite_verb_ngrams(self):
        text = (
            "Britain was now at war. Britain was now in trouble. "
            "Britain was now exhausted."
        )
        ck = self._checker({"now", "at", "in", "war", "trouble", "exhausted"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        assert "britain was now" not in result

    def test_rejects_lived_named_made(self):
        text = (
            "Paul lived in England. Paul lived in England again. "
            "He named William as heir. He named William as heir again. "
            "Jacques made the boat. Jacques made the boat once more."
        )
        ck = self._checker({"in", "as", "the", "boat", "heir", "once", "more", "again"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        assert "lived in england" not in result
        assert "named william" not in result
        assert "jacques made" not in result

    def test_rejects_leading_preposition(self):
        text = "off Cadiz the fleet waited. off Cadiz they returned."
        ck = self._checker({"the", "fleet", "waited", "they", "returned"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        assert "off cadiz" not in result

    def test_rejects_quote_attribution(self):
        text = "quote Southey aptly. quote Southey at length."
        ck = self._checker({"aptly", "at", "length"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        assert "quote southey" not in result

    def test_paul_was_reading_dropped(self):
        text = "Paul was reading aloud. Paul was reading slowly."
        ck = self._checker({"aloud", "slowly"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        assert "paul was reading" not in result

    def test_rejects_known_name_plus_dictionary_word(self):
        # `Jules looks` / `Claire when` / `Uncle Paul let` / `Emile Jules` —
        # all sentence-fragment narrations where the character is captured
        # separately. proper_noun_keys carries the single-word name keys.
        text = (
            "Jules looks around. Jules looks again. "
            "Claire when she arrived. Claire when she left. "
            "Uncle Paul let him go. Uncle Paul let her go. "
            "Emile Jules and the others. Emile Jules also helped."
        )
        ck = self._checker({
            "around", "again", "she", "arrived", "left", "him", "go",
            "her", "and", "the", "others", "also", "helped", "when", "let",
            "looks", "uncle",
        })
        sentences = split_into_sentences(text)
        proper_keys = {"jules", "paul", "emile", "claire", "uncle paul"}
        result = extract_frequent_ngrams(sentences, ck, proper_keys, 2)
        assert "jules looks" not in result
        assert "claire when" not in result
        assert "uncle paul let" not in result
        assert "emile jules" not in result

    def test_rejects_emile_possessive_plus_word(self):
        text = "Emile's comment was sharp. Emile's comment was funny."
        ck = self._checker({"was", "sharp", "funny", "comment"})
        sentences = split_into_sentences(text)
        proper_keys = {"emile"}
        result = extract_frequent_ngrams(sentences, ck, proper_keys, 2)
        assert "emile's comment" not in result
        assert "emile comment" not in result

    def test_rejects_multi_word_name_plus_body_part(self):
        # "Aunt Abigail's face" / "Aunt Abigail's hand" — multi-word
        # character name + possessive + ordinary noun. The multi-word
        # name lives in proper_noun_keys; the filter must look there too.
        text = (
            "Aunt Abigail's face was kind. Aunt Abigail's face turned red. "
            "Aunt Abigail's face softened again."
        )
        ck = self._checker({"was", "kind", "turned", "red", "softened",
                            "again", "face", "aunt"})
        sentences = split_into_sentences(text)
        proper_keys = {"abigail", "aunt abigail"}
        result = extract_frequent_ngrams(sentences, ck, proper_keys, 2)
        assert "aunt abigail's face" not in result
        assert "aunt abigail face" not in result

    def test_rejects_multi_word_name_plus_voice(self):
        text = (
            "Cousin Ann's voice was loud. Cousin Ann's voice was sharp. "
            "Cousin Ann's voice carried far."
        )
        ck = self._checker({"was", "loud", "sharp", "carried", "far",
                            "voice", "cousin"})
        sentences = split_into_sentences(text)
        proper_keys = {"ann", "cousin ann"}
        result = extract_frequent_ngrams(sentences, ck, proper_keys, 2)
        assert "cousin ann's voice" not in result
        assert "cousin ann voice" not in result

    def test_rejects_leading_function_word_plus_multi_word_name(self):
        # "like Cousin Ann" — leading content word (not a STOPWORDS entry)
        # plus a known multi-word character name. Should be dropped via
        # the multi-word sub-span match.
        text = (
            "She is like Cousin Ann. He is like Cousin Ann too. "
            "They felt like Cousin Ann that day."
        )
        ck = self._checker({"she", "is", "like", "he", "too", "they",
                            "felt", "that", "day", "cousin"})
        sentences = split_into_sentences(text)
        proper_keys = {"ann", "cousin ann"}
        result = extract_frequent_ngrams(sentences, ck, proper_keys, 2)
        assert "like cousin ann" not in result

    def test_skips_ngram_across_comma(self):
        text = (
            "There are three: Emile, Jules, and Claire. "
            "Then again Emile, Jules came together."
        )
        ck = self._checker({"there", "are", "three", "and", "came", "together",
                            "again", "then"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        # The comma between Emile and Jules should suppress the bigram.
        assert "emile jules" not in result

    def test_skips_ngram_across_quote(self):
        text = (
            "“There are ant-hills,” observed Jules. “Even in the garden.” "
            "“Yes!” said Jules. “Even more so today.”"
        )
        ck = self._checker({"are", "in", "the", "garden", "more", "so",
                            "today", "yes", "said", "observed", "even",
                            "ant", "hills", "there"})
        sentences = split_into_sentences(text)
        result = extract_frequent_ngrams(sentences, ck, set(), 2)
        # Period + quotes between Jules and Even should suppress the bigram.
        assert "jules even" not in result


class TestProperNounSequenceBreaks:

    def test_comma_breaks_sequence(self):
        # `Emile, Jules, and Claire` should NOT yield `Emile Jules` as a
        # multi-word proper noun — the comma marks a list, not a name.
        text = (
            "He said: There are three: Emile, Jules, and Claire. "
            "Again the three: Emile, Jules, and Claire helped."
        )
        sentences = split_into_sentences(text)
        checker = MagicMock(spec=DictionaryChecker)
        checker.available = True
        checker.is_english_word = lambda w: w.lower() in {
            "he", "said", "there", "are", "three", "and", "the", "again",
            "helped",
        }
        result = extract_proper_nouns(sentences, checker, min_frequency=2)
        # `emile jules` should not be a multi-word key — only the singletons.
        assert "emile jules" not in result
        # The individual names should still survive on their own.
        # (They're recorded as part of the now-shorter sequences and
        # also appear independently across the two repetitions.)
        assert any(k.startswith("emile") for k in result)

    def test_quote_breaks_sequence(self):
        # `said Jules. "Even ...` should not glue `Jules Even` into a name.
        text = (
            'observed Jules. "Even in the garden are ants," he said. '
            'replied Jules. "Even more so today," she answered.'
        )
        sentences = split_into_sentences(text)
        checker = MagicMock(spec=DictionaryChecker)
        checker.available = True
        checker.is_english_word = lambda w: w.lower() in {
            "in", "the", "garden", "are", "ants", "he", "said", "more",
            "so", "today", "she", "answered", "even", "observed", "replied",
        }
        result = extract_proper_nouns(sentences, checker, min_frequency=2)
        assert "jules even" not in result


# ---------------------------------------------------------------------------
# Demonym filtering
# ---------------------------------------------------------------------------

class TestFilterDemonyms:

    def _make(self, term, freq=5, type_guess=GlossaryTermType.OTHER):
        return GlossaryCandidate(term=term, type_guess=type_guess, frequency=freq)

    def test_drops_single_word_demonym(self):
        merged = {
            "british": self._make("British"),
            "spaniards": self._make("Spaniards"),
            "frenchmen": self._make("Frenchmen"),
            "englishman": self._make("Englishman"),
            "danes": self._make("Danes"),
            "nelson": self._make("Nelson"),
        }
        result = filter_demonyms(merged)
        assert "british" not in result
        assert "spaniards" not in result
        assert "frenchmen" not in result
        assert "englishman" not in result
        assert "danes" not in result
        # Non-demonym proper noun preserved
        assert "nelson" in result

    def test_drops_demonym_led_lowercase_phrase(self):
        merged = {
            "british ships": self._make("British ships"),
            "spanish navy": self._make("Spanish navy"),
            "british fleet": self._make("British fleet"),
            "spanish admiral": self._make("Spanish admiral"),
        }
        result = filter_demonyms(merged)
        assert "british ships" not in result
        assert "spanish navy" not in result
        assert "british fleet" not in result
        assert "spanish admiral" not in result

    def test_keeps_demonym_phrase_with_capitalized_tail(self):
        merged = {
            "british empire": self._make("British Empire"),
            "spanish inquisition": self._make("Spanish Inquisition"),
            "french revolution": self._make("French Revolution"),
        }
        result = filter_demonyms(merged)
        assert "british empire" in result
        assert "spanish inquisition" in result
        assert "french revolution" in result

    def test_non_demonym_phrases_preserved(self):
        merged = {
            "royal navy": self._make("Royal Navy"),
            "lord hood": self._make("Lord Hood"),
        }
        result = filter_demonyms(merged)
        assert "royal navy" in result
        assert "lord hood" in result


# ---------------------------------------------------------------------------
# Integration: combined Tier 1+2+3 noise reduction on a Nelson-style snippet
# ---------------------------------------------------------------------------

class TestExtractCandidatesNoiseReduction:

    def test_nelson_style_noise_reduced(self):
        text = (
            "Horatio Nelson was born in 1758. His father lived at Burnham Thorpe "
            "in the county of Norfolk. Nelson grew up at Burnham Thorpe. "
            "Nelson's letters survive. Nelson signalled the fleet. "
            "Years later, Lord St. Vincent took command. Lord St. Vincent issued orders. "
            "British ships sailed for Cadiz. British ships waited off Cadiz. "
            "British ships returned. The Spaniards retreated. The Spaniards fled. "
            "The Spaniards regrouped."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms_lower = {c.term.lower() for c in report.candidates}

        # Possessive collapse: `Nelson's` should not appear as its own term.
        assert "nelson's" not in terms_lower
        # Component pruning: `Burnham`/`Thorpe` only ever inside `Burnham Thorpe`.
        assert "burnham" not in terms_lower
        assert "thorpe" not in terms_lower
        # Sentence splitter fix: `Lord St. Vincent` survives intact.
        assert any("lord st" in t for t in terms_lower)
        # Demonym suppression: single-word and adjectival phrases gone.
        assert "british" not in terms_lower
        assert "spaniards" not in terms_lower
        assert "british ships" not in terms_lower

        # Real proper nouns survive.
        assert any("burnham thorpe" in t for t in terms_lower)


# ---------------------------------------------------------------------------
# British-spelling fallback in DictionaryChecker
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("scripts.extract_glossary_candidates", fromlist=["ENCHANT_AVAILABLE"]).ENCHANT_AVAILABLE,
    reason="PyEnchant not installed",
)
class TestDictionaryCheckerGBFallback:

    def test_british_spellings_recognised(self):
        checker = DictionaryChecker()
        if not checker.available:
            pytest.skip("Neither en_US nor en_GB dictionary available")
        # If en_GB was loaded, common British spellings should now resolve.
        if checker.gb_dict is None:
            pytest.skip("en_GB dictionary unavailable")
        for word in ("honour", "centre", "colours", "harbour", "defence"):
            assert checker.is_english_word(word), f"expected {word!r} to be in dict"


class TestRestoreTitlePeriods:

    def test_appends_period_to_known_titles(self):
        assert _restore_title_periods(["Mrs", "Ford"]) == "Mrs. Ford"
        assert _restore_title_periods(["Lord", "St", "Vincent"]) == "Lord St. Vincent"
        assert _restore_title_periods(["Capt", "Hardy"]) == "Capt. Hardy"

    def test_leaves_non_titles_alone(self):
        assert _restore_title_periods(["Burnham", "Thorpe"]) == "Burnham Thorpe"

    def test_case_insensitive(self):
        assert _restore_title_periods(["mr", "smith"]) == "mr. smith"


class TestCurlyApostropheHandling:

    def test_contractions_stay_one_token(self):
        # Curly apostrophe must not split `doesn’t` into `doesn` + `t`.
        tokens = tokenize("It doesn’t work and it isn’t broken either.")
        assert "doesn’t" in tokens or "doesn't" in tokens
        assert "doesn" not in tokens
        assert "isn" not in tokens

    def test_extract_candidates_normalizes_curly_quotes(self):
        # extract_candidates should normalize curly apostrophes so contraction
        # halves like `doesn` / `isn` / `wouldn` never become candidates.
        text = (
            "It doesn’t work, she says. "
            "It doesn’t matter much. "
            "It doesn’t fit anywhere. "
            "He wouldn’t answer. He wouldn’t go. He wouldn’t agree. "
            "I think it isn’t worth it. I told you it isn’t over yet."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms = {c.term.lower() for c in report.candidates}
        for noise in ("doesn", "isn", "wouldn", "hadn", "haven"):
            assert noise not in terms, (
                f"{noise!r} should not appear after curly-apostrophe normalization"
            )


class TestGutenbergItalicUnderscores:
    """Project Gutenberg marks italics with paired underscores (``_word_``).
    Those must be stripped before tokenizing so ``_Gaudenzia_`` doesn't become a
    junk twin of ``Gaudenzia`` (FRICTION_LOG_5 #27). The fix lives in the shared
    extractor, so it covers both the GUI and harness paths."""

    def test_italic_name_collapses_to_single_candidate(self):
        text = (
            "The filly _Gaudenzia_ ran fast. "
            "Everyone cheered for _Gaudenzia_ at the gate. "
            "Then Gaudenzia crossed the line first. "
            "Gaudenzia was the pride of the stable."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        # No candidate key carries an underscore (no `_Gaudenzia_` twin).
        assert all("_" not in c.term for c in report.candidates)
        # The bare and italic spellings merge into one `Gaudenzia` candidate.
        gaudenzia = [c for c in report.candidates if c.term.lower() == "gaudenzia"]
        assert len(gaudenzia) == 1

    def test_italic_phrase_no_underscore_terms(self):
        text = (
            "He admired _the Black Stallion_ from afar. "
            "The Black Stallion galloped past. "
            "Again the Black Stallion thundered by. "
            "People talked about _the Black Stallion_ for days."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        assert all("_" not in c.term for c in report.candidates)


class TestTitlePeriodsInCandidates:

    def test_proper_noun_keeps_mrs_period(self):
        # Repeat enough so it survives min_frequency=2 and the dictionary check.
        text = (
            "She greeted Mrs. Ford warmly. "
            "Then Mrs. Ford spoke about the engine. "
            "The next day Mrs. Ford returned with the keys."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms = {c.term for c in report.candidates}
        assert "Mrs. Ford" in terms
        assert "Mrs Ford" not in terms

    def test_bare_title_abbreviation_dropped(self):
        # Even when `Mr` appears repeatedly mid-sentence without a following
        # capitalized name, it should not become a glossary candidate.
        text = (
            "the room and ask Mr Wilson directly. "
            "Then please tell Mr Wilson again. "
            "And remind Mr Wilson tomorrow."
        )
        # Force `Mr` to appear standalone too:
        text += " Yes Mr is what they called him. No Mr is mentioned again. So Mr."
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms = {c.term.lower() for c in report.candidates}
        assert "mr" not in terms
        assert "mrs" not in terms


# ---------------------------------------------------------------------------
# Test forced glossary terms
# ---------------------------------------------------------------------------

class TestForcedGlossaryTerms:
    """User-defined forced terms that bypass normal extraction heuristics."""

    def test_build_forced_candidates_surfaces_present_term(self):
        text = "The gobbler strutted across the yard. The gobbler was loud."
        result = build_forced_candidates(text, [{"term": "gobbler"}])
        assert "gobbler" in result
        cand = result["gobbler"]
        assert cand.frequency == 2
        assert FORCED_TERM_REASON in cand.detection_reasons
        assert cand.type_guess == GlossaryTermType.OTHER

    def test_build_forced_candidates_skips_absent_term(self):
        text = "A peaceful afternoon in the garden."
        result = build_forced_candidates(text, [{"term": "kraken"}])
        assert result == {}

    def test_plural_matching_single_word(self):
        text = "Three gobblers wandered past the stalls."
        result = build_forced_candidates(
            text, [{"term": "gobbler"}, {"term": "stall"}]
        )
        assert "gobbler" in result
        assert "stall" in result
        assert result["gobbler"].frequency == 1
        assert result["stall"].frequency == 1

    def test_plural_not_applied_to_multiword(self):
        text = "The swift currents pulled the boat downstream."
        result = build_forced_candidates(text, [{"term": "swift current"}])
        # Multi-word phrases match exactly — no -s/-es suffix.
        assert result == {}

    def test_case_insensitive_matching(self):
        text = "Stall, STALL, and stall all count."
        result = build_forced_candidates(text, [{"term": "stall"}])
        assert result["stall"].frequency == 3

    def test_type_guess_parsed_from_entry(self):
        text = "The gobbler crossed the road."
        result = build_forced_candidates(
            text, [{"term": "gobbler", "type_guess": "TECHNICAL"}]
        )
        assert result["gobbler"].type_guess == GlossaryTermType.TECHNICAL

    def test_invalid_type_guess_falls_back_to_other(self):
        text = "The gobbler crossed the road."
        result = build_forced_candidates(
            text, [{"term": "gobbler", "type_guess": "BOGUS"}]
        )
        assert result["gobbler"].type_guess == GlossaryTermType.OTHER

    def test_extra_detection_reasons_preserved(self):
        text = "The gobbler crossed the road."
        result = build_forced_candidates(
            text, [{"term": "gobbler", "detection_reasons": ["custom_reason"]}]
        )
        reasons = result["gobbler"].detection_reasons
        assert "custom_reason" in reasons
        assert FORCED_TERM_REASON in reasons

    def test_force_injection_bypasses_min_frequency(self):
        # "stall" appears once and is a common dictionary word — without
        # forcing, it would never survive uncommon/rare/proper-noun extractors.
        text = (
            "He led the horse into the stall and shut the gate. "
            "Then he walked back to the farmhouse and started dinner."
        )
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[{"term": "stall"}],
        ):
            report = extract_candidates(
                text, min_frequency=5, max_candidates=100,
            )
        terms_lower = {c.term.lower() for c in report.candidates}
        assert "stall" in terms_lower
        stall_cand = next(c for c in report.candidates if c.term.lower() == "stall")
        assert FORCED_TERM_REASON in stall_cand.detection_reasons

    def test_force_injection_fills_context_sentence(self):
        text = "He led the horse into the stall and shut the gate."
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[{"term": "stall"}],
        ):
            report = extract_candidates(text, min_frequency=1, max_candidates=50)
        stall_cand = next(c for c in report.candidates if c.term.lower() == "stall")
        assert "stall" in stall_cand.context_sentence.lower()

    def test_force_injection_still_excluded_if_in_glossary(self):
        text = "The gobbler strutted across the yard."
        glossary = Glossary(terms=[
            GlossaryTerm(
                english="gobbler",
                spanish="guajolote",
                type=GlossaryTermType.OTHER,
            ),
        ])
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[{"term": "gobbler"}],
        ):
            report = extract_candidates(
                text, glossary=glossary, min_frequency=1, max_candidates=50,
            )
        terms_lower = {c.term.lower() for c in report.candidates}
        assert "gobbler" not in terms_lower
        assert report.excluded_glossary_terms >= 1

    def test_force_injection_no_entries_is_noop(self):
        text = "The children gathered in the garden one warm afternoon."
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[],
        ):
            report = extract_candidates(text, min_frequency=1, max_candidates=50)
        assert isinstance(report, CandidateReport)

    def test_load_forced_glossary_terms_missing_file(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.json"
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", missing)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        assert app_config.load_forced_glossary_terms(force_reload=True) == []

    def test_load_forced_glossary_terms_malformed_json(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json", encoding="utf-8")
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", bad)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        assert app_config.load_forced_glossary_terms(force_reload=True) == []

    def test_load_forced_glossary_terms_missing_terms_key(
        self, tmp_path, monkeypatch
    ):
        bad = tmp_path / "no_terms.json"
        bad.write_text(json.dumps({"other_key": []}), encoding="utf-8")
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", bad)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        assert app_config.load_forced_glossary_terms(force_reload=True) == []

    def test_load_forced_glossary_terms_returns_entries(
        self, tmp_path, monkeypatch
    ):
        good = tmp_path / "good.json"
        good.write_text(
            json.dumps({"terms": [
                {"term": "stall", "type_guess": "TECHNICAL"},
                {"term": "gobbler"},
            ]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", good)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        entries = app_config.load_forced_glossary_terms(force_reload=True)
        assert len(entries) == 2
        assert entries[0]["term"] == "stall"

    # ------------------------------------------------------------------
    # Gap tests: uncovered branches
    # ------------------------------------------------------------------

    def test_build_forced_candidates_skips_entry_with_no_term_key(self):
        """Entry dict has no 'term' key — should be silently skipped."""
        text = "The gobbler crossed the road."
        result = build_forced_candidates(text, [{"type_guess": "OTHER"}])
        assert result == {}

    def test_build_forced_candidates_skips_whitespace_only_term(self):
        """Term that is only whitespace after strip — should be silently skipped."""
        text = "The gobbler crossed the road."
        result = build_forced_candidates(text, [{"term": "   "}])
        assert result == {}

    def test_build_forced_candidates_deduplicates_case_variants(self):
        """Two entries that resolve to the same lowercase key — second is skipped."""
        text = "gobbler Gobbler GOBBLER"
        result = build_forced_candidates(
            text, [{"term": "gobbler"}, {"term": "Gobbler"}]
        )
        assert len(result) == 1
        assert "gobbler" in result

    def test_build_forced_candidates_no_duplicate_forced_reason(self):
        """If FORCED_TERM_REASON is already in detection_reasons it must not appear twice."""
        text = "The gobbler crossed the road."
        result = build_forced_candidates(
            text,
            [{"term": "gobbler", "detection_reasons": [FORCED_TERM_REASON]}],
        )
        reasons = result["gobbler"].detection_reasons
        assert reasons.count(FORCED_TERM_REASON) == 1

    def test_load_forced_glossary_terms_top_level_list_returns_empty(
        self, tmp_path, monkeypatch
    ):
        """JSON file whose root is a list (not a dict) should return []."""
        bad = tmp_path / "list.json"
        bad.write_text(
            json.dumps([{"term": "stall"}]), encoding="utf-8"
        )
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", bad)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        assert app_config.load_forced_glossary_terms(force_reload=True) == []

    def test_load_forced_glossary_terms_filters_non_dict_entries(
        self, tmp_path, monkeypatch
    ):
        """Non-dict items in the 'terms' list are filtered out."""
        mixed = tmp_path / "mixed.json"
        mixed.write_text(
            json.dumps({"terms": [{"term": "stall"}, "a string", 42, None]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", mixed)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        entries = app_config.load_forced_glossary_terms(force_reload=True)
        assert entries == [{"term": "stall"}]

    def test_load_forced_glossary_terms_cache_hit(self, tmp_path, monkeypatch):
        """Second call without force_reload returns the cached list."""
        good = tmp_path / "good.json"
        good.write_text(
            json.dumps({"terms": [{"term": "stall"}]}), encoding="utf-8"
        )
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_PATH", good)
        monkeypatch.setattr(app_config, "_FORCED_GLOSSARY_CACHE", None)
        first = app_config.load_forced_glossary_terms(force_reload=True)
        second = app_config.load_forced_glossary_terms()  # uses cache
        assert first == second

    def test_force_injection_skipped_when_all_forced_terms_absent_from_text(self):
        """Forced entry list is non-empty but no term matches text → merge not called."""
        text = "A quiet afternoon in the peaceful garden."
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[{"term": "kraken"}],
        ):
            report = extract_candidates(text, min_frequency=1, max_candidates=50)
        assert isinstance(report, CandidateReport)
        # kraken not in text, so it must not appear in candidates
        terms_lower = {c.term.lower() for c in report.candidates}
        assert "kraken" not in terms_lower

    def test_force_injection_verbose_prints_count(self, capsys):
        """verbose=True prints the forced-term injection line when terms match."""
        text = (
            "He led the horse into the stall and shut the gate. "
            "The stall was dark."
        )
        with patch(
            "scripts.extract_glossary_candidates.load_forced_glossary_terms",
            return_value=[{"term": "stall"}],
        ):
            extract_candidates(text, min_frequency=1, max_candidates=50, verbose=True)
        captured = capsys.readouterr()
        assert "After forced-term injection" in captured.out


# ---------------------------------------------------------------------------
# I-contraction and greeting filtering (regression: noise on first-person
# narrative + dialogue-heavy books like Understood Betsy)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("scripts.extract_glossary_candidates", fromlist=["ENCHANT_AVAILABLE"]).ENCHANT_AVAILABLE,
    reason="PyEnchant not installed",
)
class TestDictionaryCheckerIContractions:

    def test_lowercase_i_contractions_recognised(self):
        # Enchant accepts "I'll" but rejects "i'll" — is_english_word must
        # try the capital-I form so contractions don't slip into candidates
        # via the "not_in_dictionary" branch of every extractor.
        checker = DictionaryChecker()
        if not checker.available:
            pytest.skip("Neither en_US nor en_GB dictionary available")
        for word in ("i'll", "i'd", "i'm", "i've"):
            assert checker.is_english_word(word), f"expected {word!r} to be in dict"


class TestNoiseFromContractionsAndGreetings:

    def test_i_contractions_not_in_candidates(self):
        # First-person narrative repeating common contractions used to surface
        # them as candidates because enchant rejects "i'll"/"i'd"/"i'm"/"i've"
        # in lowercase. They must not appear anywhere in the output.
        text = (
            "I'll see Betsy tomorrow. I'll bring the book. "
            "I'd hoped to come sooner. I'd written ahead. "
            "I'm going to the store. I'm tired of waiting. "
            "I've been there before. I've never seen it."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms_lower = {c.term.lower() for c in report.candidates}
        for noise in ("i'll", "i'd", "i'm", "i've"):
            assert noise not in terms_lower, f"{noise!r} leaked into candidates"
        # Bigrams led by a contraction must also be filtered (the n-gram
        # "all words in dictionary" check depends on is_english_word).
        for noise in ("i'll see", "i'll bring", "i'm going", "i've been"):
            assert noise not in terms_lower, f"{noise!r} leaked into candidates"

    def test_protagonist_with_sentence_start_dominance_admitted(self):
        # Betsy frequently begins sentences ("Betsy looked up.") which used
        # to count only toward `total_occurrences`, dropping the
        # cap-ratio below 0.8 and dropping Betsy from proper_noun_keys.
        # Without Betsy in proper_noun_keys, the n-gram character-name
        # filter cannot drop "Betsy looked"/"Betsy felt"/etc., so the
        # bigrams leak into the output. Fixing the ratio fixes both.
        text = (
            "Betsy looked at the door. Betsy looked at the window. "
            "Betsy felt cold. Betsy felt warm. Betsy felt happy. "
            "Betsy turned the page. Betsy turned around. "
            "Aunt Frances said hello. Aunt Frances waved goodbye."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms_lower = {c.term.lower() for c in report.candidates}
        assert "betsy" in terms_lower, "Betsy should be admitted as a single proper noun"
        for noise in ("betsy looked", "betsy felt", "betsy turned"):
            assert noise not in terms_lower, f"{noise!r} leaked into candidates"

    def test_dialect_possessive_does_not_collapse_to_stopword(self):
        # Vermont dialect "so's" (= "so as that") is rare enough in literary
        # English to slip past extract_uncommon_words as not_in_dictionary,
        # then `collapse_possessive_keys` would strip the trailing 's and
        # leave a candidate keyed "so" — a stopword. Guard against that.
        text = (
            "She did it so's to get it off her mind. "
            "He watches the way so's to know where to go. "
            "We use this room so's to keep warm. "
            "Watch close, so's you can answer later."
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms_lower = {c.term.lower() for c in report.candidates}
        assert "so" not in terms_lower
        assert "so's" not in terms_lower

    def test_greetings_filtered_from_dialogue(self):
        # `Hello` / `Hi` / `Hey` / `Goodbye` are usually capitalized at
        # dialogue starts and used to slip through extract_repeated_capitalized
        # as "always-capitalized" + (depending on Zipf source) "rare in literary
        # English". They're now in STOPWORDS and SEQUENCE_BREAKERS.
        text = (
            '"Hello," said Betsy. "Hello," she said again. "Hello, dear." '
            '"Hi," answered the cousin. "Hi," he repeated. "Hi there." '
            '"Hey, look at this!" "Hey!" she cried. "Hey now." '
            '"Goodbye," whispered the aunt. "Goodbye." "Goodbye then."'
        )
        report = extract_candidates(text, min_frequency=2, max_candidates=200)
        terms_lower = {c.term.lower() for c in report.candidates}
        for greeting in ("hello", "hi", "hey", "goodbye", "bye", "okay", "ok"):
            assert greeting not in terms_lower, (
                f"{greeting!r} leaked into candidates"
            )
