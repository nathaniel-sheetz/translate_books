"""Gap-filling tests for the glossary-improvements branch.

Covers paths not exercised by the three existing test modules:
  - _ends_with_abbreviation / _rejoin_abbreviation_splits (private helpers)
  - tokenize_with_spans / _has_hard_break (span tokenization)
  - FrequencyChecker: corrupt cache, wordfreq-only path, last-resort fallback
  - extract_uncommon_words: freq_checker fallback when dict unavailable
  - score_and_rank: rare_in_literary_english bonus
  - DictionaryChecker: GB-only available, is_english_word GB fallback
  - load_chapter_source_text: corrupt JSON chunk falls back to chapters/
  - collapse_possessive_keys: reason merging, curly-apostrophe possessive key
  - extract_candidates: max_zipf_capitalized / max_zipf_mixed parameters
  - format_candidates_for_prompt: empty list, legacy path (no contexts key)
  - find_first_word_contexts: punct-only text; find_first_contexts max_contexts
  - prune_contained_terms: empty-string term is skipped gracefully
"""

from __future__ import annotations

import json
import pickle
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.extract_glossary_candidates import (
    DictionaryChecker,
    FrequencyChecker,
    GlossaryCandidate,
    _ends_with_abbreviation,
    _has_hard_break,
    _rejoin_abbreviation_splits,
    collapse_possessive_keys,
    extract_candidates,
    extract_uncommon_words,
    prune_contained_terms,
    score_and_rank,
    split_into_sentences,
    tokenize_with_spans,
    NLTK_AVAILABLE,
    WORDFREQ_AVAILABLE,
)
from src.models import GlossaryTermType
from src.utils.glossary_context import find_first_contexts, find_first_word_contexts
from src.utils.source_text import load_chapter_source_text


# ---------------------------------------------------------------------------
# Helpers used in multiple tests
# ---------------------------------------------------------------------------

def _dict_checker(vocab: set[str]) -> DictionaryChecker:
    checker = MagicMock(spec=DictionaryChecker)
    checker.available = True
    checker.is_english_word = lambda w: w.lower() in vocab
    return checker


def _freq_checker(zipf_table: dict[str, float], default: float = 3.0) -> FrequencyChecker:
    checker = MagicMock(spec=FrequencyChecker)
    checker.available = True
    checker.literary_zipf = lambda w: zipf_table.get(w.lower(), default)
    return checker


# ---------------------------------------------------------------------------
# _ends_with_abbreviation
# ---------------------------------------------------------------------------

class TestEndsWithAbbreviation:

    def test_known_title_abbreviation(self):
        assert _ends_with_abbreviation("Lord St.") is True
        assert _ends_with_abbreviation("afterwards Mr.") is True
        assert _ends_with_abbreviation("spoke Mrs.") is True
        assert _ends_with_abbreviation("Capt.") is True

    def test_non_abbreviation_word(self):
        assert _ends_with_abbreviation("Nelson") is False
        assert _ends_with_abbreviation("today") is False

    def test_empty_string(self):
        assert _ends_with_abbreviation("") is False

    def test_sentence_ending_with_regular_period(self):
        # "Battle." ends with "Battle" — not in _ABBREV_NO_SPLIT
        assert _ends_with_abbreviation("The Battle.") is False


# ---------------------------------------------------------------------------
# _rejoin_abbreviation_splits
# ---------------------------------------------------------------------------

class TestRejoinAbbreviationSplits:

    def test_empty_list_returns_empty(self):
        assert _rejoin_abbreviation_splits([]) == []

    def test_single_element_unchanged(self):
        assert _rejoin_abbreviation_splits(["just one"]) == ["just one"]

    def test_joins_on_abbreviation(self):
        parts = ["afterwards Lord St.", "Vincent took command"]
        result = _rejoin_abbreviation_splits(parts)
        assert result == ["afterwards Lord St. Vincent took command"]

    def test_two_abbreviations_joined_into_one(self):
        # "Lord St. Vincent met Dr. Martin today" split at both dots
        parts = ["Lord St.", "Vincent met Dr.", "Martin today"]
        result = _rejoin_abbreviation_splits(parts)
        assert result == ["Lord St. Vincent met Dr. Martin today"]

    def test_non_abbreviation_not_joined(self):
        parts = ["Hello world.", "New sentence begins."]
        result = _rejoin_abbreviation_splits(parts)
        assert result == ["Hello world.", "New sentence begins."]


# ---------------------------------------------------------------------------
# tokenize_with_spans
# ---------------------------------------------------------------------------

class TestTokenizeWithSpans:

    def test_basic_offsets(self):
        spans = tokenize_with_spans("Hello world")
        assert spans == [("Hello", 0, 5), ("world", 6, 11)]

    def test_curly_apostrophe_stays_single_token(self):
        spans = tokenize_with_spans("It doesn’t work")
        tokens = [t for t, _, _ in spans]
        assert "doesn’t" in tokens
        assert "doesn" not in tokens

    def test_span_offsets_are_consistent_with_text(self):
        text = "Lord Nelson rode."
        for tok, start, end in tokenize_with_spans(text):
            assert text[start:end] == tok

    def test_empty_text(self):
        assert tokenize_with_spans("") == []


# ---------------------------------------------------------------------------
# _has_hard_break
# ---------------------------------------------------------------------------

class TestHasHardBreak:

    def test_comma_is_hard_break(self):
        assert _has_hard_break("Emile, Jules", 5, 7) is True

    def test_semicolon_is_hard_break(self):
        assert _has_hard_break("Emile; Jules", 5, 7) is True

    def test_colon_is_hard_break(self):
        assert _has_hard_break("Smith: Jones", 5, 7) is True

    def test_double_quote_is_hard_break(self):
        # Straight double quote
        assert _has_hard_break('Jules. "Even', 6, 8) is True

    def test_space_only_is_not_hard_break(self):
        assert _has_hard_break("Hello World", 5, 6) is False

    def test_adjacent_no_break(self):
        # Start == end (zero-width slice)
        assert _has_hard_break("AB", 1, 1) is False


# ---------------------------------------------------------------------------
# FrequencyChecker: fallback and edge-case paths
# ---------------------------------------------------------------------------

class TestFrequencyCheckerFallbacks:

    def test_wordfreq_fallback_when_fd_is_none(self):
        """When the corpus FreqDist is unavailable, wordfreq is consulted."""
        if not WORDFREQ_AVAILABLE:
            pytest.skip("wordfreq not installed")
        fc = FrequencyChecker.__new__(FrequencyChecker)
        fc._fd = None
        fc._total = 0
        fc.available = True
        # wordfreq should return a real float for a known word
        score = fc.literary_zipf("binnacle")
        assert isinstance(score, float)
        assert 0.0 < score < 8.0

    def test_last_resort_fallback_returns_2(self):
        """When no backend is available, literary_zipf returns 2.0."""
        fc = FrequencyChecker.__new__(FrequencyChecker)
        fc._fd = None
        fc._total = 0
        fc.available = True
        with patch("scripts.extract_glossary_candidates.WORDFREQ_AVAILABLE", False):
            score = fc.literary_zipf("anything_rare")
        assert score == 2.0

    def test_corpus_count_below_threshold_falls_to_wordfreq(self):
        """A word with count < 3 in the corpus triggers the wordfreq fallback."""
        if not WORDFREQ_AVAILABLE:
            pytest.skip("wordfreq not installed")
        from nltk import FreqDist
        fd = FreqDist({"common": 1000, "rare_word_xyz": 1})
        fc = FrequencyChecker.__new__(FrequencyChecker)
        fc._fd = fd
        fc._total = sum(fd.values())
        fc.available = True
        # "rare_word_xyz" has count=1 < 3, so should fall through to wordfreq
        score = fc.literary_zipf("rare_word_xyz")
        assert isinstance(score, float)

    def test_corpus_count_above_threshold_uses_fd(self):
        """A word with count >= 3 in the corpus uses the FreqDist directly."""
        from nltk import FreqDist
        fd = FreqDist({"common": 10000})
        fc = FrequencyChecker.__new__(FrequencyChecker)
        fc._fd = fd
        fc._total = sum(fd.values())
        fc.available = True
        import math
        expected = math.log10((10000 / 10000) * 1e9)
        assert abs(fc.literary_zipf("common") - expected) < 0.001

    def test_corrupt_cache_falls_through_to_rebuild(self):
        """A corrupt .pkl file should be silently ignored and corpus rebuilt."""
        import scripts.extract_glossary_candidates as mod
        orig_path = mod.CACHE_PATH
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.pkl"
            bad.write_bytes(b"not valid pickle")
            mod.CACHE_PATH = bad
            try:
                fc = FrequencyChecker()
                # Should still be available (rebuilt from corpus or wordfreq)
                assert fc.available
            finally:
                mod.CACHE_PATH = orig_path

    def test_cache_version_mismatch_triggers_rebuild(self):
        """A cache file with wrong version should be ignored and rebuilt."""
        import scripts.extract_glossary_candidates as mod
        orig_path = mod.CACHE_PATH
        with tempfile.TemporaryDirectory() as td:
            stale = Path(td) / "stale.pkl"
            stale.write_bytes(pickle.dumps({"version": 0, "fd": None, "total": 0}))
            mod.CACHE_PATH = stale
            try:
                fc = FrequencyChecker()
                assert fc.available
            finally:
                mod.CACHE_PATH = orig_path


# ---------------------------------------------------------------------------
# DictionaryChecker: GB-only available path
# ---------------------------------------------------------------------------

class TestDictionaryCheckerGBOnlyPath:

    def test_available_true_when_only_gb_loaded(self):
        """available should be True if en_GB is loaded even without en_US."""
        from scripts.extract_glossary_candidates import ENCHANT_AVAILABLE
        if not ENCHANT_AVAILABLE:
            pytest.skip("PyEnchant not installed")
        real = DictionaryChecker()
        if real.gb_dict is None:
            pytest.skip("en_GB dict not installed")
        dc = DictionaryChecker.__new__(DictionaryChecker)
        dc.english_dict = None
        dc.gb_dict = real.gb_dict
        assert dc.available is True

    def test_is_english_word_via_gb_when_us_none(self):
        """is_english_word should use GB dict when US dict is None."""
        from scripts.extract_glossary_candidates import ENCHANT_AVAILABLE
        if not ENCHANT_AVAILABLE:
            pytest.skip("PyEnchant not installed")
        real = DictionaryChecker()
        if real.gb_dict is None:
            pytest.skip("en_GB dict not installed")
        dc = DictionaryChecker.__new__(DictionaryChecker)
        dc.english_dict = None
        dc.gb_dict = real.gb_dict
        assert dc.is_english_word("the") is True

    def test_is_english_word_returns_false_when_both_none(self):
        dc = DictionaryChecker.__new__(DictionaryChecker)
        dc.english_dict = None
        dc.gb_dict = None
        assert dc.is_english_word("hello") is False

    def test_exception_in_check_returns_false(self):
        """An exception thrown by enchant.Dict.check should be swallowed."""
        mock_dict = MagicMock()
        mock_dict.check.side_effect = Exception("oops")
        dc = DictionaryChecker.__new__(DictionaryChecker)
        dc.english_dict = mock_dict
        dc.gb_dict = None
        assert dc.is_english_word("hello") is False


# ---------------------------------------------------------------------------
# extract_uncommon_words: freq_checker fallback path (no dict)
# ---------------------------------------------------------------------------

class TestExtractUncommonWordsFreqFallback:

    def test_no_dict_uses_freq_checker(self):
        """When dict is unavailable, words below the Zipf threshold surface."""
        no_dict = _dict_checker(set())
        no_dict.available = False
        freq = _freq_checker({"binnacle": 1.5, "honour": 4.5}, default=3.0)
        text = (
            "The binnacle held the compass. The binnacle was old. "
            "The binnacle broke. The honour was honour."
        )
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(
            text, no_dict, set(), 2, sentences,
            freq_checker=freq, max_zipf_mixed=3.0,
        )
        # binnacle (Zipf 1.5 < 3.0) → surfaces
        assert "binnacle" in result
        # honour (Zipf 4.5 >= 3.0) → filtered
        assert "honour" not in result

    def test_both_unavailable_returns_empty(self):
        no_dict = _dict_checker(set())
        no_dict.available = False
        no_freq = MagicMock(spec=FrequencyChecker)
        no_freq.available = False
        text = "The cerambyx was there. The cerambyx returned."
        sentences = split_into_sentences(text)
        result = extract_uncommon_words(
            text, no_dict, set(), 1, sentences, freq_checker=no_freq
        )
        assert result == {}


# ---------------------------------------------------------------------------
# score_and_rank: rare_in_literary_english bonus
# ---------------------------------------------------------------------------

class TestScoreAndRankRareBonus:

    def test_rare_bonus_lifts_score_above_plain_candidate(self):
        """A candidate with 'rare_in_literary_english' reason should outscore
        an otherwise identical candidate without it."""
        checker = _dict_checker({"palmer", "something"})
        candidates = {
            "palmer": GlossaryCandidate(
                term="palmer",
                type_guess=GlossaryTermType.TECHNICAL,
                frequency=5,
                detection_reasons=["rare_in_literary_english"],
            ),
            "something": GlossaryCandidate(
                term="something",
                type_guess=GlossaryTermType.TECHNICAL,
                frequency=5,
                detection_reasons=["frequent_ngram"],
            ),
        }
        ranked = score_and_rank(candidates, checker, 10, [])
        assert len(ranked) == 2
        assert ranked[0].term == "palmer"
        assert ranked[0].score > ranked[1].score

    def test_rare_bonus_zero_without_reason(self):
        checker = _dict_checker(set())
        candidates = {
            "zorblax": GlossaryCandidate(
                term="zorblax",
                type_guess=GlossaryTermType.OTHER,
                frequency=3,
                detection_reasons=["not_in_dictionary"],
            ),
        }
        ranked = score_and_rank(candidates, checker, 10, [])
        assert len(ranked) == 1
        # rare_bonus is 0: score = 0.35*norm_freq + 0.25*1 + 0.20*0 + 0.10*norm_reasons
        # Just verify it's a sensible float and no error
        assert 0.0 < ranked[0].score <= 1.0


# ---------------------------------------------------------------------------
# load_chapter_source_text: corrupt JSON chunk falls back to chapters/
# ---------------------------------------------------------------------------

class TestLoadChapterSourceTextCorruptChunk:

    def test_corrupt_json_chunk_falls_back_to_chapter(self, tmp_path: Path):
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        (chunks / "chapter_01_chunk_000.json").write_text(
            "not valid json", encoding="utf-8"
        )
        chapters = tmp_path / "chapters"
        chapters.mkdir()
        (chapters / "chapter_01.txt").write_text(
            "Fallback chapter text.", encoding="utf-8"
        )
        text, mtime, kind = load_chapter_source_text(tmp_path, "chapter_01")
        assert kind == "chapters"
        assert text == "Fallback chapter text."

    def test_oserror_on_chunk_falls_back_to_chapter(self, tmp_path: Path, monkeypatch):
        """An OSError reading a chunk should log a warning and fall through."""
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        chunk_file = chunks / "chapter_01_chunk_000.json"
        chunk_file.write_text(
            json.dumps({"id": "chapter_01_chunk_000", "source_text": "English text."}),
            encoding="utf-8",
        )
        chapters = tmp_path / "chapters"
        chapters.mkdir()
        (chapters / "chapter_01.txt").write_text("Chapter fallback.", encoding="utf-8")

        original_open = open

        def patched_open(path, *args, **kwargs):
            if "chunk" in str(path):
                raise OSError("disk error")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", patched_open)
        text, _, kind = load_chapter_source_text(tmp_path, "chapter_01")
        assert kind == "chapters"
        assert text == "Chapter fallback."

    def test_no_chunks_dir_uses_chapters(self, tmp_path: Path):
        chapters = tmp_path / "chapters"
        chapters.mkdir()
        (chapters / "chapter_02.txt").write_text("Chapter 2 text.", encoding="utf-8")
        text, _, kind = load_chapter_source_text(tmp_path, "chapter_02")
        assert kind == "chapters"
        assert text == "Chapter 2 text."

    def test_oserror_on_chapter_returns_empty(self, tmp_path: Path, monkeypatch):
        """An OSError reading the chapter file should return empty string."""
        chapters = tmp_path / "chapters"
        chapters.mkdir()
        chapter_file = chapters / "chapter_03.txt"
        chapter_file.write_text("text", encoding="utf-8")

        original_read_text = Path.read_text

        def patched_read_text(self, **kwargs):
            if self.name == "chapter_03.txt":
                raise OSError("read error")
            return original_read_text(self, **kwargs)

        monkeypatch.setattr(Path, "read_text", patched_read_text)
        text, mtime, kind = load_chapter_source_text(tmp_path, "chapter_03")
        assert text == ""
        assert kind == ""


# ---------------------------------------------------------------------------
# collapse_possessive_keys: reason merging, curly-apostrophe key
# ---------------------------------------------------------------------------

class TestCollapsePossessiveKeysEdgeCases:

    def test_reasons_are_merged_from_both_forms(self):
        merged = {
            "hood": GlossaryCandidate(
                term="Hood",
                type_guess=GlossaryTermType.CHARACTER,
                frequency=4,
                detection_reasons=["capitalized_mid_sentence"],
            ),
            "hood's": GlossaryCandidate(
                term="Hood's",
                type_guess=GlossaryTermType.OTHER,
                frequency=3,
                detection_reasons=["capitalized_sequence"],
            ),
        }
        result = collapse_possessive_keys(merged)
        assert "capitalized_mid_sentence" in result["hood"].detection_reasons
        assert "capitalized_sequence" in result["hood"].detection_reasons

    def test_curly_apostrophe_possessive_key_normalised(self):
        """A key ending with curly ’s should be collapsed just like straight 's."""
        merged = {
            "hood’s": GlossaryCandidate(
                term="Hood’s",
                type_guess=GlossaryTermType.OTHER,
                frequency=3,
            ),
        }
        result = collapse_possessive_keys(merged)
        # _strip_possessive handles curly apostrophe, so bare 'hood' should appear
        assert "hood" in result
        assert result["hood"].term == "Hood"

    def test_higher_priority_type_wins_on_merge(self):
        """Merging a CHARACTER and an OTHER should keep CHARACTER."""
        merged = {
            "nelson": GlossaryCandidate(
                term="Nelson",
                type_guess=GlossaryTermType.CHARACTER,
                frequency=10,
                detection_reasons=["capitalized_mid_sentence"],
            ),
            "nelson's": GlossaryCandidate(
                term="Nelson's",
                type_guess=GlossaryTermType.OTHER,
                frequency=5,
                detection_reasons=["capitalized_sequence"],
            ),
        }
        result = collapse_possessive_keys(merged)
        assert result["nelson"].type_guess == GlossaryTermType.CHARACTER

    def test_no_possessives_unchanged(self):
        merged = {
            "burnham thorpe": GlossaryCandidate(
                term="Burnham Thorpe",
                type_guess=GlossaryTermType.PLACE,
                frequency=4,
            ),
        }
        result = collapse_possessive_keys(merged)
        assert "burnham thorpe" in result
        assert result["burnham thorpe"].frequency == 4


# ---------------------------------------------------------------------------
# extract_candidates: max_zipf_capitalized / max_zipf_mixed parameter threading
# ---------------------------------------------------------------------------

class TestExtractCandidatesZipfThresholds:

    def test_permissive_max_zipf_admits_high_zipf_dict_words(self):
        """max_zipf_capitalized=6.0 should admit always-capitalized words
        whose literary Zipf is 5.0 (e.g. 'Dragon' used as a name)."""
        freq = _freq_checker({"dragon": 5.0}, default=3.0)
        text = "Dragon appeared. Dragon roared. Dragon flew. Dragon vanished."
        report = extract_candidates(
            text, min_frequency=2, max_candidates=100,
            freq_checker=freq, max_zipf_capitalized=6.0,
        )
        terms = {c.term.lower() for c in report.candidates}
        assert "dragon" in terms

    def test_restrictive_max_zipf_rejects_high_zipf_dict_words(self):
        """max_zipf_capitalized=3.0 should reject 'Dragon' (Zipf 5.0)."""
        freq = _freq_checker({"dragon": 5.0}, default=3.0)
        text = "Dragon appeared. Dragon roared. Dragon flew. Dragon vanished."
        report = extract_candidates(
            text, min_frequency=2, max_candidates=100,
            freq_checker=freq, max_zipf_capitalized=3.0,
        )
        terms = {c.term.lower() for c in report.candidates}
        assert "dragon" not in terms

    def test_max_zipf_mixed_controls_rare_literary_extraction(self):
        """Lowering max_zipf_mixed should tighten the rare-literary extractor."""
        freq = _freq_checker({"halyard": 2.0, "palmer": 3.5}, default=3.0)
        # Use common words in the dict, plus halyard/palmer as rare ones
        vocab = {"halyard", "palmer", "the", "a", "and", "was", "walked", "frayed"}
        dc = _dict_checker(vocab)

        text = (
            "The halyard snapped. The halyard frayed. The halyard was old. "
            "The palmer walked. The palmer rested. The palmer prayed."
        )
        # With max_zipf_mixed=3.2: both halyard(2.0) and palmer(3.5>3.2) ... only halyard
        report = extract_candidates(
            text, min_frequency=2, max_candidates=100,
            freq_checker=freq, max_zipf_mixed=3.2,
        )
        terms = {c.term.lower() for c in report.candidates}
        assert "halyard" in terms
        assert "palmer" not in terms


# ---------------------------------------------------------------------------
# prune_contained_terms: empty-string term handled gracefully
# ---------------------------------------------------------------------------

class TestPruneContainedTermsEdgeCases:

    def test_empty_term_is_skipped(self):
        """A candidate with an empty term string should not raise and is pruned."""
        text = "Nelson stood. Nelson turned. Nelson left."
        merged = {
            "": GlossaryCandidate(
                term="", type_guess=GlossaryTermType.OTHER, frequency=3,
            ),
            "nelson": GlossaryCandidate(
                term="Nelson", type_guess=GlossaryTermType.CHARACTER, frequency=3,
            ),
        }
        result = prune_contained_terms(merged, text, min_frequency=2)
        assert "nelson" in result
        assert "" not in result

    def test_single_candidate_that_matches_is_kept(self):
        text = "Burnham Thorpe was lovely. Burnham Thorpe had a church."
        merged = {
            "burnham thorpe": GlossaryCandidate(
                term="Burnham Thorpe",
                type_guess=GlossaryTermType.PLACE,
                frequency=2,
            ),
        }
        result = prune_contained_terms(merged, text, min_frequency=2)
        assert "burnham thorpe" in result


# ---------------------------------------------------------------------------
# format_candidates_for_prompt: edge cases
# ---------------------------------------------------------------------------

class TestFormatCandidatesForPrompt:

    def test_empty_list_returns_empty_string(self):
        from src.glossary_bootstrap import format_candidates_for_prompt
        assert format_candidates_for_prompt([]) == ""

    def test_legacy_path_when_no_contexts_key(self):
        """Candidates without a 'contexts' key use the one-line legacy format."""
        from src.glossary_bootstrap import format_candidates_for_prompt
        candidates = [
            {"term": "Nelson", "type_guess": "character", "frequency": 5},
            {"term": "Cadiz", "type_guess": "place", "frequency": 3},
        ]
        result = format_candidates_for_prompt(candidates)
        assert "- Nelson (type guess: character, frequency: 5)" in result
        assert "- Cadiz (type guess: place, frequency: 3)" in result

    def test_word_mode_path_when_some_contexts_empty(self):
        """When ANY candidate has a non-empty 'contexts' list, word-mode layout
        is used. A candidate with an empty list gets the no-match marker."""
        from src.glossary_bootstrap import format_candidates_for_prompt
        # has_contexts = any(c.get("contexts") ...) — needs at least one truthy entry
        candidates = [
            {"term": "Wellington", "type_guess": "character",
             "frequency": 2, "contexts": []},
            {"term": "Nelson", "type_guess": "character",
             "frequency": 5, "contexts": [("source", "Nelson stood on deck.")]},
        ]
        result = format_candidates_for_prompt(candidates)
        assert "1. Wellington" in result
        assert "(no in-text context found)" in result
        assert "Nelson" in result

    def test_word_mode_snippet_whitespace_collapsed(self):
        """Multi-line snippets should have whitespace collapsed in the output."""
        from src.glossary_bootstrap import format_candidates_for_prompt
        candidates = [
            {
                "term": "Nelson",
                "type_guess": "character",
                "frequency": 5,
                "contexts": [("Ch1", "Lord  Nelson\n   stood   on  deck.")],
            },
        ]
        result = format_candidates_for_prompt(candidates)
        assert "Lord Nelson stood on deck." in result


# ---------------------------------------------------------------------------
# find_first_word_contexts: empty / punct-only text; find_first_contexts cap
# ---------------------------------------------------------------------------

class TestFindFirstWordContextsEdgeCases:

    def test_punct_only_text_returns_fragment(self):
        """Text with only punctuation around the term still matches."""
        pos, ctx = find_first_word_contexts(
            "Nelson", [("source", "---Nelson---")], max_contexts=1
        )
        assert pos is not None
        assert len(ctx) == 1
        _, frag = ctx[0]
        assert "Nelson" in frag

    def test_empty_chapter_list(self):
        pos, ctx = find_first_word_contexts("Nelson", [], max_contexts=2)
        assert pos is None
        assert ctx == []

    def test_term_at_very_start_no_leading_elision(self):
        text = "Nelson stood at the bow of the ship and surveyed the horizon."
        _, ctx = find_first_word_contexts(
            "Nelson", [("source", text)],
            max_contexts=1, words_before=5, words_after=5,
        )
        assert len(ctx) == 1
        _, frag = ctx[0]
        assert not frag.startswith("...")

    def test_term_at_very_end_no_trailing_elision(self):
        text = "The ship sailed and then appeared Nelson"
        _, ctx = find_first_word_contexts(
            "Nelson", [("source", text)],
            max_contexts=1, words_before=5, words_after=5,
        )
        assert len(ctx) == 1
        _, frag = ctx[0]
        assert not frag.endswith("...")


class TestFindFirstContextsMaxContexts:

    def test_max_contexts_stops_across_chapters(self):
        """max_contexts is respected even when hits span multiple chapters."""
        chapters = [
            ("Ch 1", ["Nelson rode in.", "Nelson left."]),
            ("Ch 2", ["Nelson watched.", "Nelson turned."]),
        ]
        pos, ctx = find_first_contexts("Nelson", chapters, max_contexts=3)
        assert len(ctx) == 3
        # First hit is in chapter index 0
        assert pos == (0, 0)

    def test_max_contexts_1_returns_single_hit(self):
        chapters = [
            ("Ch 1", ["Nelson here.", "And again Nelson."]),
        ]
        pos, ctx = find_first_contexts("Nelson", chapters, max_contexts=1)
        assert len(ctx) == 1
        assert ctx[0][1] == "Nelson here."
