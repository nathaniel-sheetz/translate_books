"""The archaeology miner: version chains, approval edits and questionnaire overrides.

The miner reports what a book's history says was revisited, so what's worth
pinning is that it doesn't invent revisits: repeated copies of one version, the
light guide every commit carries, and an answer given as an index.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.decision_archaeology import (  # noqa: E402
    Versions,
    _date,
    questionnaire_events,
    version_events,
)


def _style(content, updated_at=None, **extra):
    data = {"content": content, **extra}
    if updated_at:
        data["updated_at"] = updated_at
    return data


def test_a_revert_followed_by_an_equal_live_copy_is_one_version():
    v = Versions()
    v.add("book", "style", "2026-01-01", "zip a", _style("A", "2026-01-01"))
    v.add("book", "style", "2026-02-01", "zip b", _style("B", "2026-02-01"))
    v.add("book", "style", "2026-03-01", "zip c", _style("A", "2026-03-01"))
    v.add("book", "style", "2026-03-05", "live", _style("A", "2026-03-01"))

    chain = v.chains()[("book", "style", "")]
    assert [c["data"]["content"] for c in chain] == ["A", "B", "A"]
    assert chain[-1]["also_in"] == ["live"]


def test_style_approval_ignores_the_light_guide_the_commit_adds():
    v = Versions()
    v.add("book", "style", "2026-01-01", "harness draft", {"content": "Use tú."}, draft=True)
    v.add("book", "style", "2026-01-02", "live",
          _style("Use tú.", "2026-01-02", light_content="Short guide."))
    assert not [e for e in version_events(v.chains()) if e["kind"] == "approval_edit"]


def test_style_approval_reports_a_changed_content():
    v = Versions()
    v.add("book", "style", "2026-01-01", "harness draft", {"content": "Use tú."}, draft=True)
    v.add("book", "style", "2026-01-02", "live",
          _style("Use usted.", "2026-01-02", light_content="Short guide."))
    edits = [e for e in version_events(v.chains()) if e["kind"] == "approval_edit"]
    assert [e["subject"] for e in edits] == ["content"]


def _questionnaire(tmp_path, answer):
    harness = tmp_path / "book" / ".harness"
    harness.mkdir(parents=True)
    (harness / "style_questions.json").write_text(json.dumps({"questions": [{
        "id": "q1", "question": "Which?", "default": 0,
        "options": [{"id": "alpha", "label": "Alpha"}, {"id": "beta", "label": "Beta"}],
    }]}), encoding="utf-8")
    (harness / "style_answers.json").write_text(json.dumps({"q1": answer}), encoding="utf-8")
    return questionnaire_events({"book": tmp_path / "book"}, None)


def test_an_index_string_naming_the_default_is_not_an_override(tmp_path):
    assert _questionnaire(tmp_path, "0") == []


def test_an_index_string_naming_another_option_is_an_override(tmp_path):
    events = _questionnaire(tmp_path, "1")
    assert [(e["kind"], e["answer"]) for e in events] == [("questionnaire_override", "Beta")]


def test_an_out_of_range_timestamp_has_no_date():
    assert _date("1e20") is None
