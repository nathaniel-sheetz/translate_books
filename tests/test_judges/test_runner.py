"""Tests for the judge runner (error isolation + cost gate + run header)."""

from __future__ import annotations

from src.judges import runner
from src.judges.base import JudgeTarget
from src.models import EvalResult, IssueLevel


def _target() -> JudgeTarget:
    return JudgeTarget("c0", "chunk", "source", "traducción", {})


def _ok_result(name: str = "dialogue") -> EvalResult:
    return EvalResult(
        eval_name=name,
        eval_version="1.0.0",
        target_id="c0",
        target_type="chunk",
        passed=True,
        score=1.0,
        issues=[],
        metadata={},
    )


def test_run_judge_isolates_unknown_judge():
    res = runner.run_judge("does_not_exist", _target(), {})
    assert res.passed is False
    assert res.eval_name == "does_not_exist"
    assert res.issues[0].severity == IssueLevel.ERROR


def test_cost_gate_blocks_without_confirm(monkeypatch):
    monkeypatch.setattr(runner, "estimate_suite_cost", lambda *a, **k: 5.0)
    out = runner.run_judge_suite(
        ["dialogue"], [_target()], {}, cost_limit=1.0, confirm=False
    )
    assert out["status"] == "cost_exceeded"
    assert out["estimated_cost"] == 5.0
    assert out["cost_limit"] == 1.0
    assert "results" not in out


def test_cost_gate_proceeds_with_confirm(monkeypatch):
    monkeypatch.setattr(runner, "estimate_suite_cost", lambda *a, **k: 5.0)
    monkeypatch.setattr(runner, "run_judge", lambda jn, t, c: _ok_result(jn))

    out = runner.run_judge_suite(
        ["dialogue"], [_target()], {}, cost_limit=1.0, confirm=True
    )
    assert out["status"] == "ok"
    assert len(out["results"]) == 1
    assert out["aggregated"]["total_evaluators"] == 1

    header = out["run_header"]
    assert header["judges"] == {"dialogue": "1.1.0"}
    assert "dialogue" in header["prompt_versions"]
    assert header["temperature"] == 0.0
    assert header["target_count"] == 1
    assert header["judge_count"] == 1
    assert "git_commit" in header


def test_no_cost_limit_runs(monkeypatch):
    monkeypatch.setattr(runner, "estimate_suite_cost", lambda *a, **k: 99.0)
    monkeypatch.setattr(runner, "run_judge", lambda jn, t, c: _ok_result(jn))
    out = runner.run_judge_suite(["dialogue"], [_target()], {}, cost_limit=None)
    assert out["status"] == "ok"
