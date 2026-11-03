"""Revisions: the user asks for other wording on the board, the agent writes a
mapping, and ``revise`` validates it and rebuilds that one card.

Same made-up book and Bible as test_apply.
"""

import json

from src.quotations import apply as qapply
from src.quotations import board as qboard
from src.quotations import map as qmap
from src.quotations import revise as qrevise
from tests.test_quotations.test_apply import book  # noqa: F401  (fixture)


def _card(root):
    return qboard.load_mapped(root)["cards"][0]


def _ask(root, text="keep 'luz del mundo' but say 'Yo soy'"):
    key = _card(root)["key"]
    qboard.save(root, {key: {**qboard.default_choice(_card(root)), "ask": text}})
    return key


def test_asks_lists_what_the_agent_needs(book):  # noqa: F811
    key = _ask(book)
    out = qrevise.asks(book)
    assert out["status"] == "ok" and [a["key"] for a in out["asks"]] == [key]
    ask = out["asks"][0]
    assert ask["ask"].startswith("keep") and ask["applied"] is False
    assert ask["rows"][0]["es"].startswith("Jesús dijo")
    assert ask["verses"][0]["key"] == "43:008:012"


def test_a_revision_with_pieces_is_built_by_code(book):  # noqa: F811
    key = _ask(book)
    qboard.approve(book)
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino"]}]})
    assert out["status"] == "ok", out
    card = _card(book)
    assert card["revised"]["ask"].startswith("keep") and not card.get("adapted")
    assert card["quote_edits"][0]["new"] == "Yo soy la luz del camino"
    choice = qboard.load(book)["choices"][key]
    assert choice["state"] == "pending" and choice["ask"] == "" and choice["nbla"] is True
    assert qboard.load(book)["approved"] is None
    assert key in json.loads(qmap.revisions_path(book).read_text(encoding="utf-8"))
    assert qboard.recommend(card)["rule"] == "revised"


def test_a_revision_keeps_the_toss_up_tick(book):  # noqa: F811
    key = _card(book)["key"]
    qboard.save(book, {key: {**qboard.default_choice(_card(book)), "ask": "otra", "toss_up": True}})
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino"]}]})
    assert out["status"] == "ok", out
    assert qboard.load(book)["choices"][key]["toss_up"] is True


def test_a_revision_may_only_remove_the_marks_and_counts_nothing(book):  # noqa: F811
    key = _ask(book, "remove the quotation marks")
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "«Yo soy la luz del mundo»", "new": "Yo soy la luz del mundo", "pieces": []}]})
    assert out["status"] == "ok", out
    edit = _card(book)["quote_edits"][0]
    assert edit["new"] == "Yo soy la luz del mundo"
    assert edit["pieces"] == [] and edit["verse_keys"] == [] and edit["nbla_words"] == 0


def test_a_revision_with_no_pieces_may_change_only_the_marks(book):  # noqa: F811
    key = _ask(book, "otra")
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "«Yo soy la luz del mundo»", "new": "Yo soy la luz del camino", "pieces": []}]})
    assert out["status"] == "refused" and out["refusals"][0]["reason"] == qmap.REFUSE_EMPTY
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "«Yo soy la luz del mundo»", "pieces": []}]})
    assert out["status"] == "refused"


def test_a_revision_may_use_its_own_words_around_the_nbla(book):  # noqa: F811
    key = _ask(book)
    out = qrevise.revise(book, key, {"replace": [{
        "es_idx": 0, "old": "Yo soy la luz del mundo",
        "pieces": ["la luz del camino"], "new": "Yo mismo soy la luz del camino"}]})
    assert out["status"] == "ok", out
    card = _card(book)
    assert card["quote_edits"][0]["new"] == "Yo mismo soy la luz del camino"
    assert card["adapted"] is True and card["why"].startswith("revised: ")


def test_own_words_must_keep_every_nbla_piece(book):  # noqa: F811
    key = _ask(book)
    before = qmap.mapped_path(book).read_text(encoding="utf-8")
    out = qrevise.revise(book, key, {"replace": [{
        "es_idx": 0, "old": "Yo soy la luz del mundo",
        "pieces": ["la luz del camino"], "new": "Yo soy la lumbre del camino"}]})
    assert out["status"] == "refused"
    assert out["refusals"][0]["reason"] == qmap.REFUSE_PIECE_NOT_IN_NEW
    assert qmap.mapped_path(book).read_text(encoding="utf-8") == before
    assert not qmap.revisions_path(book).exists()


def test_a_piece_that_is_not_nbla_is_refused(book):  # noqa: F811
    key = _ask(book)
    out = qrevise.revise(book, key, {"replace": [
        {"es_idx": 0, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del universo"]}]})
    assert out["status"] == "refused" and out["refusals"][0]["reason"] == qmap.REFUSE_PIECE


def test_a_revision_may_not_overlap_another_card(book):  # noqa: F811
    doc = qboard.load_mapped(book)
    doc["cards"].append({
        "key": "chapter_01|other|x", "chapter_id": "chapter_01", "kind": "quotation", "status": "proposed",
        "es_idx": [1], "quote_edits": [{"es_idx": 1, "old": "Caminen", "new": "Anden"}], "echo": [],
    })
    qmap.mapped_path(book).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    key = _ask(book)
    out = qrevise.revise(book, key, {
        "replace": [{"es_idx": 0, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino"]}],
        "echo": [{"es_idx": 1, "old": "Caminen", "new": "Anden", "en_evidence": "Walk"}],
    })
    assert out["status"] == "refused" and out["refusals"][0]["reason"] == qmap.REFUSE_OVERLAP


def test_a_revision_survives_a_recommit(book):  # noqa: F811
    key = _ask(book)
    qrevise.revise(book, key, {"replace": [{
        "es_idx": 0, "old": "Yo soy la luz del mundo",
        "pieces": ["la luz del camino"], "new": "Yo mismo soy la luz del camino"}]})
    assert qmap.map_commit(book)["status"] == "ok"
    card = _card(book)
    assert card["quote_edits"][0]["new"] == "Yo mismo soy la luz del camino" and card["revised"]


def test_an_applied_card_is_not_revised(book):  # noqa: F811
    qboard.approve(book)
    qapply.apply(book)
    out = qrevise.revise(book, _card(book)["key"], {"replace": [
        {"es_idx": 0, "old": "Yo soy la luz del camino", "pieces": ["Yo soy la luz del camino"]}]})
    assert out["status"] == "error" and "fixed by hand" in out["error"]
