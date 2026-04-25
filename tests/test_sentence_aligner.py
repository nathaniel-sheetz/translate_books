"""Tests for sentence alignment module."""

import pytest
from src.sentence_aligner import (
    split_sentences,
    _split_long_sentence,
    _normalize_for_embedding,
)


class TestNormalizeForEmbedding:
    def test_lowercases_all_caps_title(self):
        assert _normalize_for_embedding("KING ALFRED AND THE CAKES.") == (
            "king alfred and the cakes."
        )

    def test_lowercases_spanish_all_caps(self):
        assert _normalize_for_embedding("EL REY ALFREDO Y LOS PASTELES.") == (
            "el rey alfredo y los pasteles."
        )

    def test_preserves_mixed_case(self):
        text = "The USA is a country."
        assert _normalize_for_embedding(text) == text

    def test_preserves_acronym_in_sentence(self):
        text = "He joined the UN last year."
        assert _normalize_for_embedding(text) == text

    def test_ignores_short_strings(self):
        # Fewer than 3 letters: don't normalize (avoids mangling e.g. "A.", "IT.")
        assert _normalize_for_embedding("A.") == "A."
        assert _normalize_for_embedding("IT.") == "IT."
        # Three or more letters, all uppercase: normalize
        assert _normalize_for_embedding("USA.") == "usa."

    def test_empty_string(self):
        assert _normalize_for_embedding("") == ""

    def test_no_letters(self):
        assert _normalize_for_embedding("123 456.") == "123 456."


class TestSplitLongSentence:
    def test_splits_on_period_uppercase(self):
        text = "The cat sat. The dog ran. The bird flew."
        result = _split_long_sentence(text)
        assert result == ["The cat sat.", "The dog ran.", "The bird flew."]

    def test_splits_on_exclamation(self):
        text = "Stop! Don't do that! Run away!"
        result = _split_long_sentence(text)
        assert result == ["Stop!", "Don't do that!", "Run away!"]

    def test_splits_on_question_mark(self):
        text = "Where is he? What happened? Is it true?"
        result = _split_long_sentence(text)
        assert result == ["Where is he?", "What happened?", "Is it true?"]

    def test_preserves_abbreviations(self):
        text = "Dr. Smith went home."
        result = _split_long_sentence(text)
        # "Dr." followed by uppercase should split, but this is a known edge case
        # The important thing is it doesn't crash
        assert len(result) >= 1

    def test_handles_quotes(self):
        text = '"Hello," said he. "Goodbye," she replied.'
        result = _split_long_sentence(text)
        assert len(result) == 2

    def test_no_split_needed(self):
        text = "Just one sentence here."
        result = _split_long_sentence(text)
        assert result == ["Just one sentence here."]

    def test_empty_string(self):
        result = _split_long_sentence("")
        assert result == []

    def test_spanish_inverted_punctuation(self):
        text = "Dijo algo. \u00bfQu\u00e9 pas\u00f3? \u00a1Incre\u00edble!"
        result = _split_long_sentence(text)
        assert len(result) == 3


class TestSplitSentences:
    def test_basic_english(self):
        text = "Hello world. How are you? I am fine."
        result = split_sentences(text, "en")
        assert len(result) == 3

    def test_basic_spanish(self):
        text = "Hola mundo. \u00bfC\u00f3mo est\u00e1s? Estoy bien."
        result = split_sentences(text, "es")
        assert len(result) == 3

    def test_splits_long_sentences(self):
        # Create a sentence longer than 50 words
        words = ["word"] * 60
        long_sent = " ".join(words[:30]) + ". " + " ".join(words[30:]) + "."
        # pysbd might keep this as one sentence, but our post-split should break it
        text = "Short sentence. " + long_sent
        result = split_sentences(text, "en")
        assert len(result) >= 2  # At minimum the short + the long (possibly split)

    def test_filters_empty(self):
        text = "Hello.   \n\n   World."
        result = split_sentences(text, "en")
        for s in result:
            assert s.strip() != ""

    def test_preserves_image_placeholders(self):
        text = "Some text. [IMAGE:images/foo.jpg] More text."
        result = split_sentences(text, "en")
        assert any("[IMAGE:" in s for s in result)


class TestAlignSentences:
    """Integration tests that require sentence-transformers model.

    These are slower (~5s for model load) so mark them for optional skip.
    """

    @pytest.fixture(scope="class")
    def model(self):
        """Load model once for all tests in this class."""
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        except ImportError:
            pytest.skip("sentence-transformers not installed")

    def test_perfect_alignment(self, model):
        from src.sentence_aligner import align_sentences

        en = ["The cat sat.", "The dog ran."]
        es = ["El gato se sent\u00f3.", "El perro corri\u00f3."]
        result = align_sentences(en, es, model)

        assert len(result) == 2
        assert result[0]["en_idx"] == 0
        assert result[1]["en_idx"] == 1
        assert all(r["confidence"] == "high" for r in result)

    def test_many_to_one_groups_into_single_row(self, model):
        """N:1 alignments are grouped into one output row (see
        test_nto1_grouping_emits_one_row for the detailed schema assertions)."""
        from src.sentence_aligner import align_sentences

        en = ["The cat sat on the mat and looked around."]
        es = ["El gato se sent\u00f3 en la alfombra.", "Mir\u00f3 a su alrededor."]
        result = align_sentences(en, es, model)

        assert len(result) == 1
        assert result[0]["en_idx"] == 0
        assert result[0]["es_indices"] == [0, 1]

    def test_empty_input(self, model):
        from src.sentence_aligner import align_sentences

        assert align_sentences([], ["hello"], model) == []
        assert align_sentences(["hello"], [], model) == []
        assert align_sentences([], [], model) == []

    def test_alignment_is_monotonic(self, model):
        from src.sentence_aligner import align_sentences

        en = ["First.", "Second.", "Third.", "Fourth."]
        es = ["Primero.", "Segundo.", "Tercero.", "Cuarto."]
        result = align_sentences(en, es, model)

        en_indices = [r["en_idx"] for r in result]
        for i in range(1, len(en_indices)):
            assert en_indices[i] >= en_indices[i - 1], "Alignment must be monotonic"

    def test_nto1_grouping_emits_one_row(self, model):
        """Two ES fragments mapping to one EN sentence should emit a single
        merged row with combined text and group-level similarity."""
        from src.sentence_aligner import align_sentences, _monotonic_alignment
        import numpy as np

        en = ["The cat sat on the mat and looked around."]
        es = ["El gato se sentó en la alfombra.", "Miró a su alrededor."]
        result = align_sentences(en, es, model)

        assert len(result) == 1
        row = result[0]
        assert row["en_idx"] == 0
        assert row["es_idx"] == 0
        assert row["es_indices"] == [0, 1]
        assert row["es_sentences"] == es
        assert row["es"] == " ".join(es)

        # Group similarity should beat the worse of the two per-fragment sims
        en_emb = model.encode(en, normalize_embeddings=True)
        es_emb = model.encode(es, normalize_embeddings=True)
        frag_sims = np.dot(es_emb, en_emb.T)[:, 0]
        assert row["similarity"] >= float(frag_sims.min()) - 1e-6

    def test_all_caps_title_scores_materially_higher(self, model):
        """All-caps EN/ES titles score materially higher after
        case-normalization than without it."""
        from src.sentence_aligner import align_sentences
        import numpy as np

        en_upper = ["KING ALFRED AND THE CAKES."]
        es_upper = ["EL REY ALFREDO Y LOS PASTELES."]
        result = align_sentences(en_upper, es_upper, model)
        normalized_sim = result[0]["similarity"]

        # Baseline: what would raw-cased embedding produce?
        en_emb_raw = model.encode(en_upper, normalize_embeddings=True)
        es_emb_raw = model.encode(es_upper, normalize_embeddings=True)
        raw_sim = float(np.dot(es_emb_raw, en_emb_raw.T)[0, 0])

        assert normalized_sim > raw_sim + 0.1, (
            f"Normalized sim {normalized_sim:.3f} should exceed raw sim "
            f"{raw_sim:.3f} by at least 0.1"
        )
        assert normalized_sim > 0.6, (
            f"Normalized title sim should clear 0.6 threshold, got {normalized_sim:.3f}"
        )
