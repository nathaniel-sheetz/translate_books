"""House rules: scope, enforce mode, triggers, book `off` overrides, `settles`."""

from __future__ import annotations

from src.judges import context
from tests.test_house.conftest import make_book


def _ids(rules):
    return [r["id"] for r in rules]


def test_translator_gets_prompt_rules_only(house):
    book = make_book(house, "plain")
    ids = _ids(context.rules_for(book, None, audience=context.AUDIENCE_TRANSLATOR))
    assert "mexican-spanish" in ids and "old-surname" in ids
    assert "rio-no-tilde" not in ids  # mechanical: fixed at commit
    assert "keep-imperial" not in ids and "adult-usted" not in ids  # draft-only


def test_decisions_get_every_rule_in_scope(house):
    ids = _ids(context.rules_for(make_book(house, "plain"), None, audience=context.AUDIENCE_DECISIONS))
    assert {"rio-no-tilde", "keep-imperial", "adult-usted"} <= set(ids)


def test_group_scope_reaches_only_group_members(house):
    assert "the-xes" not in _ids(context.house_rules_for_book(make_book(house, "plain")))
    assert "the-xes" in _ids(context.house_rules_for_book(make_book(house, "hardy-1")))
    assert "the-xes" not in _ids(context.house_rules_for_book(None))


def test_triggered_rule_needs_its_trigger_in_the_source(house):
    book = make_book(house, "plain")
    without = context.rules_for(book, "The old man walked home.", audience=context.AUDIENCE_TRANSLATOR)
    with_it = context.rules_for(book, "Old Dawson walked home.", audience=context.AUDIENCE_TRANSLATOR)
    assert "old-surname" not in _ids(without)
    assert "old-surname" in _ids(with_it)
    assert "mexican-spanish" in _ids(without)  # global rules always stay


def test_off_override_drops_a_rule_from_translator_and_judge(house):
    book = make_book(house, "plain", source="Old Dawson said nothing.")
    assert "Only tú" in context.load_style_rules(book)
    assert context.set_book_override(book, "no-vos", reason="this book uses vosotros on purpose")
    assert not context.set_book_override(book, "no-vos")  # idempotent
    assert "no-vos" not in _ids(context.rules_for(book, None, audience=context.AUDIENCE_TRANSLATOR))
    assert "Only tú" not in context.load_style_rules(book)
    assert context.load_book_rules(book) == []  # an override is not a rule of the book's own
    assert context.load_book_overrides(book)["no-vos"]["reason"].startswith("this book")
    assert context.set_book_override(book, "no-vos", off=False)
    assert "Only tú" in context.load_style_rules(book)


def test_judge_rules_filter_triggers_by_the_book_source(house):
    with_old = make_book(house, "a", source="Old Dawson came by.")
    without = make_book(house, "b", source="Nobody came by.")
    assert '"old-surname"' in context.load_style_rules(with_old)
    assert '"old-surname"' not in context.load_style_rules(without)


def test_settled_by_house_respects_off(house):
    book = make_book(house, "plain")
    assert context.settled_by_house(book, "style")["dialect"]["rule_id"] == "mexican-spanish"
    assert context.settled_by_house(book, "address")["global_rules"]["rule_id"] == "adult-usted"
    context.set_book_override(book, "mexican-spanish")
    assert "dialect" not in context.settled_by_house(book, "style")


def test_invalid_regex_trigger_never_fires():
    from src.house.match import trigger_matches

    assert not trigger_matches("re:([unclosed", "anything at all")
    assert trigger_matches("stall", "The Stalls were empty")  # plain: glossary matching
