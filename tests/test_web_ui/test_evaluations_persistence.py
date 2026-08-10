"""Tests for web_ui/evaluations.py persistence helpers."""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from src.models import EvalResult, Issue, IssueLevel
from web_ui.evaluations import (
    REVIEW_TYPES,
    append_feedback,
    chapter_id_from_chunk_id,
    empty_type_counts,
    load_chapter_type_counts,
    load_chunk_evaluation,
    load_project_summary,
    load_project_type_counts,
    mark_evaluation_stale,
    merge_judge_result,
    merge_llm_judge_result,
    save_chunk_evaluation,
)


def _make_result(name: str = "length", passed: bool = True) -> EvalResult:
    return EvalResult(
        eval_name=name,
        eval_version="1.0.0",
        target_id="ch01_chunk_001",
        target_type="chunk",
        passed=passed,
        score=0.9 if passed else 0.3,
        issues=[
            Issue(
                severity=IssueLevel.WARNING,
                message="Length looks short",
                location="translation",
                suggestion="Check for omissions",
            )
        ],
        metadata={"ratio": 1.15},
    )


def _make_aggregated() -> dict:
    return {
        "total_issues": 1,
        "issues_by_severity": {"error": 0, "warning": 1, "info": 0},
        "issues_by_evaluator": {"length": 1},
        "evaluators_run": ["length"],
        "overall_passed": True,
    }


def _make_normalized_issue() -> NormalizedIssue:
    return NormalizedIssue(
        eval_name="length",
        eval_version="1.0.0",
        issue_index=0,
        severity="warning",
        message="Length looks short",
        suggestion="Check for omissions",
        location=NormalizedLocation(raw="translation", side="translation"),
        metadata_excerpt={"ratio": 1.15},
    )


def test_save_and_load_chunk_evaluation(tmp_path):
    chunk_id = "ch01_chunk_001"
    results = [_make_result()]
    aggregated = _make_aggregated()
    issues = [_make_normalized_issue()]

    path = save_chunk_evaluation(
        tmp_path, chunk_id, results, aggregated, issues,
    )

    assert path.exists()
    assert path == tmp_path / "evaluations" / f"{chunk_id}.json"

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded is not None
    assert loaded["chunk_id"] == chunk_id
    assert loaded["aggregated"] == aggregated
    assert loaded["enabled_evals"] == ["length"]
    assert len(loaded["results"]) == 1
    assert loaded["results"][0]["eval_name"] == "length"
    assert len(loaded["normalized_issues"]) == 1
    assert loaded["normalized_issues"][0]["eval_name"] == "length"
    assert loaded["llm_judge"] is None


def test_save_overwrites_previous(tmp_path):
    chunk_id = "ch01_chunk_001"
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result(passed=False)],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result(passed=True)],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded["results"][0]["passed"] is True


def test_load_missing_chunk_returns_none(tmp_path):
    assert load_chunk_evaluation(tmp_path, "does_not_exist") is None


def test_load_malformed_file_returns_none(tmp_path):
    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir()
    (eval_dir / "bad.json").write_text("not valid json{{{", encoding="utf-8")
    assert load_chunk_evaluation(tmp_path, "bad") is None


def test_merge_llm_judge_preserves_coded_results(tmp_path):
    chunk_id = "ch01_chunk_001"
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result()],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    judge = {"overall_score": 4.2, "notes": "Looks good"}
    merge_llm_judge_result(tmp_path, chunk_id, judge)

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded["llm_judge"] == judge
    assert "llm_judge_at" in loaded
    assert len(loaded["results"]) == 1
    assert loaded["aggregated"]["total_issues"] == 1


def test_merge_llm_judge_creates_shell_if_missing(tmp_path):
    chunk_id = "ch01_chunk_002"
    judge = {"overall_score": 3.0}
    merge_llm_judge_result(tmp_path, chunk_id, judge)

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded is not None
    assert loaded["llm_judge"] == judge
    assert loaded["results"] == []
    assert loaded["enabled_evals"] == []


def test_append_feedback_creates_jsonl(tmp_path):
    chunk_id = "ch01_chunk_001"
    path = append_feedback(
        tmp_path,
        chunk_id,
        "length",
        0,
        "false_positive",
        message="Length looks short",
        note="Actually correct",
    )
    assert path.name == "_feedback.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["chunk_id"] == chunk_id
    assert record["eval_name"] == "length"
    assert record["feedback_type"] == "false_positive"
    assert record["note"] == "Actually correct"
    assert "ts" in record


def test_append_feedback_appends_multiple(tmp_path):
    append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "false_positive")
    append_feedback(tmp_path, "ch01_chunk_001", "length", 1, "bad_message")
    append_feedback(tmp_path, "ch01_chunk_002", "glossary", 0, "missing_context_gap")
    path = tmp_path / "evaluations" / "_feedback.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_append_feedback_rejects_unknown_type(tmp_path):
    with pytest.raises(ValueError, match="Unknown feedback_type"):
        append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "bogus")


def test_append_feedback_accepts_resolved(tmp_path):
    # The reader's Review Mode adds a "resolved" ("real error, handled") label
    # alongside the three quality labels.
    path = append_feedback(tmp_path, "ch01_chunk_001", "blacklist", 2, "resolved")
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["feedback_type"] == "resolved"
    assert record["eval_name"] == "blacklist"
    assert record["issue_index"] == 2


def test_load_project_summary_empty(tmp_path):
    assert load_project_summary(tmp_path) == {}


def test_load_project_summary_aggregates(tmp_path):
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_001",
        [_make_result(passed=False)],
        {
            "total_issues": 3,
            "issues_by_severity": {"error": 1, "warning": 2, "info": 0},
            "issues_by_evaluator": {"length": 3},
            "evaluators_run": ["length"],
            "overall_passed": False,
        },
        [_make_normalized_issue()],
    )
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_002",
        [_make_result()],
        {
            "total_issues": 0,
            "issues_by_severity": {"error": 0, "warning": 0, "info": 0},
            "issues_by_evaluator": {},
            "evaluators_run": ["length"],
            "overall_passed": True,
        },
        [],
    )

    summary = load_project_summary(tmp_path)
    assert set(summary.keys()) == {"ch01_chunk_001", "ch01_chunk_002"}
    assert summary["ch01_chunk_001"] == {
        "errors": 1,
        "warnings": 2,
        "info": 0,
        "total": 3,
    }
    assert summary["ch01_chunk_002"]["total"] == 0


def test_load_project_summary_ignores_feedback_file(tmp_path):
    append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "false_positive")
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_001",
        [_make_result()],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    summary = load_project_summary(tmp_path)
    assert list(summary.keys()) == ["ch01_chunk_001"]


def test_load_project_summary_stale_suppresses_judge_counts(tmp_path):
    from web_ui.evaluations import mark_evaluation_stale, merge_judge_result

    merge_judge_result(
        tmp_path,
        "ch01_chunk_001",
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [{"severity": "error", "message": "bad", "location": "x"}],
        },
    )
    mark_evaluation_stale(tmp_path, "ch01_chunk_001", "text edited")

    summary = load_project_summary(tmp_path)
    assert summary["ch01_chunk_001"] == {
        "errors": 0,
        "warnings": 0,
        "info": 0,
        "total": 0,
        "stale": 1,
    }


def test_evaluate_and_persist_preserves_stale_when_judges_kept(tmp_path):
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from web_ui.evaluations import evaluate_and_persist_chunk, mark_evaluation_stale, merge_judge_result

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="ch01",
        position=0,
        source_text="Hello",
        translated_text="Hola",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=5,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=1,
        ),
        status=ChunkStatus.TRANSLATED,
    )
    merge_judge_result(
        tmp_path,
        chunk.id,
        "dialogue",
        {"eval_name": "dialogue", "issues": [{"severity": "error", "message": "m"}]},
    )
    mark_evaluation_stale(tmp_path, chunk.id, "edited by apply")

    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert payload["stale"] is True
    assert payload["stale_reason"] == "edited by apply"
    assert "dialogue" in payload["judges"]


def test_evaluate_and_persist_return_includes_stale_fields(tmp_path):
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from web_ui.evaluations import evaluate_and_persist_chunk, mark_evaluation_stale, merge_judge_result

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="ch01",
        position=0,
        source_text="Hello",
        translated_text="Hola",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=5,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=1,
        ),
        status=ChunkStatus.TRANSLATED,
    )
    merge_judge_result(
        tmp_path,
        chunk.id,
        "dialogue",
        {"eval_name": "dialogue", "issues": [{"severity": "error", "message": "m"}]},
    )
    mark_evaluation_stale(tmp_path, chunk.id, "edited by apply")

    result = evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    assert result["stale"] is True
    assert result["stale_reason"] == "edited by apply"


# ── Per-chapter review-category counts ───────────────────────────────────────
#
# The chapter list badges flags per chapter and the home card badges them per
# book. Both read the same walk: load_chapter_type_counts does the work and
# load_project_type_counts sums it, so the two views cannot disagree.


def _target_issue(eval_name: str, issue_index: int = 0) -> NormalizedIssue:
    return NormalizedIssue(
        eval_name=eval_name,
        eval_version="1.0.0",
        issue_index=issue_index,
        severity="error",
        message="flagged",
        suggestion="reconsider",
        location=NormalizedLocation(
            raw="char 0-3", side="target", char_start=0, char_end=3, match="abc",
        ),
    )


def test_chapter_type_counts_bucket_chunks_by_chapter(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_001", results=[], aggregated={},
                          normalized_issues=[_target_issue("grammar")])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])

    by_chapter = load_chapter_type_counts(tmp_path)
    assert set(by_chapter) == {"chapter_01", "chapter_02"}
    assert by_chapter["chapter_01"]["blacklist"] == 1
    assert by_chapter["chapter_01"]["grammar"] == 1
    assert by_chapter["chapter_02"]["blacklist"] == 1
    # Every category is present so the template and the JS re-sum can index freely.
    assert set(by_chapter["chapter_02"]) == set(REVIEW_TYPES)


def test_project_type_counts_equal_the_sum_over_chapters(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    merge_judge_result(tmp_path, "chapter_02_chunk_000", "dialogue",
                       {"eval_name": "dialogue", "issues": [{"severity": "warning", "message": "raya"}]})

    totals = load_project_type_counts(tmp_path)
    summed = empty_type_counts()
    for counts in load_chapter_type_counts(tmp_path).values():
        for name, n in counts.items():
            summed[name] += n
    assert totals == summed
    assert totals["blacklist"] == 2
    assert totals["dialogue"] == 1


def test_chunk_id_without_a_chapter_still_reaches_the_project_total(tmp_path):
    # A chunk_id with no _chunk_ marker buckets under itself rather than being
    # dropped: it will never match a chapter row, but the book-wide count on the
    # home card must not silently lose it.
    save_chunk_evaluation(tmp_path, "loose_chunk", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])

    assert chapter_id_from_chunk_id("loose_chunk") == "loose_chunk"
    assert chapter_id_from_chunk_id("chapter_01_chunk_003") == "chapter_01"
    assert load_chapter_type_counts(tmp_path)["loose_chunk"]["blacklist"] == 1
    assert load_project_type_counts(tmp_path)["blacklist"] == 1


def test_chapter_type_counts_skip_stale_and_dismissed(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist", issue_index=0),
                                             _target_issue("grammar", issue_index=1)])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    append_feedback(tmp_path, "chapter_01_chunk_000", "blacklist", 0, "false_positive")
    mark_evaluation_stale(tmp_path, "chapter_02_chunk_000", "edited by apply")

    by_chapter = load_chapter_type_counts(tmp_path)
    assert by_chapter["chapter_01"]["blacklist"] == 0
    assert by_chapter["chapter_01"]["grammar"] == 1
    assert "chapter_02" not in by_chapter


def test_chapter_type_counts_ignore_source_side_findings(tmp_path):
    # Only target-side findings with a char span can be painted, so only those
    # are counted — the same gate the reader's Review Mode applies.
    issue = NormalizedIssue(
        eval_name="blacklist", eval_version="1.0.0", issue_index=0, severity="error",
        message="flagged", suggestion="", location=NormalizedLocation(raw="src", side="source"),
    )
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[issue])
    assert load_chapter_type_counts(tmp_path)["chapter_01"]["blacklist"] == 0
