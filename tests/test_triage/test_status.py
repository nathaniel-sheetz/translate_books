"""Tests for how a machine verdict surfaces on the recommendations screen.

Suppressed findings are the ones the reader never sees, so the only thing
keeping the filter honest is that they remain readable somewhere, with the
model's reason attached. These pin that, and the precedence rules around it:

* a human mark always beats a machine one — you ruling on a finding settles it;
* only a verdict that actually hides something may be labelled as having hidden
  it, or a card claims an effect the verdict had no part in;
* every status the filter UI can render has a label in both languages.
"""

from __future__ import annotations

import pytest

import web_ui.app as app
from web_ui.evaluations import TRIAGE_CONFIDENCE_FLOOR, triage_hides
from web_ui.i18n import get_strings

HIGH = {
    "verdict": "suppress", "confidence": 0.95, "reason": "A Norse proper noun.",
    "model": "grok-4.6", "ts": "2026-09-16T10:00:00",
}
LOW = {
    "verdict": "suppress", "confidence": TRIAGE_CONFIDENCE_FLOOR - 0.1,
    "reason": "Not sure.", "model": "grok-4.6", "ts": "2026-09-16T10:00:00",
}
KEEP = {
    "verdict": "keep", "confidence": 1.0, "reason": "Looks like a real typo.",
    "model": "grok-4.6", "ts": "2026-09-16T10:00:00",
}


def _item(**kw) -> dict:
    finding = {
        "eval_name": "dictionary", "chunk_id": "chapter_01_chunk_000",
        "issue_key": "k", "message": "'Sigfridos': Unknown word",
        "excerpt": "Sigfridos", **kw,
    }
    return app._finding_item(finding, "chapter_01", 0, {})


# --- triage_hides: the one definition of "does this verdict hide anything" ----

@pytest.mark.parametrize("record, expected", [
    (HIGH, True),
    (LOW, False),
    (KEEP, False),
    ({"verdict": "suppress", "confidence": "not a number"}, False),
    ({"verdict": "suppress"}, False),
    (None, False),
    ({}, False),
])
def test_triage_hides(record, expected):
    assert triage_hides(record) is expected


def test_the_floor_is_inclusive():
    """At the floor exactly, a suppress counts — the constant is a minimum."""
    assert triage_hides({"verdict": "suppress", "confidence": TRIAGE_CONFIDENCE_FLOOR})


# --- the card ----------------------------------------------------------------

def test_a_suppressed_finding_is_labelled_and_explains_itself():
    item = _item(triage=HIGH)
    assert item["status"] == "auto_suppressed"
    assert item["triage_reason"] == "A Norse proper noun."
    assert item["triage_model"] == "grok-4.6"
    assert item["triage_confidence"] == 0.95
    assert item["status_at"] == "2026-09-16T10:00:00"


def test_a_sub_floor_verdict_is_not_labelled_and_says_nothing():
    """It hid nothing, so the card must not imply it did."""
    item = _item(triage=LOW)
    assert item["status"] == "open"
    assert item["triage_reason"] is None
    assert item["triage_model"] is None
    assert item["triage_confidence"] is None


def test_a_keep_verdict_is_not_labelled():
    item = _item(triage=KEEP)
    assert item["status"] == "open"
    assert item["triage_reason"] is None


def test_a_human_mark_beats_a_machine_one():
    item = _item(triage=HIGH, feedback={"feedback_type": "resolved", "ts": "T2"})
    assert item["status"] == "fixed"
    assert item["status_at"] == "T2"
    # The machine's reasoning is withheld: it is not what became of the finding.
    assert item["triage_reason"] is None


def test_a_finding_with_no_verdict_is_unchanged():
    item = _item()
    assert item["status"] == "open"
    assert item["triage_reason"] is None


def test_a_triaged_finding_still_gets_a_favorite_handle():
    """The heart keys on chunk_id + issue_key, which a verdict does not disturb."""
    assert _item(triage=HIGH)["fav_id"] is not None


# --- registration ------------------------------------------------------------

def test_the_status_is_registered_and_unticked_on_arrival():
    assert "auto_suppressed" in app._RECOMMENDATION_STATUSES
    assert "auto_suppressed" in app._RECOMMENDATION_STATUSES_OFF
    # It is history, not outstanding work.
    assert "auto_suppressed" not in app._RECOMMENDATION_OPEN_STATUSES


@pytest.mark.parametrize("lang", ["en", "es"])
def test_every_renderable_status_has_a_label(lang):
    """An unlabelled status renders a blank chip and an unusable filter box."""
    labels = get_strings(lang)["rec_statuses"]
    missing = [s for s in app._RECOMMENDATION_STATUSES if s not in labels]
    assert missing == []
