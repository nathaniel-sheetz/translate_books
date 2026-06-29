"""Tests for the dialogue-compliance judge (LLM mocked)."""

from __future__ import annotations

import json

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
