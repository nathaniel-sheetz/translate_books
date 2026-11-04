"""The English reference parser and the Spanish reference form."""

from pathlib import Path

import pytest

from src.quotations.refs import (
    book_number,
    find_es_refs,
    find_refs,
    parse_ref,
    range_separator,
    spanish_abbreviations,
    BOOKS,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "text, key, spanish",
    [
        ("Heb. 11:6", "58 11:6", "He 11:6"),
        ("2 Pet. 3:8", "61 3:8", "2 P 3:8"),
        ("Ps 95:3", "19 95:3", "Sal 95:3"),
        ("Psalm 23:1", "19 23:1", "Sal 23:1"),
        ("Prov. 8:17", "20 8:17", "Pr 8:17"),
        ("John 10:5", "43 10:5", "Jn 10:5"),
        ("1 Jn 4:7,8", "62 4:7,4:8", "1 Jn 4:7,8"),
        ("1 Co 11:26", "46 11:26", "1 Co 11:26"),
        ("Job 26:6, 38:17", "18 26:6,38:17", "Job 26:6, 38:17"),
        ("Rom. 3:10–12", "45 3:10,3:11,3:12", "Ro 3:10–12"),
        ("Ezek. 36:22, 26–27", "26 36:22,36:26,36:27", "Ez 36:22, 26–27"),
        ("Rev. 3:5, 13:8, 20:12,15", "66 3:5,13:8,20:12,20:15", "Ap 3:5, 13:8, 20:12,15"),
        ("Song of Solomon 2:1", "22 2:1", "Cnt 2:1"),
        ("Ps. xxiii. 1", "19 23:1", "Sal 23:1"),
        ("Jude 3", "65 1:3", "Jud 3"),
        ("Psalm 23", "19 23", "Sal 23"),
    ],
)
def test_parse_ref(text, key, spanish):
    ref = parse_ref(text)
    assert ref is not None
    assert ref.key == key
    assert ref.spanish() == spanish


def test_parse_ref_takes_first_of_a_list_and_drops_notes():
    assert parse_ref("Paraphrase of 1 Kings 8:27 / 2 Chronicles 6:18").key == "11 8:27"
    assert parse_ref("James 3:2 (paraphrase)").key == "59 3:2"
    assert parse_ref("cf. Isaiah 57:15").key == "23 57:15"


@pytest.mark.parametrize("text", ["", "Not biblical (dialogue)", "Hymn 4:2", "John"])
def test_parse_ref_refuses(text):
    assert parse_ref(text) is None


def test_spanish_keeps_the_books_range_separator():
    assert parse_ref("John 21:15-17").spanish("–") == "Jn 21:15–17"
    assert parse_ref("John 21:15–17").spanish() == "Jn 21:15–17"


def test_canonical_english():
    assert parse_ref("Rom. 3:10–12").english == "Romans 3:10-12"
    assert parse_ref("Job 26:6, 38:17").english == "Job 26:6; 38:17"


def test_every_book_has_an_abbreviation():
    abbrevs = spanish_abbreviations()
    assert len(BOOKS) == 66
    for _num, _en, es in BOOKS:
        assert es in abbrevs
        assert not abbrevs[es].endswith(".")


@pytest.mark.parametrize("name", ["Gen.", "gen", "1John", "I Cor.", "First Peter", "Philem", "Revelations"])
def test_book_number_aliases(name):
    assert book_number(name) is not None


def test_find_refs_in_prose():
    text = (
        "He that cometh to God must believe that he is.—Heb. 11:6\n"
        "Hell is naked before him (Job 26:6, 38:17). See also Ps. xxiii. 1, "
        "and the time is 3:30. Is 40:12 is not read as Isaiah."
    )
    found = [f.ref.text for f in find_refs(text)]
    assert found == ["Heb. 11:6", "Job 26:6, 38:17", "Ps. xxiii. 1"]
    first = find_refs(text)[0]
    assert text[first.start : first.end] == "Heb. 11:6"


def test_find_refs_needs_a_capital():
    assert find_refs("we read john 3:16 aloud") == []


def test_find_es_refs():
    text = "_Yo soy el buen pastor._—Jn 10:11. Lean Juan 3:16 y Heb. 11:6."
    found = find_es_refs(text)
    assert [f.ref.key for f in found] == ["43 10:11", "43 3:16", "58 11:6"]
    assert [f.ref.spanish() for f in found] == ["Jn 10:11", "Jn 3:16", "He 11:6"]


def test_range_separator():
    assert range_separator("Jn 21:15–17") == "–"
    assert range_separator("Jn 21:15-17") == "-"
    assert range_separator("Jn 3:16") is None


SHORT_SERMONS = REPO / "projects" / "short-sermons-to-little-children" / "source.txt"


@pytest.mark.skipif(not SHORT_SERMONS.exists(), reason="short-sermons is not on this machine")
def test_every_reference_in_short_sermons_parses():
    """Local only: the book the parser was widened for. Each written reference resolves."""
    import re

    text = SHORT_SERMONS.read_text(encoding="utf-8")
    found = find_refs(text)
    assert len(found) >= 190
    # Anything that looks like "Abbrev. 1:2" is caught by find_refs.
    loose = re.findall(r"(?:\b[1-3] )?[A-Z][a-z]+\.? \d+:\d+", text)
    covered = {text[f.start : f.end].split(",")[0].split("–")[0] for f in found}
    missed = [m for m in loose if not any(c.startswith(m) or m.startswith(c) for c in covered)]
    assert missed == []
    for f in found:
        assert f.ref.verses, f.ref.text
