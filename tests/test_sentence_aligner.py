"""Tests for sentence alignment module."""

import numpy as np
import pytest
from src.sentence_aligner import (
    MIN_GAP_CHARS,
    MIN_SENTENCE_CHARS,
    split_sentences,
    _absorb_orphans,
    _coverage_gaps,
    _covered_en,
    _glue_units,
    _split_inside_quotes,
    _split_long_sentence,
    _normalize_for_embedding,
    _split_sentences_with_para_indices,
    _split_sentences_with_whole_indices,
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

    def test_english_title_abbreviation_is_not_a_boundary(self):
        text = '"For nothing, Mr. Harum-scarum? You are mistaken."'
        result = _split_long_sentence(text, "en")
        assert result == ['"For nothing, Mr. Harum-scarum?', 'You are mistaken."']

    def test_english_guard_covers_each_title_in_a_run(self):
        text = "She met Mrs. Dorking and Dr. Hardy there. They talked."
        result = _split_long_sentence(text, "en")
        assert result == ["She met Mrs. Dorking and Dr. Hardy there.", "They talked."]

    def test_abbreviation_guard_is_english_only(self):
        # The Spanish split is load-bearing (es_idx anchors annotations), so the
        # guard must leave it exactly as it was.
        text = "Vio al Sr. Hardy en la calle. Luego se fue."
        assert _split_long_sentence(text, "es") == _split_long_sentence(text)
        assert _split_long_sentence(text, "es") == ["Vio al Sr.", "Hardy en la calle.", "Luego se fue."]

    def test_english_initials_are_not_boundaries(self):
        text = "We met J. B. Smith and Maj. Hardy at the dock. Then we left."
        assert _split_long_sentence(text, "en") == [
            "We met J. B. Smith and Maj. Hardy at the dock.",
            "Then we left.",
        ]
        # Without the language the same text is still cut at each of them.
        assert _split_long_sentence(text, "es") == _split_long_sentence(text)
        assert len(_split_long_sentence(text)) == 5

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


class TestSplitInsideQuotes:
    """The aligner's source side splits sentences inside a quotation, so a
    short Spanish reply has a sentence of its own to match."""

    def _split(self, text):
        return _split_sentences_with_para_indices(text, "en", split_quotes=True)[0]

    def test_quoted_speech_splits_into_its_sentences(self):
        text = '"Yeah. Three fellers. Sort of onpleasant lookin\' chaps."'
        assert self._split(text) == [
            '"Yeah.',
            "Three fellers.",
            "Sort of onpleasant lookin' chaps.\"",
        ]

    def test_curly_quotes(self):
        text = "“I knew it! It is all his fault.”"
        assert self._split(text) == ["“I knew it!", "It is all his fault.”"]

    def test_quote_and_attribution_stay_whole(self):
        # The boundary sits at the closing quote, which is pysbd's business.
        text = '"Grandpa!" he cried.'
        assert self._split(text) == ['"Grandpa!" he cried.']

    def test_title_and_initial_inside_a_quote_are_not_boundaries(self):
        text = '"I saw Mr. Hardy and J. B. Smith there. They had left."'
        assert self._split(text) == [
            '"I saw Mr. Hardy and J. B. Smith there.',
            'They had left."',
        ]

    @pytest.mark.parametrize("text", [
        '"I saw Lieut. Hardy there," he said.',
        '"We sailed past Mt. Vernon and Ft. Worth," he said.',
    ])
    def test_titles_before_a_name_are_not_boundaries(self, text):
        assert self._split(text) == [text]

    def test_upper_case_title_is_not_a_boundary(self):
        text = '"I saw MR. HARDY there. He waved."'
        assert self._split(text) == ['"I saw MR. HARDY there.', 'He waved."']

    def test_ordinary_words_that_are_also_abbreviations_still_split(self):
        # pysbd lists "no" and "me" as abbreviations; a guard on its whole list
        # would leave a one-word reply stuck to the speech that follows it.
        assert self._split('"No. I will not. Go away, Tom."') == [
            '"No.',
            "I will not.",
            'Go away, Tom."',
        ]
        assert self._split('"It was me. Then he ran."') == ['"It was me.', 'Then he ran."']

    def test_pieces_of_one_sentence_share_a_whole_index(self):
        stanza = (
            "Drops of rain and bits of sunshine\n"
            "Falling here and gleaming there,\n"
            "Tiny blades of grass appearing.\n"
            "Tell of springtime bright and fair."
        )
        text = (
            '"Yeah. Three fellers. Sort of onpleasant lookin\' chaps." He nodded.'
            "\n\n" + stanza
        )
        sentences, paras, wholes = _split_sentences_with_whole_indices(
            text, "en", split_quotes=True
        )
        assert sentences[:4] == [
            '"Yeah.',
            "Three fellers.",
            "Sort of onpleasant lookin' chaps.\"",
            "He nodded.",
        ]
        # The quotation's three pieces are one sentence; each verse line is its own.
        assert wholes == [0, 0, 0, 1, 2, 3, 4, 5]
        assert paras == [0, 0, 0, 0, 1, 1, 1, 1]

        unsplit, _, unsplit_wholes = _split_sentences_with_whole_indices(text, "en")
        assert unsplit_wholes == list(range(len(unsplit)))

    def test_quote_state_carries_across_a_paragraphs_sentences(self):
        # pysbd sometimes cuts mid-quotation; the second record is still inside it.
        assert _split_inside_quotes(['"He ran.', 'He hid. He waited."']) == [
            '"He ran.',
            "He hid.",
            'He waited."',
        ]

    def test_outside_a_quotation_nothing_is_split(self):
        # Each paragraph starts closed, so an unclosed quote in the paragraph
        # before cannot leak into this one.
        assert _split_inside_quotes(["He hid. He waited."]) == ["He hid. He waited."]
        assert _split_sentences_with_para_indices(
            '"He never came back. Nobody knew why.\n\nThey passed Mt. Vernon at noon.',
            "en",
            split_quotes=True,
        ) == (
            ['"He never came back.', "Nobody knew why.", "They passed Mt. Vernon at noon."],
            [0, 0, 1],
        )

    def test_pieces_are_substrings_of_the_text(self):
        text = '“Did you? Well, I am glad of it, then,” laughed Pollyanna. She sat down.'
        pieces = self._split(text)
        assert pieces == [
            "“Did you?",
            "Well, I am glad of it, then,” laughed Pollyanna.",
            "She sat down.",
        ]
        assert " ".join(pieces) == text

    def test_default_split_is_unchanged(self):
        # The target side and every other caller must see the old behaviour.
        text = '"Yeah. Three fellers. Sort of onpleasant lookin\' chaps."'
        assert _split_sentences_with_para_indices(text, "en")[0] == [text]
        es = "—Sí. Tres fulanos. Medio antipáticos, la verdad."
        assert _split_sentences_with_para_indices(es, "es")[0] == [
            "—Sí.",
            "Tres fulanos.",
            "Medio antipáticos, la verdad.",
        ]


class TestGlueUnits:
    def test_inciso_fragment_joins_the_line_before_it(self):
        es = ["—¡Abuelo!", "—exclamó—.", "¡Misty está parada en el agua!"]
        assert _glue_units(es, [0, 0, 0]) == [[0, 1], [2]]

    def test_raya_followed_by_a_capital_starts_a_new_unit(self):
        # A new speaker's line, not a narrator's inciso.
        es = ["—¿Y ahora hacia dónde?", "—Hacia tierra firme."]
        assert _glue_units(es, [0, 1]) == [[0], [1]]

    def test_sentence_after_a_title_abbreviation_joins_it(self):
        es = ["El Sr.", "Hardy seguía en la biblioteca.", "Los chicos volvieron."]
        assert _glue_units(es, [0, 0, 0]) == [[0, 1], [2]]

    def test_a_run_of_fragments_forms_one_unit(self):
        es = ["—Sí, buen día —concedió el Sr.", "Stummer.", "—dijo otra vez—."]
        assert _glue_units(es, [0, 0, 0]) == [[0, 1, 2]]

    def test_continuation_punctuation_joins_the_sentence_before(self):
        es = ["—¡Juuu!", "... ¡Ja!", "... ¡ah!", "—gritaba.", "Sonaba distinto."]
        assert _glue_units(es, [0, 0, 0, 0, 0]) == [[0, 1, 2, 3], [4]]
        es = ["—Sí, sí, ¡qué membrana tan buena!", ", ¡qué patas tan grandes!"]
        assert _glue_units(es, [0, 0]) == [[0, 1]]

    def test_lowercase_start_alone_does_not_glue(self):
        # A verse line starts lowercase; lines must stay one unit each.
        es = ["Llega el viento del norte trayendo copos de nieve:", "viste los campos del blanco más puro,"]
        assert _glue_units(es, [0, 0]) == [[0], [1]]

    def test_never_glues_across_a_paragraph_break(self):
        es = ["—¡Abuelo!", "—exclamó—."]
        assert _glue_units(es, [0, 1]) == [[0], [1]]

    def test_without_paragraph_indices_everything_is_one_paragraph(self):
        es = ["—¡Abuelo!", "—exclamó—."]
        assert _glue_units(es) == [[0, 1]]

    def test_every_sentence_appears_exactly_once(self):
        es = ["Uno.", "—dijo—.", "Dos.", "El Dr.", "Tres."]
        units = _glue_units(es, [0, 0, 0, 1, 1])
        assert [i for unit in units for i in unit] == list(range(len(es)))

    def test_empty(self):
        assert _glue_units([]) == []


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

    def test_speech_tag_fragment_shares_its_lines_source(self, model):
        """pysbd cuts '—exclamó—.' off the line it follows. On its own the tag
        matches nothing; glued, it lands on the sentence that holds 'he cried'."""
        from src.sentence_aligner import align_sentences

        en = ['"Grandpa!" he cried.', '"Misty\'s standing in water!"']
        es = ["—¡Abuelo!", "—exclamó—.", "¡Misty está parada en el agua!"]
        result = align_sentences(en, es, model, es_para_indices=[0, 0, 0])

        assert [r["en_idx"] for r in result] == [0, 1]
        assert result[0]["es_indices"] == [0, 1]
        assert result[0]["es_sentences"] == es[:2]
        assert result[1]["es_idx"] == 2

    def test_title_abbreviation_fragment_stays_with_its_sentence(self, model):
        from src.sentence_aligner import align_sentences

        en = [
            "Mr. Hardy was still in the library when the boys returned home.",
            "He looked up from his papers.",
        ]
        es = [
            "El Sr.",
            "Hardy seguía en la biblioteca cuando los chicos volvieron a casa.",
            "Levantó la vista de sus papeles.",
        ]
        result = align_sentences(en, es, model, es_para_indices=[0, 0, 0])

        assert result[0]["en_idx"] == 0
        assert result[0]["es_indices"] == [0, 1]
        assert result[1]["en_idx"] == 1

    def test_one_spanish_sentence_takes_both_english_sentences(self, model):
        """A translator's merge: the second English sentence must not be left
        unclaimed, or the row shows half its source."""
        from src.sentence_aligner import align_sentences

        en = [
            "They got along perfectly together.",
            "They would sit side by side gossiping.",
            "Then the winter came and the snow fell.",
        ]
        es = [
            "Se llevaban perfectamente bien y se sentaban una junto a la otra a chismorrear.",
            "Luego llegó el invierno y cayó la nieve.",
        ]
        result = align_sentences(
            en, es, model, es_para_indices=[0, 0], en_para_indices=[0, 0, 0]
        )

        assert result[0]["en_idx"] == 0
        assert result[0]["en_indices"] == [0, 1]
        assert result[0]["en"] == f"{en[0]} {en[1]}"
        assert result[1]["en_idx"] == 2
        assert "en_indices" not in result[1]
        assert _coverage_gaps(en, result) == []

    def test_speech_tag_unit_takes_quote_and_attribution(self, model):
        """English writes the attribution as its own sentence; the glued
        Spanish unit needs both."""
        from src.sentence_aligner import align_sentences

        en = ['"The blacksnakes!"', "Frank exclaimed.", "The boys ran to the boat."]
        es = ["—¡Las culebras negras!", "—exclamó Frank.", "Los muchachos corrieron al bote."]
        result = align_sentences(
            en, es, model, es_para_indices=[0, 0, 1], en_para_indices=[0, 0, 1]
        )

        assert result[0]["es_indices"] == [0, 1]
        assert result[0]["en_indices"] == [0, 1]
        assert result[1]["en_idx"] == 2

    def test_orphan_in_another_paragraph_is_not_absorbed(self, model):
        """An untranslated heading sits in its own paragraph; it must not be
        folded into the sentence next door."""
        from src.sentence_aligner import align_sentences

        en = [
            "The Hold-Up",
            "Chief Collig was a burly, red-faced man.",
            "He was fond of telling long stories.",
        ]
        es = [
            "El jefe Collig era un hombre corpulento y colorado.",
            "Le gustaba contar historias largas.",
        ]
        result = align_sentences(
            en, es, model, es_para_indices=[0, 0], en_para_indices=[0, 1, 1]
        )

        assert [r["en_idx"] for r in result] == [1, 2]
        assert all("en_indices" not in r for r in result)

    def test_nothing_is_absorbed_without_source_paragraphs(self, model):
        from src.sentence_aligner import align_sentences

        en = [
            "They got along perfectly together.",
            "They would sit side by side gossiping.",
        ]
        es = ["Se llevaban perfectamente bien y se sentaban una junto a la otra a chismorrear."]
        result = align_sentences(en, es, model)

        assert len(result) == 1
        assert "en_indices" not in result[0]

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

    def test_sentences_a_row_absorbed_count_as_claimed(self):
        # A 1:N row lists every source sentence it covers in en_indices.
        en = [_sent(200) for _ in range(4)]
        alignments = [{"en_idx": 0, "en_indices": [0, 1, 2]}, {"en_idx": 3}]
        assert _coverage_gaps(en, alignments) == []

    def test_a_reportable_run_is_never_absorbable(self):
        from src.sentence_aligner import MAX_ABSORB_SENTENCES, _absorbable

        # Two sentences that together clear MIN_GAP_CHARS: a real drop.
        heavy = [_sent(MIN_GAP_CHARS // 2 + 10) for _ in range(2)]
        assert not _absorbable(heavy, [0, 1])
        # Light enough, but too many sentences.
        light = [_sent(40) for _ in range(MAX_ABSORB_SENTENCES + 1)]
        assert not _absorbable(light, list(range(len(light))))
        assert _absorbable(light, [0, 1])
        # Rules and stray punctuation are nobody's source.
        assert not _absorbable(["---"], [0])
        assert not _absorbable(light, [])

    def test_pieces_of_one_sentence_are_weighed_together_for_absorption(self):
        from src.sentence_aligner import _absorbable

        # One quotation cut in two: neither piece clears MIN_GAP_CHARS alone, but
        # the sentence they were would have been a reportable gap.
        pieces = [_sent(MIN_GAP_CHARS - 10), "Go away, Tom.\""]
        assert _absorbable(pieces, [0, 1])
        assert not _absorbable(pieces, [0, 1], [0, 0])
        # Two separate sentences are still weighed one by one.
        assert _absorbable(pieces, [0, 1], [0, 1])

    def test_dropped_quick_dialogue_is_reported(self):
        # Fifteen dropped lines, each cut into pieces too short to count alone.
        line = '"No. I will not. Go away, Tom."'
        source = "He stayed at home that day.\n\n" + "\n\n".join([line] * 15)
        en, _, wholes = _split_sentences_with_whole_indices(source, "en", split_quotes=True)
        assert len(en) == 46
        alignments = [{"en_idx": 0}]

        assert _coverage_gaps(en, alignments) == []

        gaps = _coverage_gaps(en, alignments, wholes)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["position"] == "tail"
        assert (gap["en_start"], gap["en_end"]) == (1, 45)
        assert gap["chars"] == 15 * len(line)
        assert gap["preview"] == line

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


class _StubModel:
    """encode() gives each text the similarity the test names for it.

    Every row vector in TestAbsorbOrphans is [1, 0], so a text's score against
    its row is exactly the first component returned here.
    """

    def __init__(self, scores: dict[str, float]):
        self.scores = scores

    def encode(self, texts, normalize_embeddings=True):
        return np.array([
            [s, (1 - s * s) ** 0.5] for s in (self.scores.get(t, 0.0) for t in texts)
        ])


class TestAbsorbOrphans:
    """_absorb_orphans' bookkeeping, with scores fixed by a stub encoder."""

    EN = ["He ran.", "He hid.", "He waited.", "She called.", "Nobody came."]

    @staticmethod
    def _rows(*en_indices: int) -> tuple[list[dict], list]:
        rows = [
            {
                "es_idx": i,
                "en_idx": en_idx,
                "en": TestAbsorbOrphans.EN[en_idx],
                "similarity": 0.5,
                "confidence": "low",
            }
            for i, en_idx in enumerate(en_indices)
        ]
        return rows, [np.array([1.0, 0.0]) for _ in rows]

    def test_run_before_the_first_row_extends_it_back(self):
        en = self.EN[:3]
        rows, vectors = self._rows(1, 2)
        model = _StubModel({"He ran. He hid.": 0.9})

        out = _absorb_orphans(rows, vectors, en, model, en_para_indices=[0, 0, 0])

        assert out[0]["en_idx"] == 0
        assert out[0]["en_indices"] == [0, 1]
        assert out[0]["en"] == "He ran. He hid."
        assert out[0]["similarity"] == 0.9
        assert out[0]["confidence"] == "high"
        assert "en_indices" not in out[1]

    def test_run_after_the_last_row_extends_it_forward(self):
        en = self.EN[:3]
        rows, vectors = self._rows(0, 1)
        model = _StubModel({"He hid. He waited.": 0.8})

        out = _absorb_orphans(rows, vectors, en, model, en_para_indices=[0, 0, 0])

        assert "en_indices" not in out[0]
        assert out[1]["en_idx"] == 1
        assert out[1]["en_indices"] == [1, 2]
        assert out[1]["en"] == "He hid. He waited."

    def test_between_two_rows_the_better_gain_takes_the_run(self):
        en = self.EN[:3]
        rows, vectors = self._rows(0, 2)
        model = _StubModel({"He ran. He hid.": 0.2, "He hid. He waited.": 0.7})

        out = _absorb_orphans(rows, vectors, en, model, en_para_indices=[0, 0, 0])

        assert "en_indices" not in out[0]
        assert out[1]["en_idx"] == 1
        assert out[1]["en_indices"] == [1, 2]

    def test_a_row_takes_at_most_one_run(self):
        # Sentences 1-3 share a paragraph with the middle row and with no other,
        # so it is the only candidate for the run on each side of it. It takes
        # the first and the second stays unclaimed.
        rows, vectors = self._rows(0, 2, 4)
        model = _StubModel({"He hid. He waited.": 0.9, "He waited. She called.": 0.9})

        out = _absorb_orphans(
            rows, vectors, self.EN, model, en_para_indices=[0, 1, 1, 1, 2]
        )

        assert out[1]["en_indices"] == [1, 2]
        assert _covered_en(out) == {0, 1, 2, 4}

    def test_nothing_is_absorbed_across_a_paragraph(self):
        en = self.EN[:3]
        rows, vectors = self._rows(0, 2)
        model = _StubModel({})

        out = _absorb_orphans(rows, vectors, en, model, en_para_indices=[0, 1, 2])

        assert all("en_indices" not in row for row in out)


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

    def test_align_chapter_chunks_offsets_absorbed_indices(
        self, tmp_path, monkeypatch, model
    ):
        """en_indices on a 1:N row must be shifted into chapter-global indices
        along with en_idx, or a later chunk's row points into the first chunk."""
        from src import sentence_aligner
        from src.sentence_aligner import align_chapter_chunks

        monkeypatch.setattr(sentence_aligner, "_get_model", lambda: model)

        merged_source = (
            "They got along perfectly together. They would sit side by side gossiping."
        )
        merged_translation = (
            "Se llevaban perfectamente bien y se sentaban una junto a la otra a chismorrear."
        )
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, 0, self.SOURCE, self.TRANSLATION_FULL)
        self._write_chunk(chunks_dir, 1, merged_source, merged_translation)

        result = align_chapter_chunks(
            chunk_paths=sorted(str(p) for p in chunks_dir.glob("*.json")),
            project_id="test_project",
            chapter_id="chapter_test",
        )

        row = next(
            a for a in result["alignments"] if a["chunk_id"] == "chapter_test_chunk_001"
        )
        last = result["en_count"] - 1
        assert row["en_indices"] == [last - 1, last]
        assert row["en_idx"] == last - 1
        assert result["coverage"]["en_aligned"] == result["en_count"]
        assert result["gaps"] == []

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


class TestGetModel:
    """_get_model() hands the aligner the embedding server when one is configured."""

    def test_remote_embedder_when_one_is_configured(self, monkeypatch):
        from src import embed_client, sentence_aligner

        remote = object()
        asked = []
        monkeypatch.setattr(sentence_aligner, "_model", None)
        monkeypatch.setattr(
            embed_client, "remote_embedder", lambda name, load: asked.append(name) or remote
        )
        monkeypatch.setattr(
            sentence_aligner, "_load_local_model", lambda: pytest.fail("loaded the local model")
        )

        assert sentence_aligner._get_model() is remote
        assert sentence_aligner._get_model() is remote
        # Built once, for the model the aligner names.
        assert asked == [sentence_aligner.MODEL_NAME]

    def test_local_model_when_none_is(self, monkeypatch):
        from src import embed_client, sentence_aligner

        local = object()
        monkeypatch.setattr(sentence_aligner, "_model", None)
        monkeypatch.setattr(embed_client, "remote_embedder", lambda name, load: None)
        monkeypatch.setattr(sentence_aligner, "_load_local_model", lambda: local)

        assert sentence_aligner._get_model() is local
