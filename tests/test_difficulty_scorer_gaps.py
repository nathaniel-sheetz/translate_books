"""Coverage gap tests for src/difficulty_scorer — paths not reached by
test_difficulty_scorer.py.

Gaps addressed:
  - calibration() return value
  - _linear_score degenerate (hard <= easy) branch
  - ChapterDifficulty.to_dict / from_dict round-trip
  - DifficultyManifest.to_dict / from_dict round-trip
  - manifest_path()
  - _load_manifest with corrupt JSON returns None
  - score_book with no chapters/ dir (falls back to load_clean_source_text)
  - score_book with stale cache (source_mtime newer) triggers re-score
  - suggest_target_size edge values (0.0, 1.0) and custom targets
  - _norm_apos modifier-letter apostrophe (U+02BC) second normalization branch
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.difficulty_scorer import (
    ChapterDifficulty,
    DifficultyManifest,
    DifficultyMetrics,
    _linear_score,
    _load_manifest,
    _norm_apos,
    calibration,
    dialect_marker_count,
    manifest_path,
    score_book,
    score_text,
    suggest_target_size,
)


# ---------------------------------------------------------------------------
# calibration()
# ---------------------------------------------------------------------------


def test_calibration_returns_all_expected_keys():
    cal = calibration()
    for key in (
        "length_easy", "length_hard", "rare_zipf",
        "rarity_easy", "rarity_hard",
        "dialect_easy", "dialect_hard",
        "weight_length", "weight_rarity", "weight_dialect",
        "target_easy", "target_hard",
        "wordfreq_available",
    ):
        assert key in cal, f"missing key: {key}"


def test_calibration_weights_are_positive():
    cal = calibration()
    assert 0 < cal["weight_length"] <= 1
    assert 0 < cal["weight_rarity"] <= 1
    assert 0 < cal["weight_dialect"]


# ---------------------------------------------------------------------------
# _linear_score degenerate branch
# ---------------------------------------------------------------------------


def test_linear_score_degenerate_hard_equals_easy():
    # When hard <= easy the function must return 0.0 without dividing by zero.
    assert _linear_score(50.0, easy=10.0, hard=10.0) == 0.0
    assert _linear_score(50.0, easy=10.0, hard=5.0) == 0.0


def test_linear_score_clamps_below_zero():
    # Value well below easy should clamp to 0.
    assert _linear_score(0.0, easy=18.0, hard=32.0) == 0.0


def test_linear_score_clamps_above_one():
    # Value well above hard should clamp to 1.
    assert _linear_score(100.0, easy=18.0, hard=32.0) == 1.0


# ---------------------------------------------------------------------------
# ChapterDifficulty serialisation
# ---------------------------------------------------------------------------


def test_chapter_difficulty_to_dict_and_from_dict_roundtrip():
    m = score_text("A moderate sentence with several ordinary words in it.")
    cd = ChapterDifficulty(chapter_id="chapter_03", metrics=m)
    d = cd.to_dict()
    assert d["chapter_id"] == "chapter_03"
    assert "metrics" in d

    cd2 = ChapterDifficulty.from_dict(d)
    assert cd2.chapter_id == "chapter_03"
    assert cd2.metrics == m


def test_chapter_difficulty_from_dict_handles_missing_keys():
    cd = ChapterDifficulty.from_dict({})
    assert cd.chapter_id == ""
    assert cd.metrics.difficulty == 0.0


# ---------------------------------------------------------------------------
# DifficultyManifest serialisation
# ---------------------------------------------------------------------------


def test_manifest_to_dict_and_from_dict_roundtrip():
    m = score_text("Some text for the manifest round-trip test.")
    cd = ChapterDifficulty(chapter_id="ch_01", metrics=m)
    manifest = DifficultyManifest(
        generated_at="2026-01-01T00:00:00+00:00",
        book=m,
        chapters=[cd],
        source_mtime=12345.0,
    )
    d = manifest.to_dict()
    assert d["generated_at"] == "2026-01-01T00:00:00+00:00"
    assert d["source_mtime"] == 12345.0
    assert len(d["chapters"]) == 1

    manifest2 = DifficultyManifest.from_dict(d)
    assert manifest2.generated_at == manifest.generated_at
    assert manifest2.source_mtime == 12345.0
    assert len(manifest2.chapters) == 1
    assert manifest2.chapters[0].chapter_id == "ch_01"


def test_manifest_from_dict_empty_input():
    m = DifficultyManifest.from_dict({})
    assert m.generated_at == ""
    assert m.source_mtime is None
    assert m.chapters == []


# ---------------------------------------------------------------------------
# manifest_path()
# ---------------------------------------------------------------------------


def test_manifest_path_returns_correct_location(tmp_path):
    p = manifest_path(tmp_path / "myproject")
    assert p.name == "difficulty.json"
    assert p.parent == tmp_path / "myproject"


# ---------------------------------------------------------------------------
# _load_manifest corner cases
# ---------------------------------------------------------------------------


def test_load_manifest_returns_none_for_nonexistent_file(tmp_path):
    assert _load_manifest(tmp_path / "no-such-file.json") is None


def test_load_manifest_returns_none_for_corrupt_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not: valid json{{", encoding="utf-8")
    result = _load_manifest(bad)
    assert result is None


def test_load_manifest_returns_manifest_for_valid_file(tmp_path):
    valid = tmp_path / "difficulty.json"
    data = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "source_mtime": 0.0,
        "calibration": {},
        "book": {},
        "chapters": [],
    }
    valid.write_text(json.dumps(data), encoding="utf-8")
    result = _load_manifest(valid)
    assert result is not None
    assert result.generated_at == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# suggest_target_size edge values and custom targets
# ---------------------------------------------------------------------------


def test_suggest_target_size_at_zero_returns_easy_target():
    from src.difficulty_scorer import TARGET_EASY
    assert suggest_target_size(0.0) == TARGET_EASY


def test_suggest_target_size_at_one_returns_hard_target():
    from src.difficulty_scorer import TARGET_HARD
    assert suggest_target_size(1.0) == TARGET_HARD


def test_suggest_target_size_custom_targets():
    result = suggest_target_size(0.5, easy_target=2000, hard_target=1000)
    assert result == 1500


def test_suggest_target_size_clamps_negative_difficulty():
    # Negative difficulty should be treated as 0.0.
    from src.difficulty_scorer import TARGET_EASY
    assert suggest_target_size(-1.0) == TARGET_EASY


# ---------------------------------------------------------------------------
# score_book — no chapters/ directory (fallback to whole-book source)
# ---------------------------------------------------------------------------


def test_score_book_no_chapters_dir_returns_manifest(tmp_path):
    # A project with only source.txt and no chapters/ dir.
    proj = tmp_path / "proj_no_chapters"
    proj.mkdir()
    (proj / "source.txt").write_text(
        "Simple test source. Easy reading. Good vibes.", encoding="utf-8"
    )
    manifest = score_book(proj)
    # No per-chapter breakdown, but book metrics should still be valid.
    assert 0.0 <= manifest.book.difficulty <= 1.0
    # No chapters (chapters/ dir absent).
    assert manifest.chapters == []


# ---------------------------------------------------------------------------
# score_book — stale cache triggers re-score
# ---------------------------------------------------------------------------


def test_score_book_stale_cache_rescores(tmp_path):
    """Cache with source_mtime OLDER than actual files must be discarded."""
    proj = tmp_path / "proj_stale"
    (proj / "chapters").mkdir(parents=True)
    ch = proj / "chapters" / "chapter_01.txt"
    ch.write_text("Original text here. Simple and clean.", encoding="utf-8")

    # Score once — populates cache.
    first = score_book(proj)
    cache_file = proj / "difficulty.json"
    assert cache_file.exists()

    # Manually backdate the cached source_mtime so it is older than the file.
    cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
    cached_data["source_mtime"] = 0.0  # epoch — always stale
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")

    # Without force, the stale cache should be discarded and a fresh score generated.
    second = score_book(proj)
    assert second.generated_at != first.generated_at


# ---------------------------------------------------------------------------
# _norm_apos — modifier-letter apostrophe (U+02BC) branch
# ---------------------------------------------------------------------------


def test_norm_apos_normalizes_left_curly_apostrophe():
    # U+2018 (left single quotation mark, ') is the opening-quote character in
    # typeset books. _norm_apos must collapse it to a straight apostrophe so
    # a leading elision like ‘em (typed with a left curly) is recognized.
    left_apos = "‘"
    assert _norm_apos(left_apos + "em") == "'em"
    assert _norm_apos("comin" + left_apos) == "comin'"


def test_left_curly_em_counted_as_leading_elision():
    # '‘em' must be recognized as the dialect elision 'em even when the
    # opening apostrophe is a left curly quote (as found in typeset books).
    assert dialect_marker_count("‘em went ahead") == 1


def test_there_ll_not_counted_as_dialect():
    # there'll, where'll, who'll, what'll are standard English contractions and
    # must not be counted as dialect markers.
    for word in ("there'll", "where'll", "who'll", "what'll"):
        assert dialect_marker_count(word) == 0, f"{word!r} should not be dialect"


def test_norm_apos_normalizes_modifier_letter_apostrophe():
    # U+02BC (modifier letter apostrophe, ʼ) is the second replace branch in
    # _norm_apos. It must be collapsed to a straight apostrophe so whitelist
    # comparisons (STANDARD_CONTRACTIONS, _LEADING_ELISIONS) stay consistent
    # even if a source file uses that rarer Unicode codepoint.
    modifier_apos = "ʼ"
    assert _norm_apos("comin" + modifier_apos) == "comin'"
    assert _norm_apos("don" + modifier_apos + "t") == "don't"
    # Plain text without either apostrophe variant is returned unchanged.
    assert _norm_apos("hello") == "hello"


# ---------------------------------------------------------------------------
# dialect_marker_count — edge cases not covered by test_difficulty_scorer.py
# ---------------------------------------------------------------------------


def test_leading_apostrophe_not_in_elisions_not_counted():
    # A leading apostrophe that does NOT match _LEADING_ELISIONS (e.g. an
    # opening single-quote before a capitalized word) must not be counted.
    assert dialect_marker_count("He quoted 'Evening is nigh' aloud.") == 0
    assert dialect_marker_count("She read 'Winter is coming' to herself.") == 0


def test_a_prefix_non_breaking_hyphen_counted():
    # _A_PREFIX_RE includes the non-breaking hyphen (U+2011 ‑) as an
    # alternative to the plain hyphen. Verify the pattern fires on it.
    assert dialect_marker_count("a‑thinkin'") == 1
    assert dialect_marker_count("a‑walkin") == 1


def test_score_book_compound_cache_invalidation(tmp_path, monkeypatch):
    # When BOTH source_mtime is stale AND calibration changed, the cache must
    # still be discarded. Exercises the compound `fresh and cached.calibration
    # == calibration()` branch where fresh=True but calibration diverges.
    import json as _json
    import src.difficulty_scorer as ds

    src_dir = tmp_path / "chapters"
    src_dir.mkdir()
    (src_dir / "chapter_01.txt").write_text("Plain simple text.", encoding="utf-8")
    proj = tmp_path

    first = score_book(proj)

    # Re-write cache with a stale source_mtime but an old calibration snapshot
    cache_path = proj / "difficulty.json"
    cached_data = _json.loads(cache_path.read_text(encoding="utf-8"))
    cached_data["source_mtime"] = first.source_mtime  # still fresh
    cached_data["calibration"]["weight_dialect"] = 0.0  # stale calibration
    cache_path.write_text(_json.dumps(cached_data), encoding="utf-8")

    # Cache appears mtime-fresh but calibration differs → must re-score.
    second = score_book(proj)
    assert second.generated_at != first.generated_at
