"""Tests for llm_io helpers (template, JSON parsing, cost, provider)."""

from __future__ import annotations

import json

import pytest

from src.judges import llm_io
from src.judges.llm_io import JudgeParseError, extract_json, parse_judge_json
from src.models import EvalResult, Issue, IssueLevel


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain_object():
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_array():
    """An array opener should also be extracted."""
    raw = "[1, 2, 3]"
    result = extract_json(raw)
    assert json.loads(result) == [1, 2, 3]


def test_extract_json_fenced_code_block():
    """Content inside ```json ... ``` fences is preferred."""
    raw = 'Here is the response:\n```json\n{"findings": []}\n```\nDone.'
    result = extract_json(raw)
    assert json.loads(result) == {"findings": []}


def test_extract_json_plain_fence():
    """``` without the json tag should also work."""
    raw = "```\n{\"x\": 1}\n```"
    result = extract_json(raw)
    assert json.loads(result) == {"x": 1}


def test_extract_json_json_preceded_by_prose():
    """JSON embedded after prose text should be extracted."""
    raw = 'Here is my analysis: {"score": 0.9, "findings": []}'
    result = extract_json(raw)
    assert json.loads(result)["score"] == 0.9


def test_extract_json_malformed_falls_back_to_stripped():
    """When no valid JSON is found, the stripped text is returned as-is."""
    raw = "  no json here  "
    result = extract_json(raw)
    assert result == "no json here"


def test_extract_json_broken_opener_returns_stripped():
    """When the first { has invalid JSON (not a valid object), extract_json
    continues past it (``except json.JSONDecodeError: continue``).

    With no other valid opener found, the function falls through to
    ``return stripped`` — exercising the JSONDecodeError continue branch AND
    the final fallback in the same path.
    """
    raw = "  {broken: not valid json}  "
    result = extract_json(raw)
    # raw_decode fails on the broken { → continue; no [ found → return stripped
    assert result == raw.strip()


# ---------------------------------------------------------------------------
# parse_judge_json
# ---------------------------------------------------------------------------


def test_parse_judge_json_valid():
    raw = json.dumps({"findings": [], "summary": "ok"})
    result = parse_judge_json(raw, ["findings"])
    assert result["findings"] == []


def test_parse_judge_json_invalid_json_raises():
    with pytest.raises(JudgeParseError, match="Invalid JSON"):
        parse_judge_json("not json at all !@#", ["findings"])


def test_parse_judge_json_array_raises():
    """A top-level JSON array must raise JudgeParseError (not a dict)."""
    with pytest.raises(JudgeParseError, match="Expected a JSON object"):
        parse_judge_json("[1, 2, 3]", ["findings"])


def test_parse_judge_json_missing_field_raises():
    raw = json.dumps({"summary": "ok"})  # missing 'findings'
    with pytest.raises(JudgeParseError, match="Missing fields"):
        parse_judge_json(raw, ["findings"])


def test_parse_judge_json_extra_fields_ok():
    """Extra top-level keys beyond required_fields are allowed."""
    raw = json.dumps({"findings": [], "bonus_key": "x", "summary": "y"})
    result = parse_judge_json(raw, ["findings"])
    assert "bonus_key" in result


# ---------------------------------------------------------------------------
# format_signals_for_judge
# ---------------------------------------------------------------------------


def _make_result(name: str, issues: list[Issue]) -> EvalResult:
    return EvalResult(
        eval_name=name,
        eval_version="1.0",
        target_id="c0",
        target_type="chunk",
        passed=not any(i.severity == IssueLevel.ERROR for i in issues),
        score=None,
        issues=issues,
        metadata={},
    )


def test_format_signals_no_issues():
    result = _make_result("length", [])
    assert llm_io.format_signals_for_judge([result]) == "None flagged."


def test_format_signals_none_input():
    assert llm_io.format_signals_for_judge(None) == "None flagged."


def test_format_signals_with_issues():
    issues = [
        Issue(severity=IssueLevel.WARNING, message="too long", location=None, suggestion=None),
        Issue(severity=IssueLevel.ERROR, message="missing gloss", location=None, suggestion=None),
    ]
    result = _make_result("length", issues)
    formatted = llm_io.format_signals_for_judge([result])
    assert "length (2)" in formatted
    assert "too long" in formatted
    assert "missing gloss" in formatted


def test_format_signals_multiple_evaluators():
    r1 = _make_result("length", [Issue(severity=IssueLevel.WARNING, message="long", location=None, suggestion=None)])
    r2 = _make_result("glossary", [Issue(severity=IssueLevel.ERROR, message="missing", location=None, suggestion=None)])
    formatted = llm_io.format_signals_for_judge([r1, r2])
    assert "- length (1)" in formatted
    assert "- glossary (1)" in formatted


# ---------------------------------------------------------------------------
# resolve_provider
# ---------------------------------------------------------------------------


def test_resolve_provider_explicit(monkeypatch):
    """Explicit provider wins without touching model resolution."""
    assert llm_io.resolve_provider("anthropic", None) == "anthropic"


def test_resolve_provider_from_model(monkeypatch):
    """When model resolves cleanly, its provider is returned."""
    monkeypatch.setattr(llm_io, "resolve_provider_for_model", lambda m: "openai")
    result = llm_io.resolve_provider(None, "gpt-4o")
    assert result == "openai"


def test_resolve_provider_model_unknown_falls_back_to_default(monkeypatch):
    """Unknown model (ValueError) falls through to get_default_provider."""
    monkeypatch.setattr(llm_io, "resolve_provider_for_model", lambda m: (_ for _ in ()).throw(ValueError("unknown")))
    monkeypatch.setattr(llm_io, "get_default_provider", lambda: "anthropic")
    result = llm_io.resolve_provider(None, "mystery-model")
    assert result == "anthropic"


def test_resolve_provider_no_hint(monkeypatch):
    """No provider or model → get_default_provider."""
    monkeypatch.setattr(llm_io, "get_default_provider", lambda: "anthropic")
    result = llm_io.resolve_provider(None, None)
    assert result == "anthropic"


# ---------------------------------------------------------------------------
# estimate_tokens + estimate_call_cost
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty():
    """Empty string floors to 1."""
    assert llm_io.estimate_tokens("") == 1


def test_estimate_tokens_typical():
    assert llm_io.estimate_tokens("a" * 400) == 100


def test_estimate_call_cost_zero_pricing(monkeypatch):
    """Zero pricing → zero cost."""
    monkeypatch.setattr(llm_io, "get_model_pricing", lambda p, m: {"input": 0.0, "output": 0.0})
    monkeypatch.setattr(llm_io, "resolve_provider", lambda p, m: "anthropic")
    cost = llm_io.estimate_call_cost("x" * 4000, provider="anthropic", model=None)
    assert cost == 0.0


def test_estimate_call_cost_nonzero(monkeypatch):
    """With real pricing, cost is positive and proportional to prompt size."""
    monkeypatch.setattr(llm_io, "get_model_pricing", lambda p, m: {"input": 3.0, "output": 15.0})
    monkeypatch.setattr(llm_io, "resolve_provider", lambda p, m: "anthropic")
    cost = llm_io.estimate_call_cost("a" * 4_000_000, provider="anthropic", model=None)
    # 1M tokens input * $3/M = $3 input cost; plus 600 output tokens * $15/M
    assert cost > 0.0


# ---------------------------------------------------------------------------
# load_template path traversal guard
# ---------------------------------------------------------------------------


def test_load_template_path_traversal_blocked(tmp_path, monkeypatch):
    """load_template must reject names that escape the prompts/ directory."""
    import src.judges.llm_io as _llm_io
    monkeypatch.setattr(_llm_io, "_PROMPTS_DIR", tmp_path)
    with pytest.raises(ValueError, match="escapes prompts directory"):
        llm_io.load_template("../secret.txt")


def test_load_template_valid_name(tmp_path, monkeypatch):
    """load_template loads a real file when the name stays inside the dir."""
    import src.judges.llm_io as _llm_io
    monkeypatch.setattr(_llm_io, "_PROMPTS_DIR", tmp_path)
    (tmp_path / "valid.txt").write_text("content", encoding="utf-8")
    assert llm_io.load_template("valid.txt") == "content"


# ---------------------------------------------------------------------------
# call_judge
# ---------------------------------------------------------------------------


def test_call_judge_dispatches_to_call_llm(monkeypatch):
    """call_judge resolves the provider and delegates to call_llm (lines 190-191)."""
    calls = {}

    def fake_call_llm(prompt, *, provider, model, temperature, max_retries, call_type):
        calls["provider"] = provider
        calls["call_type"] = call_type
        calls["temperature"] = temperature
        return '{"findings": []}'

    monkeypatch.setattr(llm_io, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm_io, "resolve_provider", lambda p, m: "anthropic")

    result = llm_io.call_judge("some prompt", call_type="judge_dialogue")
    assert result == '{"findings": []}'
    assert calls["provider"] == "anthropic"
    assert calls["call_type"] == "judge_dialogue"
    assert calls["temperature"] == 0.0
