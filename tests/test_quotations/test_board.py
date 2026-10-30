"""The board's recommendation and choices, on hand-made cards (no project on disk)."""

import pytest

from src.quotations import board as qboard


def _card(**over):
    card = {
        "key": "chapter_01|43 8:12|abc", "chapter_id": "chapter_01", "kind": "quotation",
        "status": "proposed", "ref_edit": None, "adapted": False, "drift": "", "confidence": "high",
        "refusals": [], "echo": [],
        "quote_edits": [{"es_idx": 0, "old": "luz del mundo", "new": "luz del camino"}],
    }
    card.update(over)
    return card


@pytest.mark.parametrize("over, verdict, rule", [
    ({"status": "not_scripture", "quote_edits": []}, "decline", "not_scripture"),
    ({"status": "unresolved", "quote_edits": [], "unresolved_reason": "no such verse"}, "decline", "unresolved"),
    ({"status": "no_mapping", "quote_edits": []}, "decline", "no_mapping"),
    ({"status": "unmapped", "quote_edits": []}, "decline", "no_mapping"),
    ({"status": "reference", "quote_edits": [], "ref_edit": {"es_idx": 0, "old": "Juan 8:12", "new": "Jn 8:12"}},
     "approve", "reference_fix"),
    ({"status": "reference", "quote_edits": []}, "decline", "reference_ok"),
    ({"status": "matches", "quote_edits": [{"es_idx": 0, "old": "x", "new": "x"}]}, "approve", "matches"),
    ({"status": "refused", "quote_edits": [], "refusals": [{"reason": "piece_not_verbatim"}]}, "unsure", "refused"),
    ({"revised": {"ask": "keep 'Ni siquiera'", "at": "t"}}, "unsure", "revised"),
    ({"drift": "KJV 'converteth'; NBLA 'restaura'"}, "unsure", "drift"),
    ({"adapted": True, "why": "birds for lilies"}, "unsure", "adapted"),
    ({"kind": "allusion"}, "unsure", "allusion"),
    ({"confidence": "low"}, "unsure", "low_confidence"),
    ({"refusals": [{"reason": "overlaps_another_edit"}]}, "unsure", "partly_refused"),
    ({"quote_edits": [{"es_idx": 0, "old": "x", "new": "x"}],
      "echo": [{"es_idx": 1, "old": "a", "new": "b"}]}, "unsure", "echo_only"),
    ({}, "approve", "clean"),
])
def test_recommend_rules(over, verdict, rule):
    out = qboard.recommend(_card(**over))
    assert (out["verdict"], out["rule"]) == (verdict, rule)


def test_the_recommendation_quotes_the_drift():
    assert qboard.recommend(_card(drift="made under the law"))["detail"] == "made under the law"


def test_default_ticks_the_change_and_follows_the_recommendation():
    assert qboard.default_choice(_card()) == {
        "ref": False, "nbla": True, "echo": [], "paste": "", "state": "approve", "hand": False, "ask": ""}
    # The NBLA wording is ticked even when unsure: the state is what holds it back.
    allusion = qboard.default_choice(_card(kind="allusion"))
    assert allusion["nbla"] is True and allusion["state"] == "pending"
    assert qboard.default_choice(_card(status="not_scripture", quote_edits=[]))["state"] == "reject"


@pytest.mark.parametrize("raw, decision, state", [
    ({"nbla": True}, {"action": "applied"}, "approve"),
    ({"nbla": False}, {"action": "declined"}, "reject"),
    ({"nbla": True}, None, "approve"),
    ({"nbla": False}, None, "reject"),
    ({"nbla": True, "state": "pending"}, {"action": "applied"}, "pending"),
])
def test_a_choice_saved_before_states_takes_one(raw, decision, state):
    assert qboard.clean_choice(_card(), raw, decision)["state"] == state


def test_an_ask_holds_the_card_at_pending():
    choice = qboard.clean_choice(_card(), {"nbla": True, "state": "approve", "ask": "  keep 'Ni siquiera'  "})
    assert choice["state"] == "pending" and choice["ask"] == "keep 'Ni siquiera'"


def test_hand_is_only_for_scripture():
    assert qboard.clean_choice(_card(), {"hand": True, "state": "approve"})["hand"] is True
    assert qboard.clean_choice(_card(status="not_scripture"), {"hand": True})["hand"] is False


@pytest.mark.parametrize("change", [{"state": "reject"}, {"state": "pending"}, {"hand": True}])
def test_only_an_approved_card_writes(change):
    choice = {**qboard.default_choice(_card()), **change}
    assert qboard.card_edits(_card(), choice) == []
    assert [e["what"] for e in qboard.ticked_edits(_card(), choice)] == ["quote"]
