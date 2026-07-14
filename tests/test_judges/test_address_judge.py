"""Tests for the forms-of-address (usted/tú) judge (LLM mocked)."""

from __future__ import annotations

import json

from src.judges import llm_io
from src.judges.address_judge import AddressComplianceJudge
from src.judges.base import JudgeTarget
from src.models import IssueLevel


def _target(translation: str = "—Ven aquí.", chapter_id: str = "chapter_02") -> JudgeTarget:
    return JudgeTarget(
        id=f"{chapter_id}_chunk_000",
        target_type="chunk",
        source_text='"Come here," said Aunt Frances.',
        translated_text=translation,
        context={"chapter_id": chapter_id, "position": 0},
    )


def _ctx() -> dict:
    # Pass map + rubric explicitly so the test never depends on files on disk.
    return {
        "address_map": "Betsy→Frances: usted. Frances→Betsy: tú. Global: tú in family.",
        "address_rubric": "tú = tú/te/tu + 2nd-sg verbs; usted = usted/le + 3rd-sg verbs.",
    }


def test_clean_compliant(monkeypatch):
    monkeypatch.setattr(
        llm_io,
        "call_judge",
        lambda *a, **k: json.dumps({"compliant": True, "findings": [], "summary": "ok"}),
    )
    res = AddressComplianceJudge().run(_target(), _ctx())

    assert res.passed is True
    assert res.issues == []
    assert res.score == 1.0
    assert res.eval_name == "address"
    assert res.target_type == "chunk"
    assert res.metadata["compliant"] is True
    assert "prompt_version" in res.metadata


def test_explicit_error_vs_global_warning(monkeypatch):
    """Explicit pair violation is an error; a global-rule inference is a warning."""
    payload = {
        "compliant": False,
        "findings": [
            {
                "rule": "wrong-form-usted-expected",
                "severity": "error",
                "excerpt": "Ven aquí",
                "message": "private, Betsy→Frances: expected usted, found tú",
                "suggestion": "Venga aquí",
            },
            {
                "rule": "global-rule-violation",
                "severity": "warning",
                "excerpt": "¿Cómo estás?",
                "message": "a stranger addressed with tú (no explicit pair)",
                "suggestion": "¿Cómo está?",
            },
        ],
        "summary": "one explicit error, one global-rule warning",
    }
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: json.dumps(payload))
    res = AddressComplianceJudge().run(_target(), _ctx())

    assert res.passed is False  # an error-level issue present
    assert len(res.issues) == 2
    assert res.issues[0].severity == IssueLevel.ERROR
    assert res.issues[0].message.startswith("[wrong-form-usted-expected]")
    assert res.issues[0].suggestion == "Venga aquí"
    assert res.issues[1].severity == IssueLevel.WARNING
    assert res.issues[1].message.startswith("[global-rule-violation]")
    # 1 - (0.25 error + 0.10 warning), each a distinct rule under the cap.
    assert res.score == 0.65
    assert res.metadata["finding_count"] == 2


def test_nonissue_findings_filtered(monkeypatch):
    payload = {
        "compliant": False,
        "findings": [
            {
                "rule": "wrong-form-tu-expected",
                "severity": "error",
                "excerpt": "¿Quiere usted?",
                "message": "private, siblings: expected tú, found usted",
                "suggestion": "¿Quieres?",
            },
            {
                "rule": "ambiguous",
                "severity": "info",
                "excerpt": "su casa",
                "message": "addressee unclear — no violation here",
                "suggestion": "no change needed",
            },
        ],
        "summary": "one real, one non-issue",
    }
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: json.dumps(payload))
    res = AddressComplianceJudge().run(_target(), _ctx())

    assert len(res.issues) == 1
    assert res.issues[0].message.startswith("[wrong-form-tu-expected]")
    assert res.metadata["finding_count"] == 1
    assert res.metadata["filtered_nonissues"] == 1


def test_chapter_ref_rendered_in_prompt():
    """item_prompt_variables surfaces the chapter for story-stage expectations."""
    judge = AddressComplianceJudge()
    prompt = judge.build_prompt(_target(chapter_id="chapter_07"), _ctx())
    assert "{{" not in prompt  # fully rendered
    assert "chapter_07" in prompt
    assert "usted" in prompt  # the address map made it into the prompt


def test_batch_prompt_has_per_item_chapter_ref():
    judge = AddressComplianceJudge()
    t1 = _target(chapter_id="chapter_02")
    t2 = _target(chapter_id="chapter_09")
    batch = judge.build_batch_prompt([t1, t2], _ctx())
    assert "<chapter_ref>" in batch
    assert "chapter_02" in batch and "chapter_09" in batch


def test_missing_map_uses_placeholder():
    """With no address_map in context, the shared block carries a clear placeholder."""
    judge = AddressComplianceJudge()
    shared = judge.shared_prompt_variables({})
    assert "no address map" in shared["address_map"].lower()
    # The rubric still loads from prompts/address_forms.txt.
    assert shared["address_rubric"].strip()


def test_parse_failure_then_retry_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "here is my analysis (no json)"
        return json.dumps({"compliant": True, "findings": [], "summary": "ok"})

    monkeypatch.setattr(llm_io, "call_judge", fake)
    res = AddressComplianceJudge().run(_target(), _ctx())

    assert calls["n"] == 2
    assert res.passed is True


def test_unparseable_twice_returns_error_issue(monkeypatch):
    monkeypatch.setattr(llm_io, "call_judge", lambda *a, **k: "still not json")
    res = AddressComplianceJudge().run(_target(), _ctx())

    assert res.passed is False
    assert len(res.issues) == 1
    assert res.issues[0].severity == IssueLevel.ERROR
    assert res.score is None


def test_name_and_version():
    judge = AddressComplianceJudge()
    assert judge.name == "address"
    assert judge.version == "1.0.0"
    assert judge.batch_template == "judge_address_batch.txt"
