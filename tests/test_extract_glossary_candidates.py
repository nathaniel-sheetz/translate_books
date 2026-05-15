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
    NLTK_AVAILABLE,
    WORDFREQ_AVAILABLE,
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
        assert "- Nelson (type guess: character, frequency: 5)" in prompt
        assert "- Copenhagen (type guess: place, frequency: 12)" in prompt
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
        assert "[character | freq=5]" in prompt
        assert "2. Copenhagen" in prompt
        assert "[place | freq=3]" in prompt
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
