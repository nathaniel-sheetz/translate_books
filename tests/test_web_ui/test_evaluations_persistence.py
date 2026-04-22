"""Tests for web_ui/evaluations.py persistence helpers."""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from src.models import EvalResult, Issue, IssueLevel
from web_ui.evaluations import (
    append_feedback,
    load_chunk_evaluation,
    load_project_summary,
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
