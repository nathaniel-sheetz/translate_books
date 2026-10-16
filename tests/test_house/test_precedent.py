"""Precedent from sibling books, the pre-fills it drives, and the divergence it reports."""

from __future__ import annotations

from src.house import precedent, prefill
from tests.test_house.conftest import make_book, pair

DIALECT_Q = {"id": "dialect", "question": "Dialect?", "options": [
    {"label": "Mexican Spanish"}, {"label": "Castilian Spanish"},
]}
UNITS_Q = {"id": "measurement_handling", "question": "Units?", "options": [
    {"label": "Keep imperial"}, {"label": "Convert to metric"},
]}
NAMES_Q = {"id": "person_name_handling", "question": "Names?", "options": [
    {"label": "Keep original personal names unchanged"}, {"label": "Translate all personal names"},
]}


def test_settles_preanswers_dialect_with_the_house_reason(house):
    book = make_book(house, "plain")
    out = prefill.style_prefills(book, [DIALECT_Q], locale="es-ES")
    assert out["dialect"]["prefilled"] == "mexican_spanish"
    assert out["dialect"]["prefilled_reason"] == "house rule `mexican-spanish`"
    # The locale disagrees, and says so beside the question.
    assert out["dialect"]["divergence"][0]["answer"] == "castilian_spanish"


def test_without_a_house_rule_the_question_is_asked(house, no_house_rules):
    out = prefill.style_prefills(make_book(house, "plain"), [DIALECT_Q, NAMES_Q])
    assert out == {}


def test_series_siblings_prefill_and_disagreements_show(house):
    make_book(house, "hardy-1", answers={"person_name_handling": 0, "measurement_handling": "convert_to_metric"})
    make_book(house, "hardy-3", answers={"person_name_handling": "translate_all_personal_names"})
    book = make_book(house, "hardy-2")
    prec = precedent.build_precedent(book)
    out = prefill.style_prefills(book, [NAMES_Q, UNITS_Q], precedent=prec)

    names = out["person_name_handling"]
    assert names["source"] == "series" and len(names["divergence"]) == 1
    # A house rule outranks the siblings, and their answer is the divergence.
    units = out["measurement_handling"]
    assert units["prefilled"] == "keep_imperial" and units["rule_id"] == "keep-imperial"
    assert units["divergence"][0]["answer"] == "convert_to_metric"


def test_lexicon_and_precedent_prefill_only_terms_present(house):
    make_book(house, "old-horse",
              glossary=[{"english": "halter", "spanish": "jáquima"},
                        {"english": "pasture", "spanish": "potrero"},
                        {"english": "Misty", "spanish": "Misty", "type": "character"}],
              chunks=[("Into the stall went the halter and the stall door.", "En la casilla, la jáquima y la casilla.")])
    make_book(house, "other-horse", glossary=[{"english": "halter", "spanish": "ronzal"}])
    book = make_book(house, "new-horse", source="He hung the halter by the stall. Misty and the deer watched.")
    prec = precedent.build_precedent(book)
    filled, candidates, divergence = prefill.glossary_prefills(book, book.joinpath("source.txt").read_text(), prec)

    by_english = {p["english"]: p for p in filled}
    assert set(by_english) == {"halter", "stall", "deer"}  # not pasture: absent; not Misty: a domain's cast
    assert by_english["stall"]["translation"] == "casilla"
    assert by_english["stall"]["origin"] == "house lexicon (group:horses)"
    assert "old-horse: stall ×2 → casilla ×2" in by_english["stall"]["evidence"]
    assert candidates == []  # gobbler has no rendering, but is not in this source
    assert {(d["english"], d["other"]) for d in divergence} == {("halter", "ronzal")}


def test_address_pairs_come_from_series_siblings_for_characters_present(house):
    make_book(house, "hardy-1", pairs=[
        pair("Frank Hardy", "Fenton Hardy", "tú", "tú"),
        pair("Frank Hardy", "Red Hawkins", "usted", "tú"),
    ], global_rules="tú among the boys")
    make_book(house, "hardy-3", pairs=[pair("Fenton Hardy", "Frank Hardy", "usted", "tú")])
    book = make_book(house, "hardy-2", source="Frank looked at Fenton Hardy. The red barn was old.")
    prec = precedent.build_precedent(book)

    kept, divergence = precedent.series_pairs(prec)
    assert [(p["a"], p["b"]) for p in kept] == [("Frank Hardy", "Fenton Hardy")]  # no Red Hawkins
    assert prec["address"]["global_rules"] == [{"from": "hardy-1", "text": "tú among the boys"}]
    assert divergence and divergence[0]["direction"] == "Fenton Hardy → Frank Hardy"


def test_domain_siblings_share_no_address_pairs(house):
    make_book(house, "old-horse", pairs=[pair("Paul", "Grandpa", "tú", "tú")])
    book = make_book(house, "new-horse", source="Paul and Grandpa talked.")
    assert precedent.build_precedent(book)["address"]["pairs"] == []


def test_snapshot_is_reused_until_rebuilt(house):
    make_book(house, "old-horse", glossary=[{"english": "halter", "spanish": "jáquima"}])
    book = make_book(house, "new-horse", source="the halter")
    first = precedent.load_precedent(book)
    make_book(house, "old-horse", glossary=[{"english": "halter", "spanish": "ronzal"}])
    assert precedent.load_precedent(book) == first
    assert precedent.leading_rendering(
        precedent.load_precedent(book, rebuild=True)["glossary"]["halter"]
    )["spanish"] == "ronzal"


def test_overrides_from_answers_names_the_rule(house):
    book = make_book(house, "plain")
    pre = prefill.style_prefills(book, [DIALECT_Q])
    assert prefill.overrides_from_answers(pre, {"dialect": "castilian_spanish"}, [DIALECT_Q]) == [
        {"rule_id": "mexican-spanish", "question": "dialect", "answer": "Castilian Spanish"},
    ]
    assert prefill.overrides_from_answers(pre, {"dialect": "Mexican Spanish"}, [DIALECT_Q]) == []
