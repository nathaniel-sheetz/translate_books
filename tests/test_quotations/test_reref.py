"""Re-reference: the user says a card names the wrong verse, and ``reref`` makes the
card for the right one; applying it supersedes the old card in the ledger.

Same made-up book and Bible as test_apply, with one more made-up verse.
"""

import json

import pytest

from src.quotations import apply as qapply
from src.quotations import board as qboard
from src.quotations import map as qmap
from src.quotations import record as qrecord
from src.quotations import resolve as qresolve
from src.quotations import reref as qreref
from src.quotations import revise as qrevise
from src.quotations.index import BibleIndex
from tests.test_quotations.test_apply import VERSES, book  # noqa: F401  (fixture)

OTHER = {"19:027:001": "La lámpara del viajero alumbra el sendero.",
         "19:028:001": "El río corre hacia el mar."}
INDEX2 = BibleIndex({"version": "nbla", "verses": {**VERSES, **OTHER}, "proper_words": [],
                     "smallcaps_resolved": True})
MAPPING = {"replace": [{"es_idx": 0, "old": "Yo soy la luz del camino",
                        "pieces": ["La lámpara del viajero alumbra el sendero"]}]}


@pytest.fixture
def bible(book, monkeypatch):  # noqa: F811
    monkeypatch.setattr(qmap, "load_index", lambda *_a, **_k: INDEX2)
    return book


def _cards(root):
    return {c["key"]: c for c in qboard.load_mapped(root)["cards"]}


def _old_key(root):
    return qboard.load_mapped(root)["cards"][0]["key"]


def _apply(root):
    qboard.approve(root)
    out = qapply.apply(root)
    assert out["status"] == "ok", out
    return out


def _text(root):
    return json.loads((root / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8"))["translated_text"]


def test_reref_makes_a_card_for_the_other_verse_and_touches_nothing_else(bible):
    _apply(bible)
    old = _old_key(bible)
    before = _text(bible)
    out = qreref.reref(bible, old, "Psalm 27:1", why="the NBLA words John 8:12 otherwise")
    assert out["status"] == "ok", out
    assert out["ref_es"] == "Sal 27:1" and out["old_applied"] is True
    assert out["in_book"] == ["Yo soy la luz del camino"]
    cards = _cards(bible)
    new = cards[out["key"]]
    assert new["replaces"] == old and new["origin"] == "reref" and new["status"] == qmap.CARD_UNMAPPED
    assert new["verse_keys"] == ["19:027:001"]
    # The old card, the book and the ledger are as they were.
    assert old in cards and _text(bible) == before
    assert old in qapply.applied_cards(bible)
    assert json.loads(qreref.rerefs_path(bible).read_text(encoding="utf-8"))[old]["key"] == out["key"]


def test_applying_the_new_card_supersedes_the_applied_one(bible):
    _apply(bible)
    old = _old_key(bible)
    new = qreref.reref(bible, old, "Psalm 27:1")["key"]
    assert qrevise.revise(bible, new, MAPPING)["status"] == "ok"
    qboard.save(bible, {new: {**qboard.load(bible)["choices"][new], "state": "approve"}})
    _apply(bible)

    assert "lámpara del viajero" in _text(bible) and "luz del camino" not in _text(bible)
    latest = qapply.latest_decisions(bible)
    assert latest[new]["action"] == "applied" and latest[new]["replaces"] == old
    assert latest[old]["action"] == "superseded" and latest[old]["superseded_by"] == new
    assert set(qapply.applied_cards(bible)) == {new}
    assert qapply.verify(bible)["status"] == "ok"
    record = qrecord.read_record(bible)
    assert [r["verses"] for r in record["rows"]] == [["19:027:001"]]

    state = qboard.build(bible)
    views = {c["key"]: c for c in state["chapters"][0]["cards"]}
    assert views[old]["phase"] == "applied" and views[old]["history"]["superseded_ref"] == "Psalms 27:1"
    assert views[new]["replaces_ref"] == "John 8:12"
    assert state["counts"]["superseded"] == 1 and state["counts"]["applied"] == 1

    # Applying again writes nothing more, and a superseded card is not revised.
    rows = len(qapply.read_decisions(bible))
    _apply(bible)
    assert len(qapply.read_decisions(bible)) == rows
    out = qrevise.revise(bible, old, MAPPING)
    assert out["status"] == "error" and new in out["error"]


def test_the_new_card_survives_resolve_and_map_commit(bible):
    _apply(bible)
    old = _old_key(bible)
    new = qreref.reref(bible, old, "Psalm 27:1")["key"]
    qrevise.revise(bible, new, MAPPING)
    assert qresolve.resolve(bible, index=INDEX2)["status"] == "ok"
    assert qmap.map_commit(bible)["status"] == "ok"
    cards = _cards(bible)
    assert list(cards) == [old, new]
    assert cards[new]["status"] == qmap.CARD_PROPOSED and cards[new]["revised"]


def test_an_unapplied_card_is_set_to_reject_and_claims_nothing(bible):
    old = _old_key(bible)
    out = qreref.reref(bible, old, "Psalm 27:1")
    assert out["status"] == "ok" and out["old_applied"] is False
    assert out["in_book"] == ["Yo soy la luz del mundo"]
    assert qboard.load(bible)["choices"][old]["state"] == "reject"
    new = out["key"]
    mapping = {"replace": [{**MAPPING["replace"][0], "old": "Yo soy la luz del mundo"}]}
    assert qrevise.revise(bible, new, mapping)["status"] == "ok"
    qboard.save(bible, {new: {**qboard.load(bible)["choices"][new], "state": "approve"}})
    _apply(bible)
    latest = qapply.latest_decisions(bible)
    assert latest[old]["action"] == "declined" and latest[new]["action"] == "applied"
    assert "lámpara del viajero" in _text(bible)


def test_asking_again_swaps_the_replacement_until_it_is_applied(bible):
    _apply(bible)
    old = _old_key(bible)
    first = qreref.reref(bible, old, "Psalm 27:1")["key"]
    out = qreref.reref(bible, old, "Psalm 28:1")
    assert out["status"] == "ok" and out["replaced_earlier"] == first
    assert [k for k in _cards(bible) if k != old] == [out["key"]]
    assert first not in {c["key"] for c in qmap.load_resolved(bible)["candidates"]}

    second = out["key"]
    mapping = {"replace": [{"es_idx": 0, "old": "Yo soy la luz del camino", "pieces": ["El río corre hacia el mar"]}]}
    assert qrevise.revise(bible, second, mapping)["status"] == "ok"
    qboard.save(bible, {second: {**qboard.load(bible)["choices"][second], "state": "approve"}})
    _apply(bible)
    out = qreref.reref(bible, old, "Psalm 27:1")
    assert out["status"] == "error" and "already replaced" in out["error"]


@pytest.mark.parametrize("ref, problem", [
    ("John 8:12", "already names"),
    ("Psalm 99:9", "not in the NBLA index"),
    ("no such book 3:1", "cannot read"),
    ("Psalm 27", "cannot read"),
])
def test_refusals_write_nothing(bible, ref, problem):
    before = qmap.mapped_path(bible).read_text(encoding="utf-8")
    out = qreref.reref(bible, _old_key(bible), ref)
    assert out["status"] == "error" and problem in out["error"]
    assert qmap.mapped_path(bible).read_text(encoding="utf-8") == before
    assert not qreref.rerefs_path(bible).exists()


def test_a_replacement_is_not_rerefed_itself(bible):
    new = qreref.reref(bible, _old_key(bible), "Psalm 27:1")["key"]
    out = qreref.reref(bible, new, "John 8:12")
    assert out["status"] == "error" and "replaces another" in out["error"]
