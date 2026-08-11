"""Tests for the dialogue-compliance judge (LLM mocked)."""

from __future__ import annotations

import json

import pytest

from src.judges import llm_io
from src.judges.base import JudgeTarget
from src.judges.dialogue_judge import DialogueComplianceJudge
from src.models import IssueLevel


def _target(translation: str = "—Hola.") -> JudgeTarget:
    return JudgeTarget(
        id="ch01_chunk_000",
        target_type="chunk",
        source_text='"Hello."',
        translated_text=translation,
        context={},
    )


def _ctx() -> dict:
    # Pass rules explicitly so the test never depends on prompts/dialogue.txt.
    return {"dialogue_rules": "Use the raya. One turn, one paragraph."}


def test_run_sends_cacheable_prefix_and_unchanged_prompt(monkeypatch):
    """run() caches the house-rules head without altering the prompt it sends."""
    seen = {}

    def fake(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["cache_prefix"] = kwargs.get("cache_prefix")
        return json.dumps({"compliant": True, "findings": [], "summary": "ok"})

    monkeypatch.setattr(llm_io, "call_judge", fake)
    judge = DialogueComplianceJudge()
    target, ctx = _target(), _ctx()
    judge.run(target, ctx)

    assert seen["prompt"] == judge.build_prompt(target, ctx)
    assert seen["cache_prefix"]
    assert seen["prompt"].startswith(seen["cache_prefix"])
    assert "Use the raya." in seen["cache_prefix"]
    assert target.translated_text not in seen["cache_prefix"]


def test_retry_reuses_the_same_cache_prefix(monkeypatch):
    """The JSON-only note is appended, so the retry reads the cache, not rewrites it."""
    calls = []

    def fake(prompt, **kwargs):
        calls.append((prompt, kwargs.get("cache_prefix")))
        if len(calls) == 1:
            return "not json at all"
        return json.dumps({"compliant": True, "findings": [], "summary": "ok"})

    monkeypatch.setattr(llm_io, "call_judge", fake)
    DialogueComplianceJudge().run(_target(), _ctx())

    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert calls[1][1]
    assert calls[1][0].startswith(calls[1][1])


def test_clean_compliant(monkeypatch):
    monkeypatch.setattr(
        llm_io,
        "call_judge",
        lambda *a, **k: json.dumps(
            {"compliant": True, "findings": [], "summary": "ok"}
        ),
    )
    res = DialogueComplianceJudge().run(_target(), _ctx())

    assert res.passed is True
    assert res.issues == []
    assert res.score == 1.0
    assert res.eval_name == "dialogue"
    assert res.target_type == "chunk"
    assert res.metadata["compliant"] is True
    assert "prompt_version" in res.metadata


def test_findings_mapped_to_issues(monkeypatch):
    payload = {
        "compliant": False,
        "findings": [
            {
                "rule": "raya-spacing",
                "severity": "error",
                "excerpt": "— Hola",
                "message": "space after the opening raya",
                "suggestion": "—Hola",
            },
            {
                "rule": "one-turn-one-paragraph",
                "severity": "warning",
                "excerpt": "...dijo. —Sí",
                "message": "turn not on its own paragraph",
                "suggestion": "split into two paragraphs",
            },
        ],
        "summary": "two issues",
    }
    # Wrapped in a code fence to exercise extract_json's fence handling.
    monkeypatch.setattr(
        llm_io, "call_judge", lambda *a, **k: "```json\n" + json.dumps(payload) + "\n```"
    )
    res = DialogueComplianceJudge().run(_target("— Hola"), _ctx())

    assert res.passed is False  # an error-level issue is present
    assert len(res.issues) == 2
    assert res.issues[0].severity == IssueLevel.ERROR
    assert res.issues[0].message.startswith("[raya-spacing]")
    assert res.issues[0].location == "— Hola"
    assert res.issues[0].suggestion == "—Hola"
    assert res.issues[1].severity == IssueLevel.WARNING
    assert 0.0 <= res.score < 1.0
    assert res.metadata["finding_count"] == 2
    assert res.metadata["compliant"] is False


def test_nonissue_findings_filtered(monkeypatch):
    """Self-described 'no violation' / 'no change needed' findings are dropped."""
    payload = {
        "compliant": False,
        "findings": [
            {
                "rule": "narration-separation",
                "severity": "warning",
                "excerpt": "...dijo. Sonrió. —Sí",
                "message": "bare narrative sentence dropped between turns",
                "suggestion": "—Sí —dijo, sonriendo.",
            },
            {
                "rule": "narration-separation",
                "severity": "warning",
                "excerpt": "Sonrió.\n\n—Sí",
                "message": "this is placed correctly — no violation here",
                "suggestion": "No change needed.",
            },
            {
                "rule": "inciso-punctuation",
                "severity": "info",
                "excerpt": "—Sí.",
                "message": "this is a borderline case",
                "suggestion": "acceptable as written; no change strictly required",
            },
        ],
        "summary": "one real issue, two non-issues",
    }
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: json.dumps(payload))
    res = DialogueComplianceJudge().run(_target(), _ctx())

    assert len(res.issues) == 1  # only the genuine violation survives
    assert res.issues[0].message.startswith("[narration-separation]")
    assert res.metadata["finding_count"] == 1
    assert res.metadata["filtered_nonissues"] == 2
    assert res.metadata["compliant"] is False


def test_repeated_rule_is_capped(monkeypatch):
    """20 findings of one rule must not floor the score the way 20x0.10 would."""
    payload = {
        "compliant": False,
        "findings": [
            {
                "rule": "narration-separation",
                "severity": "warning",
                "excerpt": f"snippet {i}",
                "message": f"bare narrative sentence #{i}",
                "suggestion": f"fold into inciso #{i}",
            }
            for i in range(20)
        ],
        "summary": "one systemic pattern, repeated",
    }
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: json.dumps(payload))
    res = DialogueComplianceJudge().run(_target(), _ctx())

    assert len(res.issues) == 20
    # Uncapped this would be 1 - 20*0.10 = 0.0; the per-rule cap keeps it >= 1 - 0.30.
    assert res.score == 0.70


def test_parse_failure_then_retry_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "sorry, here is my analysis (no json)"
        return json.dumps({"compliant": True, "findings": [], "summary": "ok"})

    monkeypatch.setattr(llm_io, "call_judge", fake)
    res = DialogueComplianceJudge().run(_target(), _ctx())

    assert calls["n"] == 2
    assert res.passed is True
    assert res.issues == []


def test_unparseable_twice_returns_error_issue(monkeypatch):
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: "still not json")
    res = DialogueComplianceJudge().run(_target(), _ctx())

    assert res.passed is False
    assert len(res.issues) == 1
    assert res.issues[0].severity == IssueLevel.ERROR
    assert res.score is None
    assert "error" in res.metadata


# ---------------------------------------------------------------------------
# coerce_severity
# ---------------------------------------------------------------------------


def test_coerce_severity_already_issue_level():
    from src.judges.base import coerce_severity
    assert coerce_severity(IssueLevel.ERROR) == IssueLevel.ERROR


def test_coerce_severity_string():
    from src.judges.base import coerce_severity
    assert coerce_severity("warning") == IssueLevel.WARNING


def test_coerce_severity_invalid_falls_back_to_default():
    from src.judges.base import coerce_severity
    assert coerce_severity("bogus_level") == IssueLevel.WARNING


def test_coerce_severity_custom_default():
    from src.judges.base import coerce_severity
    assert coerce_severity("not_real", IssueLevel.INFO) == IssueLevel.INFO


# ---------------------------------------------------------------------------
# Judge.name and Judge.version properties
# ---------------------------------------------------------------------------


def test_judge_name_and_version_properties():
    judge = DialogueComplianceJudge()
    assert judge.name == "dialogue"
    assert judge.version == "1.3.0"


# ---------------------------------------------------------------------------
# VerdictJudge.make_result optional metadata fields
# ---------------------------------------------------------------------------


def test_make_result_without_optional_fields():
    """make_result works when prompt_version, model, and provider are all None."""
    judge = DialogueComplianceJudge()
    result = judge.make_result(_target(), [], score=1.0)
    assert result.metadata.get("judge_version") == "1.3.0"
    assert result.metadata.get("judge_kind") == "verdict"
    assert "prompt_version" not in result.metadata
    assert "model" not in result.metadata
    assert "provider" not in result.metadata


def test_make_result_extra_metadata_merged():
    """metadata kwarg entries are merged into the base metadata dict."""
    judge = DialogueComplianceJudge()
    result = judge.make_result(_target(), [], score=0.8, metadata={"custom": "x"})
    assert result.metadata["custom"] == "x"
    assert result.metadata["judge_version"] == "1.3.0"


def test_make_result_model_and_provider_stamped():
    """make_result stamps model and provider into metadata when they are provided."""
    judge = DialogueComplianceJudge()
    result = judge.make_result(
        _target(), [], score=1.0, model="claude-3-5-haiku-20241022", provider="anthropic"
    )
    assert result.metadata["model"] == "claude-3-5-haiku-20241022"
    assert result.metadata["provider"] == "anthropic"


def test_dialogue_judge_uses_default_rules_when_ctx_empty(monkeypatch):
    """When context has no dialogue_rules, the judge loads from prompts/dialogue.txt."""
    monkeypatch.setattr(
        llm_io,
        "call_judge",
        lambda *a, **k: '{"findings": [], "summary": "ok"}',
    )
    # Pass empty context — _load_default_rules() should be invoked (line 131).
    res = DialogueComplianceJudge().run(_target(), {})
    assert res.passed is True


# ---------------------------------------------------------------------------
# Judge base-class: build_prompt default and parse_response default
# ---------------------------------------------------------------------------


def test_judge_base_build_prompt_default(monkeypatch):
    """Judge.build_prompt on a subclass that does NOT override prompt_variables uses
    the base default (source_text + translation_text) and produces a non-empty string."""
    from src.judges.base import Judge, JudgeSpec, JudgeTarget

    class _MinimalJudge(Judge):
        spec = JudgeSpec(
            name="dialogue",
            version="1.0.0",
            kind="verdict",
            template="judge_dialogue.txt",
        )

        def run(self, target, context):  # pragma: no cover
            pass

    monkeypatch.setattr(llm_io, "load_template", lambda t: "{source_text}|{translation_text}")
    monkeypatch.setattr(llm_io, "render", lambda tmpl, vars: tmpl.format(**vars))

    judge = _MinimalJudge()
    target = JudgeTarget("c0", "chunk", "HELLO", "HOLA", {})
    result = judge.build_prompt(target, {})
    assert "HELLO" in result
    assert "HOLA" in result


def test_judge_base_parse_response_raises_not_implemented():
    """Judge.parse_response default raises NotImplementedError (not silently returns)."""
    from src.judges.base import Judge, JudgeSpec, JudgeTarget

    class _NoParseJudge(Judge):
        spec = JudgeSpec(
            name="dialogue",
            version="1.0.0",
            kind="verdict",
            template="judge_dialogue.txt",
        )

        def run(self, target, context):  # pragma: no cover
            pass

    judge = _NoParseJudge()
    with pytest.raises(NotImplementedError, match="parse_response"):
        judge.parse_response(JudgeTarget("c0", "chunk", "", "", {}), "{}", {})
