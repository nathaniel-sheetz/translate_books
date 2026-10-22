"""Chapter-scoped notes: book rules and address shifts reach only the chunks they cover."""

from __future__ import annotations

from src.api_translator import build_translation_prompt_parts
from src.house.prompt import load_translator_house
from src.house.scope import chapter_key, in_window, window_problems
from src.judges.context import format_style_rules
from src.models import Chunk, ChunkMetadata
from tests.test_house.conftest import make_book, write_json


def _chunk(text: str, chapter: str, i: int = 0) -> Chunk:
    return Chunk(
        id=f"{chapter}_chunk_{i:03d}", chapter_id=chapter, position=i, source_text=text,
        metadata=ChunkMetadata(char_start=0, char_end=len(text), overlap_start=0,
                               overlap_end=0, paragraph_count=1, word_count=len(text.split())),
    )


def _bambi(house, *, rules=None):
    book = make_book(
        house, "bambi",
        glossary=[{"english": "Karus", "spanish": "Karus", "type": "character"},
                  {"english": "the old stag", "spanish": "el viejo ciervo", "type": "character"}],
        pairs=[{
            "a": "Bambi", "b": "Karus",
            "directions": {
                "a_to_b": [
                    {"form": "tú", "when": "once he has beaten him in the fight", "since": "chapter_13"},
                    {"form": "usted", "when": "default"},
                ],
                "b_to_a": [{"form": "tú", "when": "default"}],
            },
        }],
    )
    if rules is not None:
        write_json(book / "style_rules.json", {"rules": rules})
    return book


def test_chapter_order_is_numeric_and_windows_are_inclusive():
    assert chapter_key("chapter_9") < chapter_key("chapter_10")
    assert in_window("chapter_13", None, "chapter_13")
    assert not in_window("chapter_13", None, "chapter_12")
    assert in_window(None, "chapter_03", "chapter_03") and not in_window(None, "chapter_03", "chapter_04")
    assert in_window(None, None, None)
    assert not in_window("chapter_13", None, None)  # no chapter known: a window covers nothing


def test_address_shift_reaches_only_its_chapters_and_the_prefix_never_moves(house):
    book_house = load_translator_house(_bambi(house))
    before = _chunk("Bambi faced Karus at the edge of the clearing.", "chapter_12")
    after = _chunk("Bambi faced Karus at the edge of the clearing.", "chapter_13")
    elsewhere = _chunk("Faline grazed alone.", "chapter_13")

    p1, s1 = build_translation_prompt_parts(before, house=book_house)
    p2, s2 = build_translation_prompt_parts(after, house=book_house)
    p3, s3 = build_translation_prompt_parts(elsewhere, house=book_house)

    assert p1 == p2 == p3, "the cached prefix must be byte-identical across chunks"
    assert "- Bambi → Karus: usted" in s1 and "tú once" not in s1
    assert "- Bambi → Karus: tú once he has beaten him in the fight; otherwise usted" in s2
    assert "Karus → Bambi" not in s2  # that direction has no window: the summary covers it
    assert "NOTES FOR THIS PASSAGE" not in s3  # neither character is in the passage


def test_windowed_and_triggered_book_rules_ship_where_they_apply(house):
    book = _bambi(house, rules=[
        {"id": "grown-register", "rule": "Bambi now speaks as an adult stag.", "since": "chapter_13"},
        {"id": "gobble-sound", "rule": "Render 'gobble' as glu-glu.", "triggers": ["gobble"]},
        {"id": "everyday-lexicon", "rule": "Use the everyday word."},  # global: judge-only
    ])
    book_house = load_translator_house(book)
    early = build_translation_prompt_parts(_chunk("He heard a gobble.", "chapter_02"), house=book_house)[1]
    late = build_translation_prompt_parts(_chunk("He walked on.", "chapter_14"), house=book_house)[1]

    assert "glu-glu" in early and "adult stag" not in early
    assert "adult stag" in late and "glu-glu" not in late
    assert "everyday word" not in early + late
    # No chapter known (a sentence retranslation): only the triggered rule applies.
    assert "adult stag" not in book_house.notes_block("He walked on.")


def test_judge_rule_list_marks_the_window_with_the_books_label():
    rules = [{"id": "grown-register", "rule": "Adult register.", "since": "chapter_13"},
             {"id": "plain", "rule": "Plain."}]
    text = format_style_rules(rules, {"chapter_13": "Chapter 12"})
    assert '- "grown-register": Adult register. [applies chapter_13 (Chapter 12) onward only]' in text
    assert '- "plain": Plain.' in text and "Plain. [" not in text


def test_window_problems_name_unknown_chapters_and_reversed_windows():
    problems = window_problems(
        [{"id": "a", "since": "chapter_40"}, {"id": "b", "since": "chapter_05", "until": "chapter_02"}],
        ["chapter_01", "chapter_02", "chapter_05"],
    )
    assert any("'a'" in p and "chapter_40" in p for p in problems)
    assert any("'b'" in p and "after until" in p for p in problems)


def test_editorial_judge_names_the_chapter_only_for_a_book_with_windowed_rules(house):
    from src.judges.base import JudgeTarget
    from src.judges.context import build_judge_context
    from src.judges.editorial_judge import EditorialJudge

    target = JudgeTarget(id="chapter_13_chunk_000", target_type="chunk", source_text="x",
                         translated_text="y", context={"chapter_id": "chapter_13"})
    plain_book = _bambi(house, rules=[{"id": "plain", "rule": "Plain."}])
    ctx, _ = build_judge_context(plain_book, ["editorial"], None, None)
    assert "This passage is from" not in EditorialJudge().build_prompt(target, ctx)

    windowed = _bambi(house, rules=[{"id": "late", "rule": "Late.", "since": "chapter_13"}])
    ctx, _ = build_judge_context(windowed, ["editorial"], None, None)
    prompt = EditorialJudge().build_prompt(target, ctx)
    assert "This passage is from chapter_13.\n\n<translation>" in prompt
    assert "[applies chapter_13 onward only]" in prompt
