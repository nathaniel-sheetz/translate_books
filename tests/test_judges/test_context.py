"""Tests for src/judges/context.py — the house + per-book style-rule merge.

``load_style_rules`` had no direct unit test before the house rules moved into
``prompts/house_style_rules.json``, which is most of why that merge is worth
pinning: it is the single choke point every judge, the audit panel and the
triage pass read their rules through.

Every test pins ``HOUSE_STYLE_RULES_FILE`` at a fixture. Asserting against the
real house file would turn any edit of an actual rule into a test failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.judges import context

HOUSE = {
    "version": 1,
    "every_book": [
        {"id": "house-one", "rule": "House rule one.", "note": "House note."},
        {"id": "house-two", "rule": "House rule two."},
    ],
    "where_the_guide_agrees": [
        {"id": "opt-in", "rule": "Adopted per book."},
    ],
}


@pytest.fixture
def house(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "house_style_rules.json"
    path.write_text(json.dumps(HOUSE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", path)
    return path


def _book(tmp_path, rules) -> Path:
    book = tmp_path / "book"
    book.mkdir(exist_ok=True)
    if rules is not None:
        (book / "style_rules.json").write_text(
            json.dumps({"version": 1, "rules": rules}, ensure_ascii=False),
            encoding="utf-8",
        )
    return book


def test_book_with_no_sidecar_still_gets_the_house_rules(house, tmp_path):
    rendered = context.load_style_rules(_book(tmp_path, None))
    assert '- "house-one": House rule one. (House note.)' in rendered
    assert '- "house-two": House rule two.' in rendered


def test_house_rules_come_first_then_the_book_s_own(house, tmp_path):
    book = _book(tmp_path, [{"id": "book-one", "rule": "Book rule."}])
    rendered = context.load_style_rules(book)
    assert rendered.index('"house-one"') < rendered.index('"book-one"')


def test_collision_keeps_house_wording_and_takes_the_book_s_note(house, tmp_path):
    book = _book(
        tmp_path,
        [{"id": "house-one", "rule": "Book's divergent wording.", "note": "Book note."}],
    )
    rendered = context.load_style_rules(book)
    # The house file's contract: ids and wording stay identical across books so
    # findings aggregate by rule, while a book's note may vary.
    assert '- "house-one": House rule one. (Book note.)' in rendered
    assert "Book's divergent wording." not in rendered
    assert rendered.count('"house-one"') == 1


def test_collision_without_a_book_note_keeps_the_house_note(house, tmp_path):
    book = _book(tmp_path, [{"id": "house-one", "rule": "Whatever."}])
    assert '- "house-one": House rule one. (House note.)' in context.load_style_rules(book)


def test_where_the_guide_agrees_is_not_injected(house, tmp_path):
    assert '"opt-in"' not in context.load_style_rules(_book(tmp_path, None))


def test_a_book_opts_in_by_copying_the_rule(house, tmp_path):
    book = _book(tmp_path, [{"id": "opt-in", "rule": "Adopted per book."}])
    assert '- "opt-in": Adopted per book.' in context.load_style_rules(book)


def test_malformed_sidecar_still_delivers_the_house_rules(house, tmp_path):
    book = tmp_path / "book"
    book.mkdir(exist_ok=True)
    (book / "style_rules.json").write_text("{not json", encoding="utf-8")
    rendered = context.load_style_rules(book)
    assert '"house-one"' in rendered


def _no_house(tmp_path, monkeypatch) -> None:
    """Neither the user's copy nor the example. Both must go: patching only the
    user path would silently fall back to the repo's real example file, and the
    test would assert nothing."""
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(
        context, "HOUSE_STYLE_RULES_EXAMPLE_FILE", tmp_path / "absent.example.json"
    )


def test_missing_house_file_still_delivers_the_book_s_rules(tmp_path, monkeypatch):
    _no_house(tmp_path, monkeypatch)
    book = _book(tmp_path, [{"id": "book-one", "rule": "Book rule."}])
    assert context.load_style_rules(book) == '- "book-one": Book rule.'


def test_both_absent_is_empty(tmp_path, monkeypatch):
    _no_house(tmp_path, monkeypatch)
    assert context.load_style_rules(_book(tmp_path, None)) == ""


def test_falls_back_to_the_example_when_there_is_no_user_copy(tmp_path, monkeypatch):
    """A fresh clone has only the example, and must still judge against it."""
    example = tmp_path / "house.example.json"
    example.write_text(
        json.dumps({"every_book": [{"id": "from-example", "rule": "Shipped default."}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_EXAMPLE_FILE", example)
    assert context._house_rules_path() == example
    assert '- "from-example": Shipped default.' in context.load_style_rules(
        _book(tmp_path, None)
    )


def test_the_user_copy_wins_over_the_example(tmp_path, monkeypatch):
    mine = tmp_path / "house.json"
    mine.write_text(
        json.dumps({"every_book": [{"id": "mine", "rule": "My copy."}]}), encoding="utf-8"
    )
    example = tmp_path / "house.example.json"
    example.write_text(
        json.dumps({"every_book": [{"id": "shipped", "rule": "Shipped."}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", mine)
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_EXAMPLE_FILE", example)
    rendered = context.load_style_rules(_book(tmp_path, None))
    assert '"mine"' in rendered
    assert '"shipped"' not in rendered


def test_has_book_rules_ignores_the_house_set(house, tmp_path):
    assert context.has_book_rules(_book(tmp_path, None)) is False
    assert context.has_book_rules(_book(tmp_path, [{"id": "x", "rule": "y"}])) is True


def test_the_shipped_house_file_is_usable():
    """A hand-edit that breaks this file breaks every book at once.

    Reads whichever path actually resolves: CI clones have only the example.
    """
    data = json.loads(context._house_rules_path().read_text(encoding="utf-8"))
    every = data["every_book"]
    agrees = data.get("where_the_guide_agrees", [])
    assert every, "every_book must not be empty"
    ids = [r["id"] for r in every + agrees]
    assert len(ids) == len(set(ids)), "rule ids must be unique across both sections"
    for rule in every + agrees:
        assert rule.get("id", "").strip(), rule
        assert rule.get("rule", "").strip(), rule
