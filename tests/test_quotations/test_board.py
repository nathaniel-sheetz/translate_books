"""The board's recommendation and choices, on hand-made cards (no project on disk)."""

import json

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
        "ref": False, "nbla": True, "echo": [], "paste": "", "state": "approve", "hand": False, "ask": "",
        "toss_up": False}
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


def test_toss_up_sits_beside_the_state_and_writes_nothing_of_its_own():
    choice = qboard.clean_choice(_card(), {"nbla": True, "state": "reject", "toss_up": True})
    assert choice["state"] == "reject" and choice["toss_up"] is True
    assert qboard.card_edits(_card(), choice) == []
    approved = {**choice, "state": "approve"}
    assert qboard.card_edits(_card(), approved) == qboard.card_edits(_card(), {**approved, "toss_up": False})


@pytest.mark.parametrize("verdict, confidence, state, rule", [
    ("apply", 0.90, "approve", "judge_apply"),
    ("apply", 0.98, "approve", "judge_apply"),
    ("apply", 0.89, "pending", "judge_hold"),
    # A confident reject often throws away a good word, so it only holds the card.
    ("reject", 0.99, "pending", "judge_hold"),
    ("partial", 0.99, "pending", "judge_hold"),
    ("toss_up", 0.99, "pending", "judge_hold"),
])
def test_only_a_confident_apply_sets_a_default(verdict, confidence, state, rule):
    card = _card(judge={"verdict": verdict, "confidence": confidence, "reason": "por esto"})
    assert qboard.recommend(card)["rule"] == rule
    assert qboard.recommend(card)["detail"] == "por esto"
    assert qboard.default_choice(card)["state"] == state


def test_a_verdict_outranks_the_coded_rules_but_not_a_refusal():
    judged = {"verdict": "apply", "confidence": 0.95, "reason": ""}
    assert qboard.recommend(_card(drift="meaning differs", judge=judged))["rule"] == "judge_apply"
    assert qboard.recommend(_card(status="refused", judge=judged))["rule"] == "refused"


@pytest.mark.parametrize("confidence, band", [(0.95, "high"), (0.90, "high"), (0.89, "medium"), (0.70, "medium"), (0.69, "low")])
def test_the_bands(confidence, band):
    assert qboard.judge_band(confidence) == band


def _judged_book(tmp_path, *cards, verdicts):
    from src.quotations import judge as qjudge

    book = _book(tmp_path, *cards)
    by_key = {c["key"]: c for c in cards}
    doc = {"verdicts": {key: {"hash": qjudge.edits_hash(by_key[key]["quote_edits"]), "reason": "r", **verdict}
                        for key, verdict in verdicts.items()}}
    qjudge.verdicts_path(book).write_text(json.dumps(doc), encoding="utf-8")
    return book


def test_a_verdict_moves_only_a_card_with_no_saved_choice(tmp_path):
    held, saved = _card(), _card(key="chapter_01|43 8:13|d")
    book = _judged_book(tmp_path, held, saved, verdicts={
        held["key"]: {"verdict": "reject", "confidence": 0.99},
        saved["key"]: {"verdict": "reject", "confidence": 0.99},
    })
    qboard.save(book, {saved["key"]: {"nbla": True, "state": "approve"}})
    doc = qboard._with_verdicts(book, qboard.load_mapped(book))
    choices = qboard.effective_choices(doc, qboard.load(book)["choices"])
    assert choices[held["key"]]["state"] == "pending"
    assert choices[saved["key"]]["state"] == "approve"


def test_a_stale_verdict_leaves_the_card_to_the_coded_rules(tmp_path):
    card = _card()
    book = _judged_book(tmp_path, card, verdicts={card["key"]: {"verdict": "reject", "confidence": 0.99}})
    changed = {**card, "quote_edits": [{"es_idx": 0, "old": "luz del mundo", "new": "otra cosa"}]}
    (book / ".harness" / "quotations" / "mapped.json").write_text(json.dumps({"cards": [changed]}), encoding="utf-8")
    doc = qboard._with_verdicts(book, qboard.load_mapped(book))
    assert "judge" not in doc["cards"][0]
    assert qboard.default_choice(doc["cards"][0])["state"] == "approve"


def test_verdicts_stay_out_of_mapped_and_the_fingerprint(tmp_path):
    from src.quotations import judge as qjudge

    card = _card()
    book = _book(tmp_path, card)
    doc = qboard.load_mapped(book)
    before = qboard.fingerprint(book, doc, {})
    mapped = (book / ".harness" / "quotations" / "mapped.json").read_bytes()
    qjudge.verdicts_path(book).write_text(json.dumps({"verdicts": {card["key"]: {
        "hash": qjudge.edits_hash(card["quote_edits"]), "verdict": "reject", "confidence": 0.9}}}), encoding="utf-8")
    judged = qboard._with_verdicts(book, doc)
    assert judged["cards"][0]["judge"]["verdict"] == "reject" and "judge" not in doc["cards"][0]
    assert qboard.fingerprint(book, qboard.load_mapped(book), {}) == before
    assert (book / ".harness" / "quotations" / "mapped.json").read_bytes() == mapped


def test_a_verdict_after_an_approval_changes_nothing_it_writes(tmp_path):
    from src.quotations import judge as qjudge

    card = _card()
    book = _book(tmp_path, card)
    qboard.approve(book)
    choices, problem = qboard.approved_choices(book)
    assert problem is None and choices[card["key"]]["state"] == "approve"
    qjudge.verdicts_path(book).write_text(json.dumps({"verdicts": {card["key"]: {
        "hash": qjudge.edits_hash(card["quote_edits"]), "verdict": "reject", "confidence": 0.99}}}), encoding="utf-8")
    choices, problem = qboard.approved_choices(book)
    assert problem is None and choices[card["key"]]["state"] == "approve"


def test_clicking_a_card_off_the_judges_default_goes_in_the_history(tmp_path):
    card = _card()
    book = _judged_book(tmp_path, card, verdicts={card["key"]: {"verdict": "reject", "confidence": 0.99}})
    # Apply is the coded default but not this card's default any more, so it's a change.
    qboard.save(book, {card["key"]: {"nbla": True, "state": "approve"}})
    assert [r["after"]["state"] for r in _history(book)] == ["approve"]


def _book(tmp_path, *cards):
    (tmp_path / ".harness" / "quotations").mkdir(parents=True)
    (tmp_path / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": list(cards)}), encoding="utf-8")
    return tmp_path


def _history(book):
    path = qboard.history_path(book)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_every_change_goes_in_the_history(tmp_path):
    book = _book(tmp_path, _card())
    key = _card()["key"]
    qboard.save(book, {key: {"nbla": True, "state": "reject"}})
    qboard.save(book, {key: {"nbla": True, "state": "approve", "toss_up": True}})
    rows = _history(book)
    assert [(r["before"] or {}).get("state") for r in rows] == [None, "reject"]
    assert [r["after"]["state"] for r in rows] == ["reject", "approve"]
    assert rows[1]["after"]["toss_up"] is True and rows[0]["key"] == key


def test_a_save_that_changes_nothing_adds_no_history(tmp_path):
    book = _book(tmp_path, _card())
    key = _card()["key"]
    qboard.save(book, {key: qboard.default_choice(_card())})
    assert _history(book) == []
    qboard.save(book, {key: {"nbla": True, "state": "reject"}})
    qboard.save(book, {key: {"nbla": True, "state": "reject"}})
    assert len(_history(book)) == 1


def test_a_choice_saved_before_toss_up_does_not_read_as_changed(tmp_path):
    book = _book(tmp_path, _card())
    key = _card()["key"]
    old = {"ref": False, "nbla": True, "echo": [], "paste": "", "state": "reject", "hand": False, "ask": ""}
    qboard.board_path(book).write_text(json.dumps({"choices": {key: old}}), encoding="utf-8")
    qboard.save(book, {key: {**old, "toss_up": False}})
    assert _history(book) == []


def test_seed_holds_only_unsaved_cards_that_change_text(tmp_path):
    clean = _card()
    decided = _card(key="chapter_01|43 8:13|d")
    no_change = _card(key="chapter_01|43 8:14|e", quote_edits=[{"es_idx": 0, "old": "x", "new": "x"}])
    matches = _card(key="chapter_01|43 8:15|f", status="matches")
    elsewhere = _card(key="chapter_02|43 8:16|g", chapter_id="chapter_02")
    book = _book(tmp_path, clean, decided, no_change, matches, elsewhere)
    qboard.save(book, {decided["key"]: {"nbla": True, "state": "reject"}})

    out = qboard.seed_pending(book, ["chapter_01"])

    assert (out["status"], out["cards"], out["seeded"], out["marked"]) == ("ok", 2, 1, 1)
    assert out["unmarked"] == [clean["key"]]
    saved = qboard.load(book)["choices"]
    assert saved[clean["key"]]["state"] == "pending" and saved[clean["key"]]["nbla"] is True
    assert saved[decided["key"]]["state"] == "reject"
    assert no_change["key"] not in saved and matches["key"] not in saved and elsewhere["key"] not in saved


@pytest.mark.parametrize("mark", [
    {"state": "approve"}, {"state": "reject"}, {"state": "pending", "ask": "keep nuestra"},
    {"state": "pending", "toss_up": True}, {"state": "approve", "hand": True},
])
def test_a_second_seed_keeps_marks_and_stops_listing_them(tmp_path, mark):
    book = _book(tmp_path, _card())
    key = _card()["key"]
    assert qboard.seed_pending(book, ["chapter_01"])["unmarked"] == [key]
    qboard.save(book, {key: {"nbla": True, **mark}})
    state = qboard.load(book)["choices"][key]["state"]

    out = qboard.seed_pending(book, ["chapter_01"])

    assert out["seeded"] == 0 and out["unmarked"] == []
    assert qboard.load(book)["choices"][key]["state"] == state


def test_seed_refuses_a_chapter_with_no_cards(tmp_path):
    out = qboard.seed_pending(_book(tmp_path, _card()), ["chapter_09"])
    assert out["status"] == "error" and "chapter_09" in out["problems"][0]
    assert not qboard.board_path(tmp_path).exists()
