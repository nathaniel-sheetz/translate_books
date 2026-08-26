"""
Tests for the dictionary evaluator.
"""

import pytest
from datetime import datetime

from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    IssueLevel,
    Glossary,
    GlossaryTerm,
)
from src.evaluators.dictionary_eval import (
    DictionaryEvaluator,
    _fold_accents_preserving_enye,
)


@pytest.fixture
def evaluator():
    """Create a dictionary evaluator instance."""
    return DictionaryEvaluator()


@pytest.fixture
def base_chunk():
    """Create a basic chunk for testing."""
    return Chunk(
        id="test_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="This is a test sentence.",
        translated_text=None,  # Will be set in tests
        metadata=ChunkMetadata(
            char_start=0,
            char_end=100,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=5,
        ),
        status=ChunkStatus.PENDING,
    )


@pytest.fixture
def sample_glossary():
    """Create a sample glossary for testing."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Hogwarts",
                spanish="Hogwarts",
                term_type="place",
                context="School name, keep in English",
                alternatives=[],
            ),
            GlossaryTerm(
                english="API",
                spanish="API",
                term_type="technical",
                context="Technical term",
                alternatives=[],
            ),
        ],
        version="1.0.0",
    )


def test_all_spanish_words_pass(evaluator, base_chunk):
    """Test that valid Spanish text passes all checks."""
    base_chunk.translated_text = "Esta es una oración de prueba en español correcto."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is True
    assert len(result.issues) == 0
    assert result.score == 1.0
    assert result.metadata["english_words"] == 0
    assert result.metadata["unknown_words"] == 0


def test_english_words_flagged_as_errors(evaluator, base_chunk):
    """Test that English words in translation are flagged as errors."""
    # Mix of Spanish and English
    base_chunk.translated_text = "Esta es una sentence con some English words."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    # All English words should be detected: sentence, some, English, words
    assert result.metadata["english_words"] == 4
    assert any(issue.severity == IssueLevel.ERROR for issue in result.issues)
    # Check that at least one error mentions English
    english_errors = [i for i in result.issues if i.severity == IssueLevel.ERROR]
    assert len(english_errors) >= 1
    assert any("English" in err.message for err in english_errors)


def test_misspelled_words_flagged_as_warnings(evaluator, base_chunk):
    """Test that misspelled Spanish words are flagged as warnings."""
    # "prueba" misspelled as "preuba"
    base_chunk.translated_text = "Esta es una preuba con errores ortográficos."

    result = evaluator.evaluate(base_chunk, {})

    # Warnings don't cause failure, but issues should be present
    assert result.metadata["unknown_words"] >= 1
    # Should have warnings for unknown words
    warnings = [i for i in result.issues if i.severity == IssueLevel.WARNING]
    assert len(warnings) >= 1
    # Score should be less than perfect due to unknown words
    assert result.score < 1.0


def test_proper_nouns_handled(evaluator, base_chunk):
    """Test that proper nouns (capitalized words) are handled appropriately."""
    # "María" is a valid Spanish name
    base_chunk.translated_text = "María es una persona importante en la historia."

    result = evaluator.evaluate(base_chunk, {})

    # María should be recognized (it's in Spanish dictionaries)
    # All other words are valid Spanish
    assert result.passed is True
    assert len(result.issues) == 0


def test_glossary_terms_excluded(evaluator, base_chunk, sample_glossary):
    """Test that glossary terms are not flagged even if not in dictionaries."""
    base_chunk.translated_text = "Hogwarts es una escuela en la API mágica."

    result = evaluator.evaluate(base_chunk, {"glossary": sample_glossary})

    # "Hogwarts" and "API" are in glossary, should not be flagged
    assert result.passed is True
    assert result.metadata["glossary_words"] == 2
    assert result.metadata["english_words"] == 0
    assert result.metadata["unknown_words"] == 0


def test_numbers_ignored(evaluator, base_chunk):
    """Test that numbers are ignored in dictionary checking."""
    base_chunk.translated_text = "Había 123 personas y 45.67 kilómetros."

    result = evaluator.evaluate(base_chunk, {})

    # Numbers should be ignored
    assert result.passed is True
    assert len(result.issues) == 0


def test_single_characters_handled(evaluator, base_chunk):
    """Test that single characters are handled correctly."""
    # Spanish single-letter words: a, o, e, y
    base_chunk.translated_text = "A María y Pedro o Elena e Isabel."

    result = evaluator.evaluate(base_chunk, {})

    # Should recognize valid single-letter Spanish words
    # Note: María, Pedro, Elena, Isabel are proper names
    # May or may not be in dictionary - test should pass if no English detected
    errors = [i for i in result.issues if i.severity == IssueLevel.ERROR]
    assert len(errors) == 0  # No English words


def test_punctuation_handling(evaluator, base_chunk):
    """Test that punctuation doesn't interfere with word checking."""
    base_chunk.translated_text = "¡Hola! ¿Cómo estás? Bien, gracias."

    result = evaluator.evaluate(base_chunk, {})

    # Should extract words correctly despite punctuation
    assert result.passed is True
    assert len(result.issues) == 0


def test_accented_characters(evaluator, base_chunk):
    """Test that Spanish accented characters are handled correctly."""
    # Use common Spanish words with accents (avoid proper names)
    base_chunk.translated_text = "La canción es rápida y difícil también."

    result = evaluator.evaluate(base_chunk, {})

    # All are valid Spanish words with accents
    assert result.passed is True
    assert len(result.issues) == 0


def test_case_insensitive_by_default(evaluator, base_chunk):
    """Test that checking is case-insensitive by default."""
    base_chunk.translated_text = "HOLA hola Hola HoLa."

    result = evaluator.evaluate(base_chunk, {})

    # All variants of "hola" should be accepted
    assert result.passed is True
    assert len(result.issues) == 0


def test_case_sensitive_mode(evaluator, base_chunk):
    """Test case-sensitive mode if enabled."""
    base_chunk.translated_text = "hola es una palabra."

    # Default: case insensitive
    result = evaluator.evaluate(base_chunk, {"case_sensitive": False})
    assert result.passed is True

    # Case sensitive: should still pass (lowercase is valid)
    result2 = evaluator.evaluate(base_chunk, {"case_sensitive": True})
    assert result2.passed is True


def test_empty_translation_raises_error(evaluator, base_chunk):
    """Test that empty translation raises ValueError."""
    base_chunk.translated_text = None

    with pytest.raises(ValueError, match="has no translation"):
        evaluator.evaluate(base_chunk, {})


def test_mixed_spanish_variants(evaluator, base_chunk):
    """Test that words from both es_ES and es_MX are accepted."""
    # Use words that might differ between variants
    # Most words are shared, so just test common Spanish
    base_chunk.translated_text = "El ordenador es una computadora moderna."

    result = evaluator.evaluate(base_chunk, {})

    # Both "ordenador" (Spain) and "computadora" (Latin America) should be valid
    # because we check both dictionaries with OR logic
    assert result.passed is True


def test_character_positions_reported(evaluator, base_chunk):
    """Test that character positions are reported for flagged words."""
    base_chunk.translated_text = "Esta sentence tiene some English words aquí."

    result = evaluator.evaluate(base_chunk, {})

    # Should have issues with character positions
    assert len(result.issues) > 0
    for issue in result.issues:
        # Location should mention character position
        assert "position" in issue.location.lower()


def test_suggestions_provided(evaluator, base_chunk):
    """Test that spelling suggestions are provided for unknown words."""
    # Intentional misspelling: "prueba" -> "preuba"
    base_chunk.translated_text = "Esta es una preuba."

    result = evaluator.evaluate(base_chunk, {})

    # Should have warning with suggestion
    warnings = [i for i in result.issues if i.severity == IssueLevel.WARNING]
    assert len(warnings) >= 1

    # At least one warning should have a suggestion
    has_suggestion = any(
        w.suggestion and ("Suggestion" in w.suggestion or "prueba" in w.suggestion.lower())
        for w in warnings
    )
    assert has_suggestion


def test_repeated_words_counted_separately(evaluator, base_chunk):
    """Test that repeated words are counted at each occurrence."""
    base_chunk.translated_text = "English English English palabra."

    result = evaluator.evaluate(base_chunk, {})

    # Should report multiple positions for "English"
    assert result.metadata["english_words"] == 1  # 1 unique word
    # But flagged_instances should be 3
    assert result.metadata["flagged_instances"] == 3


def test_score_calculation(evaluator, base_chunk):
    """Test that score is calculated correctly based on error ratio."""
    # 10 words total, 2 errors
    base_chunk.translated_text = "Uno dos tres cuatro cinco error1 error2 ocho nueve diez."

    result = evaluator.evaluate(base_chunk, {})

    # Score should be approximately 0.8 (8 good / 10 total)
    # Actual score might vary based on what's in dictionary
    assert 0.0 <= result.score <= 1.0


def test_metadata_completeness(evaluator, base_chunk):
    """Test that all expected metadata fields are present."""
    base_chunk.translated_text = "Esta es una prueba con algunas palabras."

    result = evaluator.evaluate(base_chunk, {})

    # Check all expected metadata fields
    assert "total_words" in result.metadata
    assert "unique_words" in result.metadata
    assert "english_words" in result.metadata
    assert "unknown_words" in result.metadata
    assert "glossary_words" in result.metadata
    assert "flagged_instances" in result.metadata

    # Values should make sense
    assert result.metadata["total_words"] >= result.metadata["unique_words"]
    assert result.metadata["flagged_instances"] >= 0


def test_hyphenated_words(evaluator, base_chunk):
    """Test that hyphenated words are handled appropriately."""
    base_chunk.translated_text = "Es un bien-estar importante."

    result = evaluator.evaluate(base_chunk, {})

    # Hyphenated words should be tokenized
    # May or may not pass depending on whether "bien-estar" is recognized
    # At minimum, should not crash
    assert result is not None


def test_apostrophes_in_words(evaluator, base_chunk):
    """Test that apostrophes in words are handled."""
    # Spanish doesn't commonly use apostrophes, but test anyway
    base_chunk.translated_text = "La palabra l'home es catalana."

    result = evaluator.evaluate(base_chunk, {})

    # Should handle apostrophes without crashing
    assert result is not None


def test_only_english_text(evaluator, base_chunk):
    """Test translation that's entirely in English."""
    base_chunk.translated_text = "This is completely in English and not translated."

    result = evaluator.evaluate(base_chunk, {})

    assert result.passed is False
    # Most/all words should be flagged as English
    assert result.metadata["english_words"] > 0
    assert result.score < 0.5  # Very low score


def test_capitalized_proper_nouns(evaluator, base_chunk):
    """Test that capitalized proper nouns are recognized correctly."""
    # Test with Spanish country names, cities, and personal names that are
    # capitalized in the dictionary
    base_chunk.translated_text = "Inglaterra, España, Francia, Madrid, Barcelona y México son lugares importantes."

    result = evaluator.evaluate(base_chunk, {})

    # All these are valid Spanish words (capitalized proper nouns)
    assert result.passed is True
    assert len(result.issues) == 0
    assert result.metadata["unknown_words"] == 0
    assert result.score == 1.0


def test_capitalized_proper_nouns_at_sentence_start(evaluator, base_chunk):
    """Test proper nouns at the start of sentences."""
    # "Inglaterra" should be recognized even though it's capitalized
    base_chunk.translated_text = "Inglaterra es un país. Sara vive en Madrid."

    result = evaluator.evaluate(base_chunk, {})

    # "Inglaterra", "Sara", and "Madrid" are all proper nouns in the dictionary
    assert result.passed is True
    assert len(result.issues) == 0


def test_lowercase_proper_nouns_warning(evaluator, base_chunk):
    """Test that lowercase proper nouns may get warnings."""
    # Lowercase country names are not in dictionary
    base_chunk.translated_text = "inglaterra es un país hermoso."

    result = evaluator.evaluate(base_chunk, {})

    # "inglaterra" (lowercase) is not in dictionary, should be flagged
    assert result.metadata["unknown_words"] >= 1


def test_image_placeholder_tokens_not_flagged(evaluator, base_chunk):
    """[IMAGE:...] placeholder tokens must not be reported as English or unknown words."""
    base_chunk.translated_text = (
        "El niño sonrió. [IMAGE:images/c01.jpg] Luego salió."
    )

    result = evaluator.evaluate(base_chunk, {})

    # Placeholder fragments must NOT appear in issues
    flagged_words = {issue.message for issue in result.issues}
    for fragment in ("IMAGE", "jpg", "c01", "images"):
        assert not any(fragment.lower() in msg.lower() for msg in flagged_words), (
            f"Placeholder fragment '{fragment}' was flagged: {flagged_words}"
        )


def test_image_placeholder_with_description_not_flagged(evaluator, base_chunk):
    """[IMAGE:filename:description] placeholder including a description must be fully stripped."""
    base_chunk.translated_text = (
        "Primera línea. [IMAGE:images/c02.jpg:a winter scene] Segunda línea."
    )

    result = evaluator.evaluate(base_chunk, {})

    flagged_words = {issue.message for issue in result.issues}
    for fragment in ("IMAGE", "jpg", "c02", "winter", "scene"):
        assert not any(fragment.lower() in msg.lower() for msg in flagged_words), (
            f"Placeholder/description fragment '{fragment}' was flagged: {flagged_words}"
        )


def test_caption_marker_not_flagged(evaluator, base_chunk):
    """[CAPTION] is a structural marker; CAPTION must not be tokenized as unknown."""
    base_chunk.translated_text = "[CAPTION] El cordero de la cola larga."

    result = evaluator.evaluate(base_chunk, {})

    flagged_words = {issue.message for issue in result.issues}
    assert not any("caption" in msg.lower() for msg in flagged_words), (
        f"Caption marker was flagged: {flagged_words}"
    )


def test_caption_prose_is_still_checked(evaluator, base_chunk):
    """Blanking the marker must not hide real errors in the caption text."""
    base_chunk.translated_text = "[CAPTION] El xyzzyword no es una palabra."

    result = evaluator.evaluate(base_chunk, {})

    assert result.metadata["unknown_words"] >= 1
    flagged = " ".join(issue.message for issue in result.issues).lower()
    assert "xyzzyword" in flagged
    assert "caption" not in flagged


def test_glossary_plural_inflection(evaluator, base_chunk):
    """Plural forms should match singular glossary entries."""
    glossary = Glossary(
        terms=[
            GlossaryTerm(english="spider", spanish="épeira"),
        ]
    )
    base_chunk.translated_text = "Las épeiras tejen sus telas."

    result = evaluator.evaluate(base_chunk, {"glossary": glossary})

    assert result.metadata["glossary_words"] >= 1
    assert result.metadata["unknown_words"] == 0


def test_glossary_multi_word_token(evaluator, base_chunk):
    """A word that is part of a multi-word glossary term should be excluded."""
    glossary = Glossary(
        terms=[
            GlossaryTerm(english="Mother Ambroisine", spanish="la madre Ambroisine"),
        ]
    )
    base_chunk.translated_text = "Ambroisine cuidaba a los niños."

    result = evaluator.evaluate(base_chunk, {"glossary": glossary})

    assert result.metadata["glossary_words"] == 1
    assert result.metadata["unknown_words"] == 0


def test_glossary_accent_insensitive(evaluator, base_chunk):
    """Accent-folded variants should match glossary terms."""
    glossary = Glossary(
        terms=[
            GlossaryTerm(english="spider", spanish="épeira"),
        ]
    )
    base_chunk.translated_text = "La epeira es un arácnido."

    result = evaluator.evaluate(base_chunk, {"glossary": glossary})

    assert result.metadata["glossary_words"] == 1
    assert result.metadata["unknown_words"] == 0


class TestFoldAccentsPreservingEnye:
    """Regression tests for accent folding that must not strip ñ."""

    @pytest.mark.parametrize(
        "word, expected",
        [
            # ñ / Ñ must survive untouched — they are distinct letters, not
            # accented n's. This is the core regression guard: folding ñ→n would
            # let "moño" validate against the different real word "mono".
            ("moño", "moño"),
            ("niños", "niños"),
            ("Ñandú", "Ñandu"),
            ("año", "año"),
            # Vowel accents and the u-diaeresis are folded.
            ("época", "epoca"),
            ("épeira", "epeira"),
            ("pingüino", "pinguino"),
            ("corazón", "corazon"),
            ("áéíóúü", "aeiouu"),
            # No accents -> unchanged.
            ("casa", "casa"),
            ("", ""),
        ],
    )
    def test_folds_vowel_accents_but_keeps_enye(self, word, expected):
        assert _fold_accents_preserving_enye(word) == expected

    def test_enye_word_is_not_folded_to_a_different_word(self):
        """The specific regression: "moño" must NOT fold to "mono"."""
        folded = _fold_accents_preserving_enye("moño")
        assert "ñ" in folded
        assert folded != "mono"

    def test_differs_from_naive_nfd_strip_all(self):
        """Guard against reverting to the old NFD-strip-all fold that dropped ñ.

        The previous implementation stripped every combining mark, folding ñ→n
        and letting an ñ word validate against a different real word in the
        morphological fallback. This pins the divergence so a regression is loud.
        """
        import unicodedata

        def naive_strip_all(s: str) -> str:
            nfd = unicodedata.normalize("NFD", s)
            return "".join(c for c in nfd if unicodedata.category(c) != "Mn")

        # Old behavior collapsed the distinction; the fix preserves it.
        assert naive_strip_all("moño") == "mono"
        assert _fold_accents_preserving_enye("moño") == "moño"
        # Vowel-accent folding is identical between the two.
        assert naive_strip_all("época") == _fold_accents_preserving_enye("época")


class TestTokenizer:
    """The token boundary is half of this evaluator's precision.

    ``\\w`` includes ``_``, so the original pattern tokenized markdown emphasis
    together with its delimiters and reported ``_sí_`` -- a correctly spelled,
    correctly emphasized word -- as an unknown word. It was the single largest
    source of false positives the evaluator produced.
    """

    def test_markdown_emphasis_is_not_part_of_the_token(self, evaluator):
        tokens = dict(evaluator._tokenize_with_positions("Dijo _sí_ y _no_."))
        assert set(tokens) == {"Dijo", "sí", "y", "no"}
        # The offset is the inner word's real start, not the underscore's.
        assert tokens["sí"] == 6

    def test_underscored_word_is_no_longer_flagged(self, evaluator, base_chunk):
        base_chunk.translated_text = "Dijo _sí_ y luego _usted_ se marchó."
        result = evaluator.evaluate(base_chunk, {})
        assert result.metadata["unknown_words"] == 0
        assert result.issues == []
        # The same text under the old token boundary produced two findings.
        assert "_sí_" not in [w for w, _ in
                              evaluator._tokenize_with_positions(base_chunk.translated_text)]

    def test_internal_apostrophe_keeps_the_word_whole(self, evaluator):
        tokens = [w for w, _ in evaluator._tokenize_with_positions("d'Artagnan llegó")]
        assert tokens == ["d'Artagnan", "llegó"]

    @pytest.mark.parametrize("apostrophe", ["'", "’", "ʼ"])
    def test_every_apostrophe_shape_keeps_the_word_whole(self, evaluator, apostrophe):
        """Typeset sources carry ’, not the ASCII quote, and a hand-edited
        chunk can reintroduce one after clean_translation_text has run. Splitting
        it drops the "d" -- _is_special_case discards single characters -- and
        leaves "Artagnan" unable to match its glossary entry.
        """
        word = f"d{apostrophe}Artagnan"
        tokens = [w for w, _ in evaluator._tokenize_with_positions(f"{word} llegó")]
        assert tokens == [word, "llegó"]

    def test_hyphen_splits_deliberately(self, evaluator):
        """Not a regression: the character class never held a hyphen.

        Splitting is the safe behavior -- "bien" and "amado" are both real
        words, while hunspell has no entry for the compound.
        """
        tokens = [w for w, _ in evaluator._tokenize_with_positions("bien-amado")]
        assert tokens == ["bien", "amado"]

    def test_digits_never_become_tokens(self, evaluator):
        # "y" is a real one-letter Spanish word and stays a token; the numbers
        # do not become tokens at all.
        tokens = [w for w, _ in evaluator._tokenize_with_positions("3.5 y 1998")]
        assert tokens == ["y"]
        # And a digit glued to a word does not drag the word out of shape.
        tokens = [w for w, _ in evaluator._tokenize_with_positions("mesa_98")]
        assert tokens == ["mesa"]

    def test_footnote_marker_is_not_flagged(self, evaluator, base_chunk):
        """Without blanking, "FOOTNOTE" is reported once per footnote in the book."""
        base_chunk.translated_text = "Dijo que sí.[FOOTNOTE:3] Y se marchó."
        result = evaluator.evaluate(base_chunk, {})
        assert result.metadata["unknown_words"] == 0
        assert "FOOTNOTE" not in " ".join(i.message for i in result.issues)

    def test_footnote_blanking_preserves_reported_offsets(self, evaluator, base_chunk):
        """A word after a footnote marker must still report its true position."""
        text = "Dijo que sí.[FOOTNOTE:3] Zzzqqq marchó."
        base_chunk.translated_text = text
        result = evaluator.evaluate(base_chunk, {})
        issue = next(i for i in result.issues if "Zzzqqq" in i.message)
        assert str(text.index("Zzzqqq")) in issue.location


class TestSpanishMorphology:
    """Productive derivations hunspell does not list.

    A word whose base form is in the dictionary is not a misspelling, so there
    is nothing to rank here -- unlike the proper-noun bucket, which no feature
    separates. Each case below names the derivation being undone.
    """

    @pytest.mark.parametrize(
        "word",
        [
            # -- the keystone: the accent lives on the DICTIONARY side. The stem
            # a suffix leaves behind is unaccented ("monton"), and only the base
            # it came from carries the accent ("montón").
            "montoncito", "arbolito", "ratoncito", "jardincito", "camisoncito",
            "cojincito", "rinconcito", "tazoncitos", "saloncito", "apretoncito",
            "camioncitos", "angelita", "rapidito",
            # -- -cillo/-cilla, and the epenthetic -ec- forms
            "pastorcillo", "pastorcillos", "piedrecillas", "nubecilla",
            # -- orthographic alternations: c -> qu and z -> c before the suffix
            "banquito", "flaquito", "barquitos", "muñequita", "naricita",
            # -- superlatives (accent required; see TestStillFlagged)
            "elegantísimo", "elegantísimas", "riquísimo",
            # -- -mente adverbs
            "juguetonamente", "inconfundiblemente",
            # -- the -monos contraction, which deletes the verb's own -s
            "vámonos", "Vámonos", "marchémonos",
            # -- regular plurals whose singular is in the dictionary
            "cacareos",
        ],
    )
    def test_derived_forms_are_accepted(self, evaluator, word):
        assert evaluator._check_spanish_word(word)

    def test_vamonos_is_the_most_flagged_real_word_in_the_corpus(self, evaluator):
        """"vámonos" = "vamos" + "nos" with the verb's -s deleted.

        Nothing that only strips a suffix recovers it: [:-5] leaves "vá" and
        [:-3] leaves "vámo". The -s has to be put back, and the length guard has
        to apply to that reconstruction rather than to the bare stem -- guarding
        the stem is exactly what used to kill this word.
        """
        assert evaluator._check_spanish_word("vámonos")
        assert evaluator._check_spanish_word("Vámonos")

    def test_accent_insensitive_lookup_restores_the_dictionary_accent(self, evaluator):
        """The direction that matters: candidate unaccented, dictionary accented."""
        assert not evaluator._is_valid("monton")
        assert evaluator._accent_insensitive_valid("monton")
        assert not evaluator._is_valid("arbol")
        assert evaluator._accent_insensitive_valid("arbol")

    def test_accent_folding_still_preserves_enye(self, evaluator):
        """ñ is a letter, not an accent: "nino" must not reach "niño"."""
        assert evaluator._is_valid("niño")
        assert not evaluator._accent_insensitive_valid("nino")

    def test_apply_morphology_false_turns_the_fallback_off(self, evaluator, base_chunk):
        """The off-switch the replay harness scores against.

        Mirrors grammar_eval's apply_default_ignores: before/after has to be
        measurable rather than asserted.
        """
        base_chunk.translated_text = "Vio un montoncito de piedras."
        on = evaluator.evaluate(base_chunk, {})
        off = evaluator.evaluate(base_chunk, {"apply_morphology": False})
        assert on.metadata["unknown_words"] == 0
        assert off.metadata["unknown_words"] == 1

    def test_memoized_lookups_agree_with_the_dictionaries(self, evaluator):
        first = evaluator._is_valid("casa")
        second = evaluator._is_valid("casa")
        assert first is second is True
        assert evaluator._valid_cache["casa"] is True
        # suggest() is the expensive call and only runs on the fallback path.
        assert evaluator._suggest_cached("monton") is evaluator._suggest_cached("monton")


class TestStillFlagged:
    """The bound on the morphology fix: real defects it must not swallow.

    Every word here was confirmed against the live es_ES/es_MX dictionaries.
    They stay flagged for one of two reasons -- either no pass strips a suffix
    they end in (so the accent-insensitive lookup is never reached), or the pass
    that would match requires a written accent they lack.
    """

    @pytest.mark.parametrize(
        "word, why",
        [
            # No suffix any pass strips, so _accent_insensitive_valid is never
            # called on them. This is the constraint that keeps the fix safe:
            # a global accent-insensitive lookup WOULD validate all four
            # ("nívea", "razón", "también", "María" are all real words).
            ("nivea", "genuine typo the evaluator has actually caught"),
            ("razon", "missing accent"),
            ("tambien", "missing accent"),
            ("Maria", "missing accent"),
            # No -s/-es strip reaches a valid stem.
            ("petorales", "genuine typo"),
            ("princesca", "genuine typo"),
            ("transmutiría", "genuine typo"),
            ("rehíciron", "genuine typo"),
            # Accent on the wrong syllable: "vámonos" is right, this is not, and
            # endswith("monos") is false for "vamonós".
            ("vamonós", "accent on the wrong syllable"),
            # The superlative suffix carries the written í.
            ("grandisimo", "superlative missing its accent"),
            # The adverb inherits the adjective's accent, which is why the
            # -mente pass uses a strict lookup and not the folding one.
            ("rapidamente", "adverb missing the base adjective's accent"),
        ],
    )
    def test_genuine_defects_stay_flagged(self, evaluator, word, why):
        assert not evaluator._check_spanish_word(word), why

    def test_the_global_fold_that_is_deliberately_not_applied(self, evaluator):
        """Pins *why* these stay flagged, so a future widening is loud.

        Each of these validates under an accent-insensitive lookup. They survive
        only because no suffix pass ever hands them to one.
        """
        for word in ("nivea", "razon", "tambien", "maria"):
            assert evaluator._accent_insensitive_valid(word), word
            assert not evaluator._check_spanish_word(word), word

    @pytest.mark.parametrize(
        "word",
        ["razons", "niveas", "cancions", "tambiens"],
    )
    def test_a_plural_s_does_not_launder_an_accent_typo(self, evaluator, word):
        """The plural pass must not restore accents.

        Each of these is a pinned typo from
        ``test_genuine_defects_stay_flagged`` wearing a trailing -s. While Pass E
        called ``_accent_insensitive_valid`` they were all accepted, which
        defeated the very words this evaluator is best at catching.
        """
        assert not evaluator._check_spanish_word(word)

    def test_the_plural_pass_still_earns_its_place(self, evaluator):
        """Closing the hole above cost no legitimate acceptance: a regular
        plural absent from hunspell is still recovered from its singular."""
        assert not evaluator._is_valid("cacareos")
        assert evaluator._check_spanish_word("cacareos")

    @pytest.mark.parametrize("stem", ["hill", "cos", "cuart", "catarina"])
    def test_a_case_only_suggestion_is_not_an_accent_difference(self, evaluator, stem):
        """hunspell offers a capitalized proper noun for a lowercase stem
        ("hill" -> "Hill"). That is not the vowel-accent difference this method
        documents, and honouring it let English words validate as Spanish."""
        assert not evaluator._accent_insensitive_valid(stem)

    @pytest.mark.parametrize("word", ["algomonos", "casimonos"])
    def test_monos_is_not_a_clitic_suffix(self, evaluator, word):
        """With "monos" in the clitic table any unknown *monos word whose first
        three-or-more characters spelled a real word was accepted."""
        assert not evaluator._check_spanish_word(word)

    @pytest.mark.parametrize(
        "word",
        ["vámonos", "Vámonos", "marchémonos", "démonos", "pongámonos",
         "vayámonos", "sentémonos", "quedémonos"],
    )
    def test_dropping_monos_costs_no_real_contraction(self, evaluator, word):
        """Pass C reconstructs every one of these by restoring the verb's
        deleted -s, which is why the clitic entry caught nothing of its own."""
        assert evaluator._check_spanish_word(word)

    @pytest.mark.parametrize(
        "word",
        ["vamonos", "marchemonos", "sentemonos", "quedemonos",
         "vayamonos", "levantemonos", "abracemonos", "contentemonos"],
    )
    def test_an_unaccented_monos_form_is_a_misspelling(self, evaluator, word):
        """Pass C put the verb's -s back and then folded, so every one of these
        reached "vamos"/"quedemos"/"demos" and validated. The enclitic makes the
        real form esdrújula, which Spanish always writes with the accent -- so
        an unaccented -monos word is the contraction misspelled, and this was
        the one morphological pass that laundered a missing accent."""
        assert not evaluator._check_spanish_word(word)

    def test_the_diaeresis_is_not_a_stress_accent(self, evaluator):
        """Pins why the guard names the five acute vowels instead of asking
        whether the word survives accent folding. "apacigüemonos" is missing
        its é, but its ü makes it differ from its own folded form, so a
        fold-based guard would wave it straight through to "apacigüemos"."""
        assert not evaluator._check_spanish_word("apacigüemonos")

    def test_known_false_negative_left_in_place(self, evaluator):
        """Documented, not fixed: the clitic pass over-accepts.

        "darselo" is a genuine typo -- Spanish writes "dárselo" -- but stripping
        the cluster "selo" leaves the valid infinitive "dar". Same for "ninos",
        where stripping the vosotros enclitic "-os" leaves the real word "nin".
        Correcting these *adds* findings, which is a different change with a
        different risk profile; this test records the behavior so the gap is not
        rediscovered from scratch.

        "demonos" is the same gap reached by a different road, and it is why
        the -monos accent guard is not a complete fix for that family: Pass C
        now refuses it, but Pass D still strips the plain "nos" and lands on
        the real noun "demo". Only the forms whose stem is *not* a word --
        "vamonos", "quedemonos" and the rest -- are actually caught.
        """
        assert evaluator._check_spanish_word("darselo")
        assert evaluator._check_spanish_word("ninos")
        assert evaluator._check_spanish_word("demonos")
