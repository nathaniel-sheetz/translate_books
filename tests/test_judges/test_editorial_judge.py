"""Tests for the editorial judge's pass-1 behaviour (LLM mocked).

The properties pinned here are the ones carrying the risk. Two matter most:

* **The judge never sees the English in pass 1.** That is the whole design — a
  reader shown the original stops evaluating the Spanish as Spanish — and it is a
  single line in ``item_prompt_variables`` that a later refactor could restore
  from the base class without any other test noticing.
* **The coded half of the threshold actually fires.** The prompt asks for a
  confidence floor, a non-issue filter and a per-passage budget; the reason they
  are enforced in code is that the existing prompt-only threshold produces a 45%
  false-positive rate on the dialogue judge.
"""

from __future__ import annotations

import json

import pytest

from src.judges import llm_io
from src.judges.base import JudgeTarget
from src.judges.editorial_judge import (
    FINDINGS_PER_1000_WORDS,
    EditorialJudge,
    findings_budget,
    normalize_category,
    normalize_confidence,
    normalize_source_check,
    resolve_findings_per_1000,
)
from src.judges.scoring import finding_key
from src.models import IssueLevel

SPANISH = (
    "Pollyanna subió al ático con su pequeña maleta. La habitación estaba "
    "desnuda y calurosa, pero ella miró por la ventana y sonrió. Era un cuarto "
    "sin cuadros, sin alfombras, sin cortinas."
)


def _target(translation: str = SPANISH) -> JudgeTarget:
    return JudgeTarget(
        id="chapter_01_chunk_000",
        target_type="chunk",
        source_text="Pollyanna climbed to the attic with her little trunk.",
        translated_text=translation,
        context={"chapter_id": "chapter_01"},
    )


def _ctx(**overrides) -> dict:
    # Passed explicitly so no test depends on a book on disk.
    ctx = {
        "style_guide": "Mexican Spanish. Keep names in English form.",
        "style_rules": '- "names-english": keep personal names in English form.',
        "glossary": "CHARACTER NAMES:\n- Pollyanna → Pollyanna",
        "coded_findings": {},
    }
    ctx.update(overrides)
    return ctx


def _finding(**overrides) -> dict:
    finding = {
        "rule": "calque-syntax",
        "category": "NATURALNESS",
        "severity": "warning",
        "confidence": "high",
        "excerpt": "La habitación estaba desnuda y calurosa",
        "message": "Calqued English word order.",
        "suggestion": "El cuarto estaba desnudo y caluroso",
        "source_check": "not_needed",
    }
    finding.update(overrides)
    return finding


def _response(findings: list[dict], summary: str = "ok") -> str:
    return json.dumps({"findings": findings, "summary": summary}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Spanish-first


def test_pass_one_never_shows_the_english_source():
    """The rendered prompt carries the translation and not the source."""
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    prompt = judge.build_prompt(target, ctx)

    assert target.translated_text in prompt
    assert target.source_text not in prompt
    assert "source_text" not in judge.item_prompt_variables(target, ctx)


def test_cache_prefix_holds_the_book_inputs_and_not_the_passage():
    """The style guide and glossary cache; the passage does not."""
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    prefix, suffix = judge.build_prompt_parts(target, ctx)

    assert prefix + suffix == judge.build_prompt(target, ctx)
    assert "Mexican Spanish" in prefix
    assert "Pollyanna → Pollyanna" in prefix
    assert target.translated_text not in prefix
    assert target.translated_text in suffix


def test_run_sends_the_unchanged_prompt_with_a_cache_prefix(monkeypatch):
    seen = {}

    def fake(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["cache_prefix"] = kwargs.get("cache_prefix")
        return _response([])

    monkeypatch.setattr(llm_io, "call_judge", fake)
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    judge.run(target, ctx)

    assert seen["prompt"] == judge.build_prompt(target, ctx)
    assert seen["prompt"].startswith(seen["cache_prefix"])


# ---------------------------------------------------------------------------
# The coded half of the threshold


def test_clean_passage_returns_no_findings(monkeypatch):
    """An empty verdict is a normal outcome, not an error."""
    monkeypatch.setattr(llm_io, "call_judge", lambda prompt, **kw: _response([]))
    result = EditorialJudge().run(_target(), _ctx())

    assert result.issues == []
    assert result.passed is True
    assert result.score == 1.0
    assert result.metadata["clean"] is True


def test_medium_confidence_is_filtered_at_the_default_floor():
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    raw = _response([_finding(), _finding(confidence="medium", rule="other")])

    result = judge.parse_response(target, raw, ctx)

    assert len(result.issues) == 1
    assert result.metadata["filtered_low_confidence"] == 1
    assert result.metadata["proposed_count"] == 2


def test_medium_confidence_survives_a_relaxed_floor():
    """The floor is a knob, so it can be relaxed once the accept rate is known."""
    judge, target = EditorialJudge(), _target()
    ctx = _ctx(editorial_min_confidence="medium")
    raw = _response([_finding(), _finding(confidence="medium", rule="other")])

    result = judge.parse_response(target, raw, ctx)

    assert len(result.issues) == 2
    assert result.metadata["filtered_low_confidence"] == 0


def test_unknown_confidence_reads_as_the_weakest_value():
    """A malformed field must never promote a finding past the floor."""
    assert normalize_confidence("low") == "medium"
    assert normalize_confidence(None) == "medium"
    assert normalize_confidence("HIGH") == "high"

    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    result = judge.parse_response(target, _response([_finding(confidence="low")]), ctx)

    assert result.issues == []
    assert result.metadata["filtered_low_confidence"] == 1


def test_self_described_non_issues_are_dropped():
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    raw = _response([_finding(message="This is fine, no change needed.")])

    result = judge.parse_response(target, raw, ctx)

    assert result.issues == []
    assert result.metadata["filtered_nonissues"] == 1


def test_budget_truncates_by_severity_not_by_order():
    """Over budget, the least serious go — not whatever happened to be last."""
    judge = EditorialJudge()
    target = _target("palabra " * 400)  # 400 words -> budget 2
    ctx = _ctx()
    raw = _response(
        [
            _finding(severity="info", rule="a"),
            _finding(severity="error", rule="b"),
            _finding(severity="warning", rule="c"),
            _finding(severity="info", rule="d"),
        ]
    )

    result = judge.parse_response(target, raw, ctx)

    assert result.metadata["findings_budget"] == 2
    assert result.metadata["filtered_over_budget"] == 2
    assert [i.rule_id for i in result.issues] == ["b", "c"]


@pytest.mark.parametrize(
    "words,expected",
    [(0, 2), (76, 2), (400, 2), (1000, 6), (1246, 7), (2866, 17)],
)
def test_findings_budget_scales_with_length_and_floors_at_two(words, expected):
    assert findings_budget(words) == expected


@pytest.mark.parametrize(
    "words,per_1000,expected",
    [(1000, 3, 3), (1000, 12, 12), (2866, 3, 9), (76, 12, 2)],
)
def test_findings_budget_takes_a_rate(words, per_1000, expected):
    """The rate is a parameter; the short-chunk floor is not."""
    assert findings_budget(words, per_1000) == expected


@pytest.mark.parametrize("raw", [None, 0, -4, "", "lots", [3]])
def test_a_bad_rate_falls_back_to_the_default(raw):
    """A malformed knob must never widen a gate that exists to be narrow."""
    assert resolve_findings_per_1000({"editorial_findings_per_1000": raw}) == (
        FINDINGS_PER_1000_WORDS
    )
    assert resolve_findings_per_1000({}) == FINDINGS_PER_1000_WORDS


def test_a_rate_given_as_a_string_still_reads():
    """The CLI types it as int, but a manifest or a JSON payload may not."""
    assert resolve_findings_per_1000({"editorial_findings_per_1000": "9"}) == 9


def test_the_rate_reaches_both_the_prompt_and_the_truncation():
    """The number the prompt states is the number code enforces."""
    judge = EditorialJudge()
    target = _target("palabra " * 1000)  # 1000 words
    ctx = _ctx(editorial_findings_per_1000=4)

    prompt = judge.build_prompt(target, ctx)
    assert "at most 4 findings" in prompt

    raw = _response([_finding(rule=str(i)) for i in range(6)])
    result = judge.parse_response(target, raw, ctx)

    assert result.metadata["findings_budget"] == 4
    assert result.metadata["findings_per_1000"] == 4
    assert result.metadata["filtered_over_budget"] == 2
    assert len(result.issues) == 4


def test_an_explicit_override_beats_the_word_count():
    """How the subagent backend carries `prepare`'s ceiling across to `commit`.

    `commit` rebuilds targets with empty text, so without the override a long
    chunk's findings would be truncated to the short-chunk floor.
    """
    judge = EditorialJudge()
    empty = JudgeTarget(
        id="chapter_01_chunk_000",
        target_type="chunk",
        source_text="",
        translated_text="",
        context={},
    )
    raw = _response([_finding(rule=str(i)) for i in range(5)])

    assert len(judge.parse_response(empty, raw, _ctx()).issues) == 2  # the floor

    ctx = _ctx(max_findings_override=5)
    result = judge.parse_response(empty, raw, ctx)
    assert result.metadata["findings_budget"] == 5
    assert result.metadata["filtered_over_budget"] == 0
    assert len(result.issues) == 5


@pytest.mark.parametrize("bad", [0, -1, True, "5", None])
def test_a_bad_override_is_ignored_rather_than_obeyed(bad):
    judge = EditorialJudge()
    target = _target("palabra " * 1000)
    raw = _response([_finding()])

    result = judge.parse_response(target, raw, _ctx(max_findings_override=bad))

    assert result.metadata["findings_budget"] == findings_budget(1000)


# ---------------------------------------------------------------------------
# Identity, so a dismissal survives a re-judge


def test_findings_carry_a_stable_identity():
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    result = judge.parse_response(target, _response([_finding()]), ctx)
    issue = result.issues[0]

    assert issue.rule_id == "calque-syntax"
    assert issue.category == "NATURALNESS"
    assert issue.finding_key == finding_key(
        "calque-syntax", "La habitación estaba desnuda y calurosa"
    )


def test_identity_survives_a_reworded_message():
    """The point of the explicit key: an LLM rewords, a dismissal must not move."""
    judge, target, ctx = EditorialJudge(), _target(), _ctx()

    first = judge.parse_response(target, _response([_finding()]), ctx)
    second = judge.parse_response(
        target,
        _response([_finding(message="English word order has been carried over here.")]),
        ctx,
    )

    assert first.issues[0].message != second.issues[0].message
    assert first.issues[0].finding_key == second.issues[0].finding_key


def test_identity_moves_when_the_quoted_prose_changes():
    judge, target, ctx = EditorialJudge(), _target(), _ctx()

    first = judge.parse_response(target, _response([_finding()]), ctx)
    second = judge.parse_response(
        target, _response([_finding(excerpt="Era un cuarto sin cuadros")]), ctx
    )

    assert first.issues[0].finding_key != second.issues[0].finding_key


def test_finding_key_ignores_rewrapped_whitespace():
    assert finding_key("r", "dos  palabras\n aquí") == finding_key("r", "dos palabras aquí")


def test_issue_key_prefers_the_explicit_finding_key():
    """The web_ui join must read the judge's key, not re-derive one from the message."""
    from web_ui.evaluations import issue_key

    issue = {
        "severity": "warning",
        "message": "[calque-syntax] one wording",
        "location": "La habitación estaba desnuda y calurosa",
        "finding_key": "deadbeefdeadbeef",
    }
    reworded = {**issue, "message": "[calque-syntax] a different wording"}

    assert issue_key("editorial", issue) == "deadbeefdeadbeef"
    assert issue_key("editorial", reworded) == issue_key("editorial", issue)


def test_issue_key_is_unchanged_for_findings_without_one():
    """The 879-row corpus keys on message+location and must keep doing so."""
    from web_ui.evaluations import issue_key

    issue = {"severity": "warning", "message": "m", "location": "l"}

    assert issue_key("dialogue", issue) == issue_key("dialogue", {**issue, "finding_key": None})
    assert issue_key("dialogue", issue) != issue_key("dialogue", {**issue, "message": "m2"})


def test_existing_judges_keep_the_derived_key():
    """``stable_identity`` is opt-in, so dialogue/address dismissals are not orphaned."""
    from src.judges.scoring import finding_to_issue

    issue = finding_to_issue(_finding(), default_message="x")

    assert issue.finding_key is None
    assert issue.rule_id is None
    assert issue.category is None


# ---------------------------------------------------------------------------
# Normalization and dedup


@pytest.mark.parametrize(
    "value,expected",
    [
        ("GRAMMAR", "GRAMMAR"),
        ("grammar", "GRAMMAR"),
        ("CLARITY", "NATURALNESS"),
        (None, "NATURALNESS"),
    ],
)
def test_unknown_categories_fall_back_rather_than_drop_the_finding(value, expected):
    assert normalize_category(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("required", "required"),
        ("RECOMMENDED", "recommended"),
        ("maybe", "not_needed"),
        (None, "not_needed"),
    ],
)
def test_unknown_source_check_does_not_conscript_an_english_window(value, expected):
    assert normalize_source_check(value) == expected


def test_already_reported_findings_reach_the_prompt():
    judge, target = EditorialJudge(), _target()
    ctx = _ctx(
        coded_findings={
            "chapter_01_chunk_000": ["[dictionary] 'ático': Unknown word"],
            "chapter_09_chunk_000": ["[grammar] some other chunk"],
        }
    )

    prompt = judge.build_prompt(target, ctx)

    assert "'ático': Unknown word" in prompt
    assert "some other chunk" not in prompt


def test_no_coded_findings_renders_a_stated_placeholder():
    judge, target, ctx = EditorialJudge(), _target(), _ctx()
    variables = judge.item_prompt_variables(target, ctx)

    assert "nothing has been reported" in variables["already_reported"]


# ---------------------------------------------------------------------------
# Parse failure


def test_unparseable_response_retries_then_reports_one_error(monkeypatch):
    calls = []

    def fake(prompt, **kwargs):
        calls.append(prompt)
        return "not json at all"

    monkeypatch.setattr(llm_io, "call_judge", fake)
    result = EditorialJudge().run(_target(), _ctx())

    assert len(calls) == 2
    assert len(result.issues) == 1
    assert result.issues[0].severity == IssueLevel.ERROR
    assert "unparseable" in result.issues[0].message


def test_parse_response_raises_so_the_draft_backend_can_respawn():
    with pytest.raises(llm_io.JudgeParseError):
        EditorialJudge().parse_response(_target(), "{}", _ctx())


# ---------------------------------------------------------------------------
# Batched prompt


def test_batch_prompt_carries_one_item_per_target_and_shares_the_book_inputs():
    judge, ctx = EditorialJudge(), _ctx()
    first = _target()
    second = JudgeTarget(
        id="chapter_02_chunk_000",
        target_type="chunk",
        source_text="Another source.",
        translated_text="Otra traducción distinta que juzgar.",
        context={"chapter_id": "chapter_02"},
    )

    prompt = judge.build_batch_prompt([first, second], ctx)

    assert prompt.count('<item id=') == 2
    assert prompt.count("Mexican Spanish") == 1
    assert first.translated_text in prompt
    assert second.translated_text in prompt
    assert first.source_text not in prompt
