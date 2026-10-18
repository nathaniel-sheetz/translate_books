"""The whole-book scan for speech and framing that needs its own style rule."""

from __future__ import annotations

import pytest

from src.harness import speech_hotspots as hs
from src.house.prompt import TranslatorHouse

PLAIN = "Frank looked at the map. \"We'll leave at dawn,\" said Joe. The road ran north. " * 20
FARMERS = (
    "An ancient Ford came by with an enormous negro at the wheel. Beside him sat another "
    "colored man.\n\n"
    "\"Ef dey runs oveh any moah of mah chickens, Ah'll folley 'em fum heah,\" he declared.\n\n"
    "\"White trash!\" shouted the other."
)
PROSPECTOR = (
    "\"I'm comin' to that. We was huntin' gold an' prospectin' all year, an' nothin' "
    "doin', I reckon, till one mornin' we struck it, nigh on yonder ridge.\" " * 6
)


def _book(tmp_path, chapters: list[str]):
    book = tmp_path / "book"
    (book / "chapters").mkdir(parents=True)
    for i, text in enumerate(chapters, 1):
        (book / "chapters" / f"chapter_{i:02d}.txt").write_text(text, encoding="utf-8")
    return book


@pytest.fixture
def no_house(monkeypatch):
    monkeypatch.setattr(
        "src.house.prompt.load_translator_house", lambda project_dir: TranslatorHouse((), ())
    )


def test_framing_chapters_come_first_then_unusual_dialect(tmp_path):
    book = _book(tmp_path, [PLAIN, PROSPECTOR, PLAIN, PLAIN, FARMERS + "\n\n" + PLAIN])
    rows = hs.speech_hotspots(book)
    assert [r["chapter_id"] for r in rows] == ["chapter_05", "chapter_02"]
    assert rows[0]["framing"] == {"negro": 1, "colored": 1, "white trash": 1}
    assert any("enormous negro" in line for line in rows[0]["lines"])
    assert rows[1]["framing"] == {} and rows[1]["dialect_per_1k"] > 5


def test_dialect_throughout_the_book_is_not_a_hotspot(tmp_path):
    # The opening sample already shows a book written in dialect.
    assert hs.speech_hotspots(_book(tmp_path, [PROSPECTOR] * 4)) == []


def test_colored_needs_a_person_noun():
    assert hs._framing("the colored lights of the fair") == {}
    assert hs._framing("two colored men") == {"colored": 1}


def test_format_names_each_chapter_and_its_signals(tmp_path):
    block = hs.format_hotspots(hs.speech_hotspots(_book(tmp_path, [PLAIN, FARMERS])))
    assert block.startswith("BOOK-WIDE SPEECH AND FRAMING HOTSPOTS")
    assert "- chapter_02: negro ×1, colored ×1, white trash ×1, dialect" in block
    assert hs.format_hotspots([]) == ""


def test_framing_warning_fires_only_without_a_rule(tmp_path, no_house):
    book = _book(tmp_path, [PLAIN, FARMERS])
    warning = hs.framing_rule_warning(book, "THE TRAIN PORTER\nPlain Spanish for the porter.")
    assert warning.startswith("REVIEW:") and "chapter_02" in warning
    assert hs.framing_rule_warning(book, "Render racialized asides neutrally; never a slur.") == ""
    assert hs.framing_rule_warning(_book(tmp_path / "plain", [PLAIN]), "no rule") == ""


def test_a_house_rule_on_framing_is_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.house.prompt.load_translator_house",
        lambda project_dir: TranslatorHouse(({"id": "x", "rule": "Render racial framing neutrally."},), ()),
    )
    assert hs.framing_rule_warning(_book(tmp_path, [PLAIN, FARMERS]), "no rule here") == ""
