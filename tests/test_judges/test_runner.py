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
    assert header["judges"] == {"dialogue": "1.2.0"}
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


def test_estimate_suite_cost_direct(monkeypatch):
    """estimate_suite_cost returns a float without crashing on real (mocked) calls."""
    monkeypatch.setattr(runner.llm_io, "estimate_call_cost", lambda *a, **k: 0.001)
    cost = runner.estimate_suite_cost(["dialogue"], [_target()], {})
    assert isinstance(cost, float)
    assert cost >= 0.0


def test_estimate_suite_cost_unknown_judge_skips(monkeypatch):
    """estimate_suite_cost tolerates an unknown judge name (ValueError) gracefully."""
    monkeypatch.setattr(runner.llm_io, "estimate_call_cost", lambda *a, **k: 0.01)
    # 'bogus' is not in the registry; the template load will raise ValueError.
    cost = runner.estimate_suite_cost(["bogus"], [_target()], {})
    # Still runs but with empty template — should produce some cost from the target text.
    assert isinstance(cost, float)


def test_run_judge_suite_header_skips_bad_judge(monkeypatch):
    """run_judge_suite header-build continues past a judge that raises ValueError."""
    monkeypatch.setattr(runner, "estimate_suite_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(runner, "run_judge", lambda jn, t, c: _ok_result(jn))
    # 'bogus' will raise ValueError in get_judge inside the header-build loop.
    out = runner.run_judge_suite(["bogus"], [_target()], {}, cost_limit=None)
    assert out["status"] == "ok"
    # The header judges dict should be empty (the bad judge was skipped).
    assert out["run_header"]["judges"] == {}


def test_run_judge_success_path(monkeypatch):
    """run_judge returns the judge's EvalResult on the happy path (line 50)."""
    # Patch the name runner actually calls: it does `from src.judges.registry import
    # get_judge` at module load, so that binding lives on runner, and patching
    # registry.get_judge is inert -- which silently ran the REAL dialogue judge and
    # billed a live API call on every test run.
    monkeypatch.setattr(runner, "get_judge", lambda name: _make_judge())
    result = runner.run_judge("dialogue", _target(), {})
    assert result.passed is True
    assert result.eval_name == "dialogue"


def _make_judge():
    """Return a minimal Judge whose run() immediately returns an ok EvalResult."""
    from src.judges.base import Judge, JudgeSpec, JudgeTarget
    from src.models import EvalResult
    from datetime import datetime

    class _FixedJudge(Judge):
        spec = JudgeSpec(
            name="dialogue",
            version="1.0.0",
            kind="verdict",
            template="judge_dialogue.txt",
        )

        def run(self, target: JudgeTarget, context):
            return EvalResult(
                eval_name="dialogue",
                eval_version="1.0.0",
                target_id=target.id,
                target_type=target.target_type,
                passed=True,
                score=1.0,
                issues=[],
                metadata={},
                executed_at=datetime.now(),
            )

    return _FixedJudge()


# ---------------------------------------------------------------------------
# build_run_header — extracted in this branch, previously inlined in run_judge_suite
# ---------------------------------------------------------------------------


def test_build_run_header_api_backend_no_worker_model():
    """API backend header has no worker_model key when worker_model is not passed."""
    header = runner.build_run_header(
        ["dialogue"],
        target_count=3,
        model=None,
        provider=None,
        backend="api",
    )
    assert header["backend"] == "api"
    assert header["target_count"] == 3
    assert header["judge_count"] == 1
    assert "worker_model" not in header


def test_build_run_header_subagent_backend_stamps_worker_model():
    """Subagent backend header includes worker_model when passed."""
    header = runner.build_run_header(
        ["dialogue"],
        target_count=2,
        model="claude-3-5-haiku-20241022",
        provider="anthropic",
        backend="subagent",
        worker_model="haiku",
    )
    assert header["backend"] == "subagent"
    assert header["worker_model"] == "haiku"
    assert header["model"] == "claude-3-5-haiku-20241022"
    assert header["provider"] == "anthropic"


def test_build_run_header_skips_bad_judge():
    """build_run_header continues when a judge name is not in the registry."""
    header = runner.build_run_header(
        ["bogus_judge_xyz"],
        target_count=1,
        model=None,
        provider=None,
    )
    # bad judge skipped — judges/prompt_versions dicts are empty, no crash
    assert header["judges"] == {}
    assert header["prompt_versions"] == {}


def test_build_run_header_started_at_defaulted_when_none():
    """started_at is auto-populated when not supplied."""
    header = runner.build_run_header([], target_count=0, model=None, provider=None)
    assert header["started_at"] is not None
