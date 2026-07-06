"""Tests for the deterministic translation-difficulty scorer."""

import json

import pytest

from src.difficulty_scorer import (
    AGGREGATION_P,
    WEIGHT_LENGTH,
    WEIGHT_RARITY,
    WEIGHTS,
    WORDFREQ_AVAILABLE,
    DifficultyMetrics,
    _aggregate_difficulty,
    build_glossary_skip,
    dialogue_marker_counts,
    dialect_marker_count,
    score_book,
    score_text,
    suggest_target_size,
)
from src.models import Glossary, GlossaryTerm

requires_wordfreq = pytest.mark.skipif(
    not WORDFREQ_AVAILABLE, reason="wordfreq not installed"
)


# ---------------------------------------------------------------------------
# Sentence-length metric
# ---------------------------------------------------------------------------


def test_long_sentences_raise_weighted_length_and_score():
    short = "Cats run. Dogs bark. Birds sing. Fish swim. Kids play."
    long_text = (
        "The remarkably loquacious narrator, who never once paused for breath "
        "across the entire afternoon, continued recounting the tangled and "
        "interminable saga of his ancestors despite the growing impatience of "
        "everyone unfortunate enough to remain within earshot of the parlor."
    )
    ms = score_text(short)
    ml = score_text(long_text)
    assert ml.sentence_length_weighted > ms.sentence_length_weighted
    assert ml.length_score > ms.length_score


def test_weighted_exceeds_mean_when_tail_present():
    # Several short sentences plus one very long one: the word-weighted mean
    # must exceed the plain mean (the whole point of tail-weighting).
    text = "Go. Go. Go. Go. " + " ".join(["word"] * 60) + "."
    m = score_text(text)
    assert m.sentence_length_weighted > m.mean_sentence_length


# ---------------------------------------------------------------------------
# Lexical rarity + glossary exclusion
# ---------------------------------------------------------------------------


@requires_wordfreq
def test_glossary_excludes_proper_name_from_rarity():
    # A rare token (unknown to wordfreq ⇒ Zipf 0 ⇒ counted as rare) repeated.
    name = "Zzyzxle"
    text = (name + " walked slowly. ") * 15
    without = score_text(text)
    skip = build_glossary_skip(
        Glossary(terms=[GlossaryTerm(english=name, spanish=name)])
    )
    with_skip = score_text(text, glossary_skip=skip)
    assert without.rare_word_fraction > 0
    assert with_skip.rare_word_fraction < without.rare_word_fraction


def test_build_glossary_skip_splits_multiword_terms():
    skip = build_glossary_skip(
        Glossary(terms=[GlossaryTerm(english="Aunt Harriet", spanish="Tía Harriet")])
    )
    assert "aunt" in skip and "harriet" in skip


def test_build_glossary_skip_none_is_empty():
    assert build_glossary_skip(None) == set()


# ---------------------------------------------------------------------------
# Combination, determinism, target mapping
# ---------------------------------------------------------------------------


def test_determinism():
    text = "The quick brown fox jumped over the lazy dog near the wide river."
    assert score_text(text).to_dict() == score_text(text).to_dict()


def test_difficulty_in_unit_interval():
    text = "Some perfectly ordinary text that should land somewhere in the middle."
    m = score_text(text)
    assert 0.0 <= m.difficulty <= 1.0
    assert 0.0 <= m.length_score <= 1.0
    assert 0.0 <= m.rarity_score <= 1.0


def test_suggest_target_size_monotonic_decreasing():
    assert (
        suggest_target_size(0.0)
        > suggest_target_size(0.5)
        > suggest_target_size(1.0)
    )


def test_suggest_target_size_respects_floor():
    assert suggest_target_size(1.0) >= 100
    # Even an absurd difficulty stays clamped and valid.
    assert suggest_target_size(5.0) >= 100


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_text():
    m = score_text("")
    assert m.sentence_count == 0
    assert m.word_count == 0
    assert m.difficulty == 0.0
    assert m.suggested_target_size >= 100


def test_single_sentence():
    m = score_text("This is one simple sentence.")
    assert m.sentence_count == 1
    assert m.tokens_scored > 0


def test_metrics_roundtrip():
    m = score_text("A modest sentence for round-tripping the dataclass.")
    assert DifficultyMetrics.from_dict(m.to_dict()) == m


# ---------------------------------------------------------------------------
# Dialect density
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "ain't", "comin'", "jes'", "off'n", "young'un", "smarter'n",
        "a-thinkin'", "'twas", "'em", "reckon", "yonder", "o'", "t'other",
        "nacherel", "acrost",
    ],
)
def test_dialect_markers_counted(token):
    assert dialect_marker_count(token) == 1


@pytest.mark.parametrize(
    "token",
    [
        "don't", "doesn't", "it's", "we're", "would've", "o'clock", "ma'am",
        "needn't", "Misty's", "Grandpa's", "horses'", "girls'", "horse",
        "morning",
    ],
)
def test_standard_and_possessive_not_counted(token):
    assert dialect_marker_count(token) == 0


@pytest.mark.parametrize(
    "token",
    ["O'Brien", "O'Hara", "O'Neill", "O'Malley"],
)
def test_irish_surname_prefix_not_counted(token):
    assert dialect_marker_count(token) == 0


def test_curly_apostrophe_contractions_not_counted():
    # Typeset books use the curly apostrophe (U+2019); standard contractions
    # written that way must still be whitelisted (regression for false positives
    # that scored a clean book as wall-to-wall dialect).
    text = "I don’t think you’d say it couldn’t be done, ma’am."
    assert dialect_marker_count(text) == 0


def test_curly_apostrophe_gdrop_still_counts():
    # A g-drop written with a curly apostrophe is still dialect.
    assert dialect_marker_count("comin’") == 1


def test_closing_single_quote_not_counted_as_gdrop():
    # A word wrapped in single quotes ends in a bare apostrophe but is not a
    # g-drop; it must not be counted.
    assert dialect_marker_count("He said the word 'separate' aloud.") == 0


def test_a_prefixed_progressive_counted_once():
    # a-thinkin' matches both the a-prefix and apostrophe rules; count once.
    assert dialect_marker_count("a-thinkin'") == 1
    assert dialect_marker_count("a-walking") == 1


def test_dialect_text_scores_higher_than_clean_prose():
    # Two passages of comparable sentence length and common vocabulary; only the
    # second is eye-dialect. The dialect one must score strictly higher.
    clean = (
        "The young horse walked along the misty shore in the cool morning. "
        "He looked at the water and waited for his mother to come back."
    )
    dialect = (
        "The young'un were a-thinkin' 'twas time to go, jes' like his ma "
        "reckoned. He weren't comin' back, naw, not nohow, an' that critter "
        "knowed it."
    )
    mc = score_text(clean)
    md = score_text(dialect)
    assert mc.dialect_score == 0.0
    assert md.dialect_score > mc.dialect_score
    assert md.difficulty > mc.difficulty


def test_dialect_score_in_unit_interval():
    md = score_text("jes' a-thinkin' 'twas comin' reckon young'un ain't off'n")
    assert 0.0 <= md.dialect_score <= 1.0
    assert 0.0 <= md.difficulty <= 1.0


def test_power_mean_p1_is_weighted_mean():
    scores = {"length": 0.2, "rarity": 0.4, "dialect": 0.6, "dialogue": 0.8, "verse": 1.0}
    expected = sum(WEIGHTS[k] * scores[k] for k in scores)
    assert _aggregate_difficulty(scores, WEIGHTS, 1.0) == pytest.approx(expected)


def test_power_mean_raises_difficulty_for_single_high_signal():
    equal = {"length": 0.5, "rarity": 0.5, "dialect": 0.5, "dialogue": 0.5, "verse": 0.5}
    skewed = {"length": 1.0, "rarity": 0.0, "dialect": 0.0, "dialogue": 0.0, "verse": 0.0}
    d_equal = _aggregate_difficulty(equal, WEIGHTS, AGGREGATION_P)
    d_skewed = _aggregate_difficulty(skewed, WEIGHTS, AGGREGATION_P)
    d_skewed_p1 = _aggregate_difficulty(skewed, WEIGHTS, 1.0)
    assert d_equal == pytest.approx(0.5)
    assert d_skewed > d_skewed_p1


def test_zero_hazard_scores_leave_other_signals_unchanged():
    # With zero dialect/dialogue/verse markers, only length+rarity contribute.
    text = (
        "The narrator described the wide green valley and the slow river that "
        "wound between the hills under a pale and quiet morning sky."
    )
    m = score_text(text)
    assert m.dialect_marker_count == 0
    assert m.dialect_score == 0.0
    assert m.nested_quote_count == 0
    assert m.dialogue_score == 0.0
    assert m.verse_score == 0.0
    scores = {
        "length": m.length_score,
        "rarity": m.rarity_score,
        "dialect": 0.0,
        "dialogue": 0.0,
        "verse": 0.0,
    }
    expected = _aggregate_difficulty(scores, WEIGHTS, AGGREGATION_P)
    assert m.difficulty == pytest.approx(expected, abs=1e-4)


def test_dialect_metrics_roundtrip():
    m = score_text("He were a-comin' back, jes' like he reckoned, young'un.")
    assert DifficultyMetrics.from_dict(m.to_dict()) == m
    assert m.dialect_marker_count > 0


# ---------------------------------------------------------------------------
# Dialogue density (nested quotes)
# ---------------------------------------------------------------------------


def test_nested_quotes_raise_dialogue_score_more_than_plain_double_quotes():
    plain = '"Hello there," she said. "How are you today?"'
    nested = '"He said \'hello\' to her," she replied.'
    mp = score_text(plain)
    mn = score_text(nested)
    assert mn.nested_quote_count > mp.nested_quote_count
    assert mn.dialogue_density > mp.dialogue_density
    assert mn.dialogue_score >= mp.dialogue_score


@pytest.mark.parametrize(
    "text",
    [
        "don't stop",
        "John's book",
        "o'clock",
        "'twas the night",
        "'em went ahead",
    ],
)
def test_apostrophes_and_elisions_do_not_inflate_nested_quote_count(text):
    _, nested = dialogue_marker_counts(text)
    assert nested == 0


def test_dialogue_score_in_unit_interval():
    text = '"She cried \'help me\' loudly," he whispered.'
    m = score_text(text)
    assert 0.0 <= m.dialogue_score <= 1.0


# ---------------------------------------------------------------------------
# Verse density
# ---------------------------------------------------------------------------


def _verse_block() -> str:
    lines = [
        "The wind was a torrent of darkness among the gusty trees,",
        "The moon was a ghostly galleon tossed upon cloudy seas,",
        "The road was a ribbon of moonlight over the purple moor,",
        "And the highwayman came riding—",
        "Riding—riding—",
        "The highwayman came riding, up to the old inn-door.",
    ]
    return "\n".join(lines)


def test_verse_heavy_text_raises_verse_score_and_lowers_target():
    prose = (
        "The narrator walked along the quiet lane. "
        "Birds sang in the trees overhead. "
        "It was a pleasant afternoon."
    )
    verse = _verse_block()
    mp = score_text(prose)
    mv = score_text(verse)
    assert mv.verse_line_count > 0
    assert mv.verse_score > mp.verse_score
    assert mv.suggested_target_size < mp.suggested_target_size


# ---------------------------------------------------------------------------
# Book-level scoring + caching
# ---------------------------------------------------------------------------


def _make_project(tmp_path, chapters: dict):
    proj = tmp_path / "proj"
    (proj / "chapters").mkdir(parents=True)
    for ch_id, text in chapters.items():
        (proj / "chapters" / f"{ch_id}.txt").write_text(text, encoding="utf-8")
    return proj


def test_score_book_writes_manifest_and_scores_chapters(tmp_path):
    proj = _make_project(
        tmp_path,
        {
            "chapter_01": "Short and simple. Easy reading here. All good.",
            "chapter_02": (
                "The interminable and labyrinthine ruminations of the "
                "philosopher, unspooling across clause after subordinate "
                "clause without mercy, taxed even the most patient reader."
            ),
        },
    )
    manifest = score_book(proj)
    assert (proj / "difficulty.json").exists()
    assert len(manifest.chapters) == 2
    ids = [c.chapter_id for c in manifest.chapters]
    assert ids == ["chapter_01", "chapter_02"]
    # The dense chapter should score harder than the simple one.
    by_id = {c.chapter_id: c.metrics for c in manifest.chapters}
    assert by_id["chapter_02"].difficulty > by_id["chapter_01"].difficulty


def test_score_book_uses_cache_until_force(tmp_path):
    proj = _make_project(tmp_path, {"chapter_01": "First version of the text."})
    first = score_book(proj)
    # Mutate the source without bumping mtime forward enough to matter: a fresh
    # cache (source_mtime >= current) should be reused, returning identical data.
    cached = score_book(proj)
    assert cached.generated_at == first.generated_at
    # force=True must re-score (new generated_at timestamp).
    forced = score_book(proj, force=True)
    assert forced.generated_at != first.generated_at


def test_score_book_rescores_when_calibration_changes(tmp_path, monkeypatch):
    # Changing the algorithm/thresholds (not the source) must invalidate the
    # cache so cached numbers don't silently go stale after a re-tune.
    import src.difficulty_scorer as ds

    proj = _make_project(tmp_path, {"chapter_01": "Plain simple text right here."})
    first = score_book(proj)
    # Same calibration ⇒ cached (no new timestamp).
    assert score_book(proj).generated_at == first.generated_at
    # Different calibration ⇒ re-score.
    monkeypatch.setattr(ds, "WEIGHT_DIALECT", ds.WEIGHT_DIALECT + 0.1)
    assert score_book(proj).generated_at != first.generated_at


def test_score_book_glossary_excluded_from_chapter_rarity(tmp_path):
    name = "Zzyzxle"
    proj = _make_project(
        tmp_path, {"chapter_01": (name + " went to town. ") * 12}
    )
    # Write a glossary that should suppress the proper name's rarity.
    (proj / "glossary.json").write_text(
        json.dumps({"terms": [{"english": name, "spanish": name}]}),
        encoding="utf-8",
    )
    manifest = score_book(proj, force=True)
    ch = manifest.chapters[0].metrics
    if WORDFREQ_AVAILABLE:
        assert ch.rare_word_fraction == 0.0
