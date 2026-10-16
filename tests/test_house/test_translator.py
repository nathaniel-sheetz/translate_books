"""The translator's house blocks, mechanical fixes and the save check's house rule."""

from __future__ import annotations

import json

from src.api_translator import (
    _CACHE_PREFIX_SPLIT_MARKER,
    apply_translation,
    build_translation_prompt,
    build_translation_prompt_parts,
    place_house_blocks,
)
from src.house.mechanical import FIXERS, apply_fixes, fix_translation, introduced_hits
from src.house.prompt import TranslatorHouse, load_translator_house
from src.models import Chunk, ChunkMetadata, Glossary, GlossaryTerm
from tests.test_house.conftest import make_book

RIO = [("rio-no-tilde", FIXERS["rio_no_tilde"])]


def _chunk(text: str, i: int = 0) -> Chunk:
    words = len(text.split())
    return Chunk(
        id=f"chapter_01_chunk_{i:03d}", chapter_id="chapter_01", position=i, source_text=text,
        metadata=ChunkMetadata(char_start=0, char_end=len(text), overlap_start=0,
                               overlap_end=0, paragraph_count=1, word_count=words),
    )


def test_prefix_is_identical_and_notes_follow_the_trigger(house):
    book_house = load_translator_house(make_book(house, "plain"))
    glossary = Glossary(terms=[GlossaryTerm(english="stall", spanish="casilla")])
    plain = _chunk("The horse stood in the stall all night.", 0)
    triggered = _chunk("Old Dawson led the mare out.", 1)

    p1, s1 = build_translation_prompt_parts(plain, glossary=glossary, house=book_house)
    p2, s2 = build_translation_prompt_parts(triggered, glossary=glossary, house=book_house)

    assert p1 == p2, "the cached prefix must be byte-identical across chunks"
    assert "HOUSE RULES" in p1 and "Write Mexican Spanish." in p1
    assert "Old + surname" not in p1  # triggered rules never sit in the prefix
    assert "HOUSE NOTES" not in s1 and "HOUSE NOTES" in s2
    assert "Old + surname is el viejo" in s2
    assert "casilla" in s1 and "casilla" not in s2  # the glossary filter, unchanged
    for prompt in (p1 + s1, p2 + s2):
        assert "rió" not in prompt and "Keep imperial" not in prompt


def test_no_house_renders_no_house_blocks():
    prompt = build_translation_prompt(_chunk("Old Dawson led the mare out."))
    assert "HOUSE RULES" not in prompt and "HOUSE NOTES" not in prompt


def test_a_template_without_slots_still_gets_both_blocks():
    sep = "=" * 80
    template = f"{sep}\nSTYLE\n{sep}\n\nx\n\n{_CACHE_PREFIX_SPLIT_MARKER}\n\ng\n\n{sep}\nSOURCE TEXT TO TRANSLATE\n{sep}\n\ns\n"
    placed = place_house_blocks(template, template, "RULES-BLOCK", "NOTES-BLOCK")
    assert placed.index("RULES-BLOCK") < placed.index(_CACHE_PREFIX_SPLIT_MARKER)
    assert placed.index(_CACHE_PREFIX_SPLIT_MARKER) < placed.index("NOTES-BLOCK") < placed.index("SOURCE TEXT")


def test_rio_fix_leaves_sonrio_crio_and_rio_alone():
    text = "—Claro —rió Ben. Rió otra vez, sonrió y se crió junto al río."
    fixed, changes = apply_fixes(text, RIO)
    assert fixed == "—Claro —rio Ben. Rio otra vez, sonrió y se crió junto al río."
    assert sorted((c["before"], c["after"], c["count"]) for c in changes) == [
        ("Rió", "Rio", 1), ("rió", "rio", 1),
    ]


def test_commit_applies_and_logs_the_book_fixes(house):
    book = make_book(house, "plain")
    chunk = apply_translation(_chunk("He laughed."), "—Sí —rió.", project_dir=book, writer="test")
    assert chunk.translated_text == "—Sí —rio."
    rows = [json.loads(line) for line in (book / "house_fixes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["rule_id"] == "rio-no-tilde" and rows[0]["chunk_id"] == chunk.id


def test_a_book_that_turned_the_rule_off_keeps_its_text(house):
    from src.judges.context import set_book_override

    book = make_book(house, "plain")
    set_book_override(book, "rio-no-tilde")
    assert fix_translation("—rió.", project_dir=book, chunk_id="c", writer="t") == "—rió."
    assert not (book / "house_fixes.jsonl").exists()


def test_save_check_flags_a_reintroduced_rio_only():
    from src import save_check

    hits = save_check.check_write("", "—Sí —rio Ben.", "—Sí —rió Ben.", {}, house_fixers=RIO)
    assert {"rule": "house_rule", "text": "rió"} in hits
    assert not introduced_hits("—rió ya.", "—rió ya, dijo.", RIO)  # not new in this edit
    assert not save_check.check_write("", "a", "—rió", {}, house_fixers=RIO, disabled_rules=["house_rule"])


def test_italics_rule_rides_only_in_chunks_with_underscore_italics():
    """The shipped example: the italics instruction is a triggered house rule."""
    house = load_translator_house(None)  # the example house rules (autouse fixture)
    italic = build_translation_prompt(_chunk("She was _so_ tired."), house=house)
    plain = build_translation_prompt(_chunk("She was so tired."), house=house)
    image = build_translation_prompt(_chunk("[IMAGE:the_old_mill.jpg] She slept."), house=house)
    assert "wrap the matching Spanish words in the same underscores" in italic
    assert "underscores" not in plain and "underscores" not in image


def test_house_block_lines_carry_rule_and_note():
    house = TranslatorHouse(global_rules=({"id": "x", "rule": "Do this.", "note": "Like so."},))
    assert "- Do this. Like so." in house.rules_block()
    assert house.notes_block("anything") == ""
