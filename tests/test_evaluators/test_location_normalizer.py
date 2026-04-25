"""
Tests for ``src.evaluators.location_normalizer``.

Covers the known ``Issue.location`` formats emitted by each evaluator and the
fan-out behavior used by the GUI.
"""

from __future__ import annotations

import pytest

from src.evaluators.location_normalizer import (
    NormalizedLocation,
    fan_out_issues,
    normalize_issue_location,
)
from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    EvalResult,
    Issue,
    IssueLevel,
)


@pytest.fixture
def chunk() -> Chunk:
    """Source and target text that span two paragraphs each.

    Target: "El perro cafe salta.\n\nLa gata duerme tranquila."
    Source: "The brown dog jumps.\n\nThe cat sleeps peacefully."
    """
    source = "The brown dog jumps.\n\nThe cat sleeps peacefully."
    target = "El perro cafe salta.\n\nLa gata duerme tranquila."
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=0,
        source_text=source,
        translated_text=target,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=2,
            word_count=7,
        ),
        status=ChunkStatus.TRANSLATED,
    )


def _issue(message: str, location: str, severity: IssueLevel = IssueLevel.WARNING) -> Issue:
    return Issue(severity=severity, message=message, location=location)


# ---------------------------------------------------------------------------
# Aggregate-style locations (no span)


def test_length_location_is_aggregate(chunk):
    issue = _issue("Translation is 50% shorter", "5 words -> 2 words")
    locs = normalize_issue_location(issue, chunk, "length")
    assert len(locs) == 1
    assert locs[0].side == "none"
    assert locs[0].char_start is None
    assert locs[0].match == ""


def test_paragraph_location_is_aggregate(chunk):
    issue = _issue("Paragraph mismatch", "2 paragraphs -> 1 paragraphs")
    locs = normalize_issue_location(issue, chunk, "paragraph")
    assert len(locs) == 1
    assert locs[0].side == "none"


# ---------------------------------------------------------------------------
# Target-side span locations: grammar / blacklist / completeness char ranges


def test_grammar_char_range(chunk):
    # "cafe" starts at char 9 in the target; grammar emits "char N-M".
    issue = _issue("Missing accent", "char 9-13")
    locs = normalize_issue_location(issue, chunk, "grammar")
    assert len(locs) == 1
    loc = locs[0]
    assert loc.side == "target"
    assert loc.char_start == 9
    assert loc.char_end == 13
    assert loc.match == "cafe"
    assert loc.paragraph_index == 0
    assert "El perro " in loc.snippet_before
    assert loc.snippet_after.startswith(" salta.")


def test_blacklist_char_single(chunk):
    issue = _issue("'gata': informal", "char 25")
    locs = normalize_issue_location(issue, chunk, "blacklist")
    assert len(locs) == 1
    loc = locs[0]
    assert loc.side == "target"
    assert loc.char_start == 25
    # Message-embedded token lets us recover length.
    assert loc.match == "gata"


def test_completeness_char_range(chunk):
    # "tranquila" starts at target offset 37; char 37-46 spans "tranquila."
    issue = _issue("Unfinished sentence", "char 37-46")
    locs = normalize_issue_location(issue, chunk, "completeness")
    assert locs[0].side == "target"
    assert locs[0].match == "tranquila"


# ---------------------------------------------------------------------------
# Dictionary: "Character position(s): ..." → one loc per position


def test_dictionary_single_position(chunk):
    # "cafe" at position 9 in target.
    issue = _issue("'cafe': not in Spanish dict (found 1 time(s))", "Character position 9")
    locs = normalize_issue_location(issue, chunk, "dictionary")
    assert len(locs) == 1
    assert locs[0].side == "target"
    assert locs[0].char_start == 9
    assert locs[0].match == "cafe"


def test_dictionary_multiple_positions_fan_out(chunk):
    # Synthetic: two positions in the target.
    issue = _issue(
        "'gata': unknown (found 2 time(s))",
        "Character positions: 25, 35",
    )
    locs = normalize_issue_location(issue, chunk, "dictionary")
    assert len(locs) == 2
    assert all(loc.side == "target" for loc in locs)
    assert locs[0].char_start == 25
    assert locs[1].char_start == 35


def test_dictionary_truncated_positions(chunk):
    issue = _issue(
        "'x': reason (found 10 time(s))",
        "Character positions: 1, 5, 9, ... (10 total)",
    )
    locs = normalize_issue_location(issue, chunk, "dictionary")
    assert len(locs) == 3  # only the 3 shown positions; "total" count is metadata.


# ---------------------------------------------------------------------------
# Glossary variants


def test_glossary_source_positions(chunk):
    issue = _issue("'dog' missing in translation", "source positions: [4, 10]")
    locs = normalize_issue_location(issue, chunk, "glossary")
    assert len(locs) == 2
    assert all(loc.side == "source" for loc in locs)
    assert locs[0].char_start == 4
    assert locs[1].char_start == 10


def test_glossary_translation_side(chunk):
    issue = _issue("Variant mismatch", "translation")
    locs = normalize_issue_location(issue, chunk, "glossary")
    assert locs[0].side == "target"
    assert locs[0].char_start is None


def test_glossary_source_side(chunk):
    issue = _issue("Missing source term", "source")
    locs = normalize_issue_location(issue, chunk, "glossary")
    assert locs[0].side == "source"


def test_glossary_mixed_location(chunk):
    issue = _issue(
        "Term used inconsistently",
        "source: [4, 10], translation: ['perro']",
    )
    locs = normalize_issue_location(issue, chunk, "glossary")
    assert all(loc.side == "source" for loc in locs)
    assert {loc.char_start for loc in locs} == {4, 10}


def test_glossary_variants_used(chunk):
    issue = _issue("Consistency", "variants used: ['perro', 'can']")
    locs = normalize_issue_location(issue, chunk, "glossary")
    assert locs[0].side == "target"
    assert locs[0].char_start is None


# ---------------------------------------------------------------------------
# Completeness / informational labels


def test_completeness_translated_text_label(chunk):
    issue = _issue("Looks complete", "translated_text")
    locs = normalize_issue_location(issue, chunk, "completeness")
    assert locs[0].side == "target"
    assert locs[0].char_start is None


def test_completeness_end_of_text(chunk):
    issue = _issue("Abrupt ending", "end of text: '...tranquila.'")
    locs = normalize_issue_location(issue, chunk, "completeness")
    assert locs[0].side == "target"


def test_completeness_special_markers(chunk):
    issue = _issue("Missing marker", "special markers")
    locs = normalize_issue_location(issue, chunk, "completeness")
    assert locs[0].side == "target"


# ---------------------------------------------------------------------------
# LLM judge / internal errors


def test_llm_judge_chunk_id(chunk):
    issue = _issue("Low fluency", chunk.id, IssueLevel.WARNING)
    locs = normalize_issue_location(issue, chunk, "llm_judge")
    assert locs[0].side == "none"
    assert locs[0].raw == chunk.id


def test_evaluator_initialization_error(chunk):
    issue = _issue("Failed to init", "evaluator_initialization", IssueLevel.ERROR)
    locs = normalize_issue_location(issue, chunk, "dictionary")
    assert locs[0].side == "none"


# ---------------------------------------------------------------------------
# Fallback + missing location


def test_unknown_location_fallback(chunk):
    issue = _issue("Mystery", "somewhere in the middle of the text")
    locs = normalize_issue_location(issue, chunk, "custom_eval")
    assert len(locs) == 1
    assert locs[0].side == "none"
    assert locs[0].raw == "somewhere in the middle of the text"


def test_empty_location_returns_empty_list(chunk):
    issue = _issue("No info", "")
    locs = normalize_issue_location(issue, chunk, "length")
    assert locs == []


# ---------------------------------------------------------------------------
# fan_out_issues


def test_fan_out_splits_multi_position(chunk):
    result = EvalResult(
        eval_name="dictionary",
        eval_version="1.0.0",
        target_id=chunk.id,
        target_type="chunk",
        passed=False,
        score=0.5,
        issues=[
            _issue("'gata': unknown (found 2 time(s))", "Character positions: 25, 35"),
            _issue("Translation is short", ""),
        ],
        metadata={"total_positions": 2, "flagged_words": 1},
    )
    normalized = fan_out_issues(result, chunk)
    # 2 locations for the first issue + 1 "no location" entry for the second.
    assert len(normalized) == 3
    assert all(n.eval_name == "dictionary" for n in normalized)
    # issue_index is preserved so feedback can target the original Issue.
    assert [n.issue_index for n in normalized] == [0, 0, 1]
    assert normalized[2].location is None
    # Metadata excerpt is curated per evaluator.
    assert "total_positions" in normalized[0].metadata_excerpt


def test_fan_out_does_not_mutate_result(chunk):
    result = EvalResult(
        eval_name="length",
        eval_version="1.0.0",
        target_id=chunk.id,
        target_type="chunk",
        passed=True,
        score=1.0,
        issues=[_issue("ok", "5 words -> 6 words", IssueLevel.INFO)],
        metadata={"ratio": 1.2},
    )
    before = result.model_copy(deep=True)
    fan_out_issues(result, chunk)
    assert result.model_dump() == before.model_dump()


def test_normalized_location_to_dict_roundtrip():
    loc = NormalizedLocation(
        raw="char 0-3",
        side="target",
        paragraph_index=0,
        char_start=0,
        char_end=3,
        snippet_before="",
        match="abc",
        snippet_after="",
    )
    data = loc.to_dict()
    assert data["side"] == "target"
    assert data["match"] == "abc"
