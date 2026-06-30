"""Tests for judge-result persistence in web_ui/evaluations.py."""

from __future__ import annotations

from src.models import EvalResult, Issue, IssueLevel
from web_ui.evaluations import (
    load_chunk_evaluation,
    load_project_summary,
    merge_judge_result,
    save_chunk_evaluation,
)


def _judge_payload(severity: str = "error") -> dict:
    return EvalResult(
        eval_name="dialogue",
        eval_version="1.0.0",
        target_id="c0",
        target_type="chunk",
        passed=severity != "error",
        score=0.75,
        issues=[
            Issue(
                severity=IssueLevel(severity),
                message="[raya-spacing] space after raya",
                location="— Hola",
                suggestion="—Hola",
            )
        ],
        metadata={"compliant": False},
    ).model_dump(mode="json")


def _coded_aggregated() -> dict:
    return {
        "total_issues": 1,
        "issues_by_severity": {"error": 0, "warning": 1, "info": 0},
        "issues_by_evaluator": {"length": 1},
    }


def test_merge_creates_shell_when_missing(tmp_path):
    merge_judge_result(tmp_path, "c0", "dialogue", _judge_payload())
    loaded = load_chunk_evaluation(tmp_path, "c0")
    assert loaded is not None
    assert loaded["judges"]["dialogue"]["eval_name"] == "dialogue"
    assert loaded["results"] == []
    assert "judges_at" in loaded


def test_merge_preserves_coded_results(tmp_path):
    save_chunk_evaluation(tmp_path, "c0", [], _coded_aggregated(), [])
    merge_judge_result(tmp_path, "c0", "dialogue", _judge_payload())

    loaded = load_chunk_evaluation(tmp_path, "c0")
    assert loaded["aggregated"] == _coded_aggregated()
    assert "dialogue" in loaded["judges"]


def test_summary_folds_judge_issues_into_badges(tmp_path):
    save_chunk_evaluation(tmp_path, "c0", [], _coded_aggregated(), [])
    merge_judge_result(tmp_path, "c0", "dialogue", _judge_payload("error"))

    summary = load_project_summary(tmp_path)
    # 1 coded warning + 1 judge error, folded into the same 4-key shape.
    assert summary["c0"] == {"errors": 1, "warnings": 1, "info": 0, "total": 2}


def test_coded_rerun_preserves_judges(tmp_path):
    # Judge first, then a coded rerun that passes judges= through.
    merge_judge_result(tmp_path, "c0", "dialogue", _judge_payload())
    previous = load_chunk_evaluation(tmp_path, "c0")
    save_chunk_evaluation(
        tmp_path,
        "c0",
        [],
        _coded_aggregated(),
        [],
        judges=previous.get("judges"),
    )
    loaded = load_chunk_evaluation(tmp_path, "c0")
    assert "dialogue" in (loaded.get("judges") or {})
