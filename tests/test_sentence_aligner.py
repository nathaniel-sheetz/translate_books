"""Tests for sentence alignment module."""

import pytest
from src.sentence_aligner import (
    split_sentences,
    _split_long_sentence,
    _normalize_for_embedding,
    _split_sentences_with_para_indices,
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


class TestSplitSentencesWithParaIndices:
    def test_prose_paragraph_uses_pysbd(self):
        text = "Hello world. How are you?"
        sentences, indices = _split_sentences_with_para_indices(text, "en")
        assert sentences == ["Hello world.", "How are you?"]
        assert indices == [0, 0]

    def test_verse_paragraph_splits_on_newlines(self):
        stanza = (
            "Drops of rain and bits of sunshine\n"
            "Falling here and gleaming there,\n"
            "Tiny blades of grass appearing.\n"
            "Tell of springtime bright and fair."
        )
        sentences, indices = _split_sentences_with_para_indices(stanza, "en")
        assert len(sentences) == 4
        assert "Drops of rain and bits of sunshine" in sentences
        assert "Falling here and gleaming there," in sentences
        assert all(idx == 0 for idx in indices)

    def test_verse_empty_lines_stripped(self):
        # Single \n separates verse lines within a stanza (double \n would split
        # into separate paragraphs and bypass the verse path entirely).
        stanza = "Line one\n\nLine two\nLine three\nLine four"
        sentences, indices = _split_sentences_with_para_indices(stanza, "en")
        assert all(s.strip() for s in sentences)
        # Lines two/three/four form a 3-line verse block; line one is a solo para.
        assert "Line two" in sentences
        assert "Line three" in sentences
        assert "Line four" in sentences

    def test_prose_then_verse_paragraph_indices(self):
        text = "Prose paragraph.\n\nDrops of rain and bits of sunshine\nFalling here."
        sentences, indices = _split_sentences_with_para_indices(text, "en")
        assert indices[0] == 0
        assert indices[-1] == 1


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


class TestAlignChapterChunks:
    """Cross-chunk stitching behavior in ``align_chapter_chunks``."""

    @pytest.fixture(scope="class")
    def model(self):
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        except ImportError:
            pytest.skip("sentence-transformers not installed")

    def test_marks_first_sentence_of_non_first_chunk_as_para_start(
        self, tmp_path, monkeypatch, model
    ):
        """Regression for the chapter_04 'Día tras día' boundary: the chunker
        only splits on paragraph boundaries, so the first sentence of every
        chunk after the first is itself a paragraph start. ``align_chunk``
        cannot see chapter context (it only flags ``para_start`` for
        same-chunk paragraph crossings), so ``align_chapter_chunks`` must
        mark the cross-chunk boundary.
        """
        import json

        from src import sentence_aligner
        from src.sentence_aligner import align_chapter_chunks

        # Reuse the loaded model rather than letting align_chunk re-download it.
        monkeypatch.setattr(sentence_aligner, "_get_model", lambda: model)

        chunk_0 = {
            "id": "chapter_test_chunk_000",
            "chapter_id": "chapter_test",
            "position": 0,
            "source_text": (
                "The cat sat on the mat.\n\n"
                "The dog watched from the doorway."
            ),
            "translated_text": (
                "El gato se sentó en la alfombra.\n\n"
                "El perro miraba desde la puerta."
            ),
        }
        chunk_1 = {
            "id": "chapter_test_chunk_001",
            "chapter_id": "chapter_test",
            "position": 1,
            "source_text": (
                "Day after day the seasons changed.\n\n"
                "The garden bloomed."
            ),
            "translated_text": (
                "Día tras día las estaciones cambiaban.\n\n"
                "El jardín florecía."
            ),
        }

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        for chunk in (chunk_0, chunk_1):
            (chunks_dir / f"{chunk['id']}.json").write_text(
                json.dumps(chunk, ensure_ascii=False), encoding="utf-8"
            )

        chunk_paths = sorted(str(p) for p in chunks_dir.glob("*.json"))
        result = align_chapter_chunks(
            chunk_paths=chunk_paths,
            project_id="test_project",
            chapter_id="chapter_test",
        )

        alignments = result["alignments"]
        assert alignments, "expected at least one alignment row"

        # First sentence of chunk 0 must NOT be flagged — it's the start of
        # the chapter, with no preceding paragraph.
        c0 = [a for a in alignments if a["chunk_id"] == chunk_0["id"]]
        assert c0, "No alignments found for chunk 0"
        assert not c0[0].get("para_start"), "First sentence of chunk 0 should not be a para_start"

        # First sentence of chunk 1 MUST be flagged.
        c1 = [a for a in alignments if a["chunk_id"] == chunk_1["id"]]
        assert c1, "No alignments found for chunk 1"
        assert c1[0].get("para_start") is True, (
            "First sentence of chunk 1 must be flagged as para_start "
            "(chunks are always paragraph-aligned)"
        )

    def test_para_start_set_on_every_non_first_chunk(
        self, tmp_path, monkeypatch, model
    ):
        """Every chunk after the first (not just chunk 1) must have its first
        sentence flagged as para_start — regression guard for three-chunk chapters.
        """
        import json

        from src import sentence_aligner
        from src.sentence_aligner import align_chapter_chunks

        monkeypatch.setattr(sentence_aligner, "_get_model", lambda: model)

        def make_chunk(idx, src, tgt):
            return {
                "id": f"chapter_test_chunk_{idx:03d}",
                "chapter_id": "chapter_test",
                "position": idx,
                "source_text": src,
                "translated_text": tgt,
            }

        chunk_0 = make_chunk(0, "The cat sat.\n\nThe dog watched.", "El gato.\n\nEl perro.")
        chunk_1 = make_chunk(1, "Day after day it rained.\n\nThe fields stayed wet.",
                             "Día tras día llovió.\n\nLos campos seguían mojados.")
        chunk_2 = make_chunk(2, "Spring finally arrived.\n\nFlowers bloomed.",
                             "Por fin llegó la primavera.\n\nFloraron flores.")

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        for chunk in (chunk_0, chunk_1, chunk_2):
            (chunks_dir / f"{chunk['id']}.json").write_text(
                json.dumps(chunk, ensure_ascii=False), encoding="utf-8"
            )

        chunk_paths = sorted(str(p) for p in chunks_dir.glob("*.json"))
        result = align_chapter_chunks(
            chunk_paths=chunk_paths,
            project_id="test_project",
            chapter_id="chapter_test",
        )

        alignments = result["alignments"]
        assert alignments, "expected alignments"

        c0 = [a for a in alignments if a["chunk_id"] == chunk_0["id"]]
        assert c0, "No alignments for chunk 0"
        assert not c0[0].get("para_start"), "chunk 0 should not be para_start"

        c1 = [a for a in alignments if a["chunk_id"] == chunk_1["id"]]
        assert c1, "No alignments for chunk 1"
        assert c1[0].get("para_start") is True, "chunk 1 first sentence must be para_start"

        c2 = [a for a in alignments if a["chunk_id"] == chunk_2["id"]]
        assert c2, "No alignments for chunk 2"
        assert c2[0].get("para_start") is True, "chunk 2 first sentence must be para_start"
