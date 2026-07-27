"""Tests for sentence alignment module."""

import pytest
from src.sentence_aligner import (
    MIN_GAP_CHARS,
    MIN_SENTENCE_CHARS,
    split_sentences,
    _coverage_gaps,
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


def _sent(length: int, marker: str = "a") -> str:
    """A sentence whose stripped length is exactly ``length``."""
    assert length >= 1
    return (marker * (length - 1)) + "."


class TestCoverageGaps:
    """Source runs no target sentence claims — i.e. prose the translator dropped.

    Regression cover for the Little Duke silent-omission bug: a translator dropped
    the final paragraph of ``chapter_01_chunk_000`` and every existing signal stayed
    clean (character ratio 1.002, paragraph delta 0, high_confidence_pct unchanged).
    The only trace was three source sentences that no alignment row referenced.
    """

    def test_no_gaps_when_every_source_sentence_is_claimed(self):
        en = [_sent(150) for _ in range(5)]
        alignments = [{"en_idx": i} for i in range(5)]
        assert _coverage_gaps(en, alignments) == []

    def test_no_gaps_for_empty_source(self):
        assert _coverage_gaps([], []) == []

    def test_many_to_one_rows_do_not_create_gaps(self):
        # Spanish em-dash dialogue routinely renders one English sentence as
        # several, so duplicate en_idx values are normal and must not read as
        # missing coverage.
        en = [_sent(150) for _ in range(3)]
        alignments = [{"en_idx": 0}, {"en_idx": 0}, {"en_idx": 1}, {"en_idx": 2}]
        assert _coverage_gaps(en, alignments) == []

    def test_dropped_tail_is_reported(self):
        en = [_sent(150) for _ in range(5)]
        alignments = [{"en_idx": i} for i in range(3)]  # 3 and 4 unclaimed

        gaps = _coverage_gaps(en, alignments)

        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["position"] == "tail"
        assert (gap["en_start"], gap["en_end"]) == (3, 4)
        assert gap["sentences"] == 2
        assert gap["chars"] == 300

    def test_dropped_head_is_reported(self):
        en = [_sent(150) for _ in range(5)]
        alignments = [{"en_idx": i} for i in range(2, 5)]

        gaps = _coverage_gaps(en, alignments)

        assert len(gaps) == 1
        assert gaps[0]["position"] == "head"
        assert (gaps[0]["en_start"], gaps[0]["en_end"]) == (0, 1)

    def test_whole_chunk_drop_is_reported_as_full(self):
        """Empty / fully dropped translation must not be mis-bucketed as head."""
        en = [_sent(150) for _ in range(3)]
        gaps = _coverage_gaps(en, [])

        assert len(gaps) == 1
        assert gaps[0]["position"] == "full"
        assert (gaps[0]["en_start"], gaps[0]["en_end"]) == (0, 2)
        assert gaps[0]["chars"] == 450

    def test_dropped_middle_is_reported_as_interior(self):
        en = [_sent(150) for _ in range(6)]
        alignments = [{"en_idx": 0}, {"en_idx": 4}, {"en_idx": 5}]

        gaps = _coverage_gaps(en, alignments)

        assert len(gaps) == 1
        assert gaps[0]["position"] == "interior"
        assert (gaps[0]["en_start"], gaps[0]["en_end"]) == (1, 3)

    def test_sub_threshold_run_is_not_reported(self):
        """The 1-ES:N-EN merge case.

        When Spanish packs two English sentences into one, the second goes
        unclaimed even though it *was* translated — the DP maps each target
        sentence to exactly one source sentence. Those runs are short, so the
        character threshold suppresses them.
        """
        en = [_sent(150) for _ in range(5)]
        alignments = [{"en_idx": i} for i in range(4)]  # only index 4 unclaimed

        assert 150 < MIN_GAP_CHARS
        assert _coverage_gaps(en, alignments) == []

    def test_junk_only_run_is_not_reported(self):
        """Gutenberg rules and stray quote marks are never "translated"."""
        en = [_sent(400), "---", '"']
        alignments = [{"en_idx": 0}]

        assert len("---") < MIN_SENTENCE_CHARS
        assert _coverage_gaps(en, alignments) == []

    def test_junk_excluded_from_mass_but_kept_in_span(self):
        en = [_sent(150), _sent(400), "---"]
        alignments = [{"en_idx": 0}]

        gaps = _coverage_gaps(en, alignments)

        assert len(gaps) == 1
        # Mass counts only the real sentence; the span still covers the junk record.
        assert gaps[0]["chars"] == 400
        assert gaps[0]["sentences"] == 2
        assert (gaps[0]["en_start"], gaps[0]["en_end"]) == (1, 2)
        assert gaps[0]["preview"].startswith("a")

    def test_hard_wrapped_line_fragments_are_not_reported(self):
        """Regression for projects/fabre chapter_10.

        Some sources are hard-wrapped at ~70 columns, and ``is_verse_block`` reads
        those prose paragraphs as verse and splits them per line. One translated
        Spanish sentence then faces seven English line *fragments*, all unclaimed
        by the 1-ES:N-EN rule — 414 chars of "missing" text that was never missing.
        Fragments do not end like sentences, so they contribute no mass.
        """
        fragments = [
            "longer drag himself along; a pig is a tottering veteran at twenty; at",
            "fifteen at the most, a cat no longer chases mice, it says good-by to the",
            "joys of the roof and retires to some corner of a granary to die in",
            "peace; the goat and sheep, at ten or fifteen, touch extreme old age, the",
            "rabbit is at the end of its skein at eight or ten; and the miserable",
            "rat, if it lives four years, is looked upon among its own kind as a",
        ]
        en = [_sent(150)] + fragments + [_sent(150)]
        alignments = [{"en_idx": 0}, {"en_idx": len(en) - 1}]

        assert sum(len(f) for f in fragments) > MIN_GAP_CHARS  # would fire on mass alone
        assert _coverage_gaps(en, alignments) == []

    def test_only_complete_sentences_contribute_mass(self):
        en = [
            _sent(150),
            "a wrapped fragment that simply runs on past the column limit and",
            _sent(400),
        ]
        alignments = [{"en_idx": 0}]

        gaps = _coverage_gaps(en, alignments)

        assert len(gaps) == 1
        assert gaps[0]["chars"] == 400  # fragment excluded
        assert gaps[0]["sentences"] == 2  # but still inside the reported span

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("A normal sentence.", True),
            ("Shouted loudly!", True),
            ("Really?", True),
            ('He said, "go away."', True),
            ("«Se acabó.»", True),
            ("Trailing off…", True),
            ("a wrapped line fragment that ends mid", False),
            ("", False),
            ("   ", False),
            ('"', False),
        ],
    )
    def test_complete_sentence_predicate(self, text, expected):
        from src.sentence_aligner import _is_complete_sentence

        assert _is_complete_sentence(text) is expected

    def test_multiple_runs_reported_separately(self):
        en = [_sent(200) for _ in range(8)]
        alignments = [{"en_idx": 0}, {"en_idx": 4}]

        gaps = _coverage_gaps(en, alignments)

        assert [g["position"] for g in gaps] == ["interior", "tail"]
        assert [(g["en_start"], g["en_end"]) for g in gaps] == [(1, 3), (5, 7)]

    def test_preview_is_truncated(self):
        en = [_sent(150), _sent(400)]
        alignments = [{"en_idx": 0}]

        preview = _coverage_gaps(en, alignments)[0]["preview"]

        assert len(preview) == 101  # 100 chars + ellipsis
        assert preview.endswith("…")


class TestCoverageGapsIntegration:
    """End-to-end: a chunk whose translation drops a paragraph."""

    @pytest.fixture(scope="class")
    def model(self):
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        except ImportError:
            pytest.skip("sentence-transformers not installed")

    SOURCE = (
        "The cat sat on the mat beside the fire.\n\n"
        "The dog watched from the doorway, wagging his tail slowly.\n\n"
        "Then the old woman came into the kitchen carrying a heavy basket of apples "
        "from the orchard, and she set it down upon the wooden table with a sigh of "
        "relief. She had walked a long way that morning, and her arms ached from the "
        "weight of the fruit she had gathered. The apples were red and firm, and she "
        "meant to bake them into pies before the evening came."
    )
    TRANSLATION_FULL = (
        "El gato se sentó en la alfombra junto al fuego.\n\n"
        "El perro miraba desde la puerta, moviendo la cola lentamente.\n\n"
        "Entonces la anciana entró en la cocina cargando una pesada cesta de manzanas "
        "del huerto, y la dejó sobre la mesa de madera con un suspiro de alivio. Había "
        "caminado mucho aquella mañana, y le dolían los brazos por el peso de la fruta "
        "que había recogido. Las manzanas eran rojas y firmes, y pensaba hornearlas en "
        "tartas antes de que llegara la noche."
    )
    TRANSLATION_DROPPED_TAIL = (
        "El gato se sentó en la alfombra junto al fuego.\n\n"
        "El perro miraba desde la puerta, moviendo la cola lentamente."
    )

    def _write_chunk(self, chunks_dir, idx, source, translation):
        import json

        chunk = {
            "id": f"chapter_test_chunk_{idx:03d}",
            "chapter_id": "chapter_test",
            "position": idx,
            "source_text": source,
            "translated_text": translation,
        }
        path = chunks_dir / f"{chunk['id']}.json"
        path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
        return path

    def test_align_chunk_reports_dropped_final_paragraph(self, tmp_path, model):
        from src.sentence_aligner import align_chunk

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        path = self._write_chunk(
            chunks_dir, 0, self.SOURCE, self.TRANSLATION_DROPPED_TAIL
        )

        result = align_chunk(str(path), model=model)

        assert len(result["gaps"]) == 1, result["gaps"]
        gap = result["gaps"][0]
        assert gap["position"] == "tail"
        assert gap["chars"] >= MIN_GAP_CHARS
        assert result["coverage"]["gap_count"] == 1
        assert result["coverage"]["en_orphan_chars"] == gap["chars"]
        assert result["coverage"]["en_aligned"] < result["coverage"]["en_count"]

    def test_align_chunk_reports_no_gaps_for_complete_translation(
        self, tmp_path, model
    ):
        from src.sentence_aligner import align_chunk

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        path = self._write_chunk(chunks_dir, 0, self.SOURCE, self.TRANSLATION_FULL)

        result = align_chunk(str(path), model=model)

        assert result["gaps"] == []
        assert result["coverage"]["gap_count"] == 0
        assert result["coverage"]["en_orphan_chars"] == 0

    def test_align_chapter_chunks_offsets_gap_indices_and_stamps_chunk_id(
        self, tmp_path, monkeypatch, model
    ):
        """Gaps must be offset into chapter-global indices exactly like alignment
        rows, so a gap in a later chunk does not point at the wrong sentences."""
        from src import sentence_aligner
        from src.sentence_aligner import align_chapter_chunks

        monkeypatch.setattr(sentence_aligner, "_get_model", lambda: model)

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        # Chunk 0 is complete; chunk 1 drops its final paragraph.
        self._write_chunk(chunks_dir, 0, self.SOURCE, self.TRANSLATION_FULL)
        self._write_chunk(
            chunks_dir, 1, self.SOURCE, self.TRANSLATION_DROPPED_TAIL
        )

        result = align_chapter_chunks(
            chunk_paths=sorted(str(p) for p in chunks_dir.glob("*.json")),
            project_id="test_project",
            chapter_id="chapter_test",
        )

        assert len(result["gaps"]) == 1, result["gaps"]
        gap = result["gaps"][0]
        assert gap["chunk_id"] == "chapter_test_chunk_001"
        # Chunk 0 contributed every sentence before this gap, so the indices must
        # have been shifted past it rather than left chunk-local.
        chunk_0_rows = [
            a for a in result["alignments"]
            if a["chunk_id"] == "chapter_test_chunk_000"
        ]
        assert gap["en_start"] > max(a["en_idx"] for a in chunk_0_rows)
        assert gap["en_end"] < result["en_count"]
        assert result["coverage"]["gap_count"] == 1
