"""Tests for the deterministic translation-difficulty scorer."""

import json

import pytest

from src.difficulty_scorer import (
    WORDFREQ_AVAILABLE,
    DifficultyMetrics,
    build_glossary_skip,
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
