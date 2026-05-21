"""Tests for src/utils/glossary_context.py shared helpers."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.glossary_context import (
    _normalize_quotes,
    _term_pattern,
    find_first_contexts,
    find_first_word_contexts,
)


class TestTermPattern:

    def test_single_token_word_boundary(self):
        pat = _term_pattern("Nelson")
        assert pat.search("Lord Nelson rode out.")
        assert pat.search("nelson")  # case-insensitive
        # word-boundary: 'Nelsonish' should NOT match (after 'Nelson' is a word char)
        assert pat.search("Nelsonish words don't match wholly.") is None
        assert pat.search("Bobson") is None

    def test_single_token_matches_plural_suffix(self):
        # Mirrors _forced_term_pattern in scripts/extract_glossary_candidates.py
        # so forced terms whose only occurrences are plural still find context.
        pat = _term_pattern("doughnut")
        assert pat.search("a tray of doughnuts cooled on the counter")
        assert pat.search("one doughnut remained")
        pat_es = _term_pattern("dress")
        assert pat_es.search("the dresses were stitched by hand")
        # Suffix is optional: a term ending in something the suffix would
        # falsely extend shouldn't bleed into adjacent word chars.
        pat_nelson = _term_pattern("Nelson")
        assert pat_nelson.search("Nelsonish words don't match wholly.") is None

    def test_s_ending_term_does_not_over_match(self):
        # Terms already ending in 's' must NOT get the plural suffix because
        # "Atlas" + "(?:es|s)?" would match "atlases" (a different word).
        pat = _term_pattern("Atlas")
        assert pat.search("Atlas carried the world")  # base form matches
        assert pat.search("The atlases were worn.") is None  # must NOT match
        pat_bus = _term_pattern("Pericles")
        assert pat_bus.search("Pericles spoke") is not None
        assert pat_bus.search("Pericleses") is None  # junk form must not match

    def test_multi_word_term_allows_punctuation_separator(self):
        pat = _term_pattern("dictator Aulus")
        assert pat.search("the dictator Aulus marched")
        assert pat.search("the dictator, Aulus marched")
        assert pat.search("the dictator -- Aulus marched")

    def test_quote_normalization_via_helpers(self):
        # _term_pattern normalizes only the term; callers must normalize the
        # text. Exercise both via find_first_word_contexts which normalizes
        # the text internally.
        for variant in ("d'Artagnan", "d’Artagnan", "dʼArtagnan"):
            text = f"Then {variant} drew his sword."
            for term in ("d'Artagnan", "d’Artagnan"):
                _, ctx = find_first_word_contexts(
                    term, [("source", text)], max_contexts=1,
                )
                assert ctx, f"term={term!r} did not match text variant={variant!r}"


class TestNormalizeQuotes:

    def test_collapses_all_apostrophe_variants(self):
        assert _normalize_quotes("don’t") == "don't"
        assert _normalize_quotes("dʼArtagnan") == "d'Artagnan"
        assert _normalize_quotes("plain'text") == "plain'text"


class TestFindFirstWordContexts:

    SHORT_TEXT = (
        "The Royal Navy assembled. Lord Nelson stood on the deck of HMS Victory "
        "and surveyed the line. The wind was fair. Later that morning Nelson "
        "issued his famous signal. The fleet engaged the enemy at noon."
    )

    def test_basic_window_and_count(self):
        pos, ctx = find_first_word_contexts(
            "Nelson", [("source", self.SHORT_TEXT)],
            max_contexts=2, words_before=4, words_after=3,
        )
        assert pos is not None
        assert pos[0] == 0  # chapter index
        assert len(ctx) == 2
        # Both fragments should mention Nelson
        for label, frag in ctx:
            assert label == "source"
            assert "Nelson" in frag

    def test_elision_markers_when_clipped(self):
        pos, ctx = find_first_word_contexts(
            "Nelson", [("source", self.SHORT_TEXT)],
            max_contexts=1, words_before=2, words_after=2,
        )
        assert len(ctx) == 1
        _, frag = ctx[0]
        # First Nelson is mid-text, so both ends should be clipped.
        assert frag.startswith("... ")
        assert frag.endswith(" ...")

    def test_no_elision_at_text_edges(self):
        text = "Nelson stood at the bow"  # term is at start, end exhausted
        _, ctx = find_first_word_contexts(
            "Nelson", [("source", text)],
            max_contexts=1, words_before=10, words_after=10,
        )
        _, frag = ctx[0]
        assert not frag.startswith("...")
        assert not frag.endswith("...")

    def test_no_match_returns_none(self):
        pos, ctx = find_first_word_contexts(
            "Wellington", [("source", self.SHORT_TEXT)],
        )
        assert pos is None
        assert ctx == []

    def test_max_contexts_cap_honored(self):
        text = "Nelson Nelson Nelson Nelson Nelson Nelson"
        _, ctx = find_first_word_contexts(
            "Nelson", [("source", text)],
            max_contexts=3, words_before=1, words_after=1,
        )
        assert len(ctx) == 3

    def test_first_position_reflects_first_match(self):
        chapters = [
            ("Ch 1", "Nothing here yet."),
            ("Ch 2", "And then Nelson appeared."),
            ("Ch 3", "Nelson again."),
        ]
        pos, ctx = find_first_word_contexts("Nelson", chapters, max_contexts=2)
        assert pos is not None
        assert pos[0] == 1  # chapter index 1 = "Ch 2"
        assert ctx[0][0] == "Ch 2"
        assert ctx[1][0] == "Ch 3"

    def test_multi_word_term_match(self):
        text = "The Battle of Copenhagen began at dawn. The Battle of Copenhagen was fierce."
        _, ctx = find_first_word_contexts(
            "Battle of Copenhagen", [("source", text)],
            max_contexts=2, words_before=3, words_after=3,
        )
        assert len(ctx) == 2
        for _, frag in ctx:
            assert "Battle of Copenhagen" in frag


class TestFindFirstContexts:

    def test_returns_containing_sentences(self):
        chapters = [
            ("Ch 1", ["Nelson rode in.", "Then he left."]),
            ("Ch 2", ["The fleet sailed.", "Nelson watched from the deck."]),
        ]
        pos, ctx = find_first_contexts("Nelson", chapters, max_contexts=2)
        assert pos == (0, 0)
        assert len(ctx) == 2
        assert ctx[0] == ("Ch 1", "Nelson rode in.")
        assert ctx[1] == ("Ch 2", "Nelson watched from the deck.")

    def test_no_match(self):
        chapters = [("Ch 1", ["Nothing here."])]
        pos, ctx = find_first_contexts("Nelson", chapters)
        assert pos is None
        assert ctx == []
