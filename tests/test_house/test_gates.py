"""Groups, proposals, the consistency report and the harness gates that use them."""

from __future__ import annotations

import json

import pytest

from src.harness import flow, state
from src.house import check, groups, proposals
from tests.test_house.conftest import make_book, write_json


def test_join_requires_a_kind_for_a_new_group(house):
    with pytest.raises(ValueError, match="pass --kind"):
        groups.join_group("book", "nonesuch")
    joined = groups.join_group("book", "misty", kind="series", about="Chincoteague")
    assert joined["members"] == ["book"]
    assert groups.siblings("hardy-1")[0]["kind"] == "series"
    assert "misty" in groups.describe("book")["joined"]
    groups.leave_group("book", "misty")
    assert groups.describe("book")["joined"] == []


def test_proposals_dedupe_by_book_and_adopt_into_the_house_file(house, tmp_path, monkeypatch):
    from src.judges import context

    monkeypatch.setattr(proposals, "PROPOSALS_FILE", tmp_path / "proposals.jsonl")
    cats = [{"category": "titles-honorifics", "proposals": [
        {"rule": "Old + surname is el viejo + surname.", "subjects": ["Old Dawson"]},
    ]}]
    first = proposals.record_proposals("book-a", cats)
    assert proposals.record_proposals("book-a", cats) == []  # a re-commit adds nothing
    proposals.record_proposals("book-b", cats)
    [p] = proposals.load_proposals(status="open")
    assert p["id"] == first[0] and p["books"] == ["book-a", "book-b"]

    rule = proposals.adopt(p["id"], scope="group:hardy", triggers=["re:\\bOld [A-Z]"], rule_id="old-x")
    assert rule["scope"] == "group:hardy" and rule["settles"] == {
        "decisions:titles-honorifics": "Old + surname is el viejo + surname."}
    written = json.loads(context.HOUSE_STYLE_RULES_FILE.read_text(encoding="utf-8"))
    assert written["every_book"][-1]["id"] == "old-x"
    assert proposals.load_proposals(status="adopted")[0]["rule_id"] == "old-x"
    with pytest.raises(ValueError, match="already adopted"):
        proposals.adopt(p["id"])


def test_adopt_can_reword_a_proposal(house, tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "PROPOSALS_FILE", tmp_path / "proposals.jsonl")
    [pid] = proposals.record_proposals("book-a", [{"category": "speech-typography", "proposals": [
        {"rule": "Italics stay italics.", "subjects": ["italic emphasis"]},
    ]}])
    rule = proposals.adopt(pid, rule_text="Keep _underscore_ italics.", rule_id="italics")
    assert rule["rule"] == "Keep _underscore_ italics."
    assert rule["settles"] == {"decisions:speech-typography": "Keep _underscore_ italics."}


def test_house_check_reports_lexicon_siblings_committed_and_promotions(house):
    make_book(house, "old-horse",
              glossary=[{"english": "mare", "spanish": "yegua"}, {"english": "heat", "spanish": "serie"}],
              chunks=[("The heat was fast. The heat ended. The heat was won.", "La serie fue rápida. Terminó la serie. Ganó la serie.")])
    make_book(house, "other-horse", glossary=[{"english": "mare", "spanish": "la yegua"}])
    book = make_book(
        house, "new-horse", source="The mare left her stall for the heat.",
        glossary=[{"english": "stall", "spanish": "establo"}, {"english": "heat", "spanish": "manga"},
                  {"english": "mare", "spanish": "yegua"}],
        chunks=[("The mare stood in the stall.", "La yegua estaba en el establo.")],
    )
    report = check.run_check(book)
    stall = next(r for r in report["glossary"] if r["english"] == "stall")
    assert stall["status"] == "differs" and stall["expected"] == "casilla"
    assert report["sibling_usage"] == [
        {"english": "heat", "rendering": "manga", "siblings_without_it": ["old-horse (×3)"]},
    ]
    assert [(r["english"], r["spanish"]) for r in report["promotion_candidates"]] == [("mare", "yegua")]
    assert "# House check: new-horse" in check.render_markdown(report)


def test_committed_text_that_ignores_the_glossary_is_listed(house):
    book = make_book(
        house, "plain", source="x",
        glossary=[{"english": "saucepan", "spanish": "cacerola", "alternatives": ["olla"]}],
        chunks=[("The saucepan boiled.", "La olla hervía."), ("A saucepan fell.", "Se cayó un sartén.")],
    )
    [row] = check.check_committed(book)
    assert row["english"] == "saucepan" and row["chunks_without_rendering"] == ["chapter_01_chunk_001"]


def test_join_refuses_to_overwrite_an_unreadable_groups_file(house):
    groups.GROUPS_FILE.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        groups.join_group("book", "misty", kind="series")
    assert groups.GROUPS_FILE.read_text(encoding="utf-8") == "{bad json"


def test_adopt_checks_the_group_and_decline_takes_only_open_proposals(house, tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "PROPOSALS_FILE", tmp_path / "proposals.jsonl")
    [pid] = proposals.record_proposals("book-a", [{"category": "speech-typography", "proposals": [
        {"rule": "Italics stay italics.", "subjects": ["italic emphasis"]},
    ]}])
    for scope in ("group:", "group:nope"):
        with pytest.raises(ValueError, match="no group"):
            proposals.adopt(pid, scope=scope)
    assert proposals.adopt(pid, scope="group:hardy", rule_id="italics")["scope"] == "group:hardy"
    with pytest.raises(ValueError, match="already adopted"):
        proposals.decline(pid)


def test_house_check_takes_no_snapshot_and_reports_drift_against_one(house):
    from src.house import precedent

    make_book(house, "old-horse", glossary=[{"english": "halter", "spanish": "jáquima"}])
    book = make_book(house, "new-horse", source="the halter")
    report = check.run_check(book)
    assert not precedent.cache_path(book).exists()
    assert report["precedent_built_at"] is None and report["drift"] == []

    precedent.load_precedent(book)  # drafting takes the snapshot
    make_book(house, "old-horse", glossary=[{"english": "halter", "spanish": "ronzal"}])
    assert check.run_check(book)["drift"] == [{"english": "halter", "snapshot": "jáquima", "now": "ronzal"}]


# ── harness gates ──────────────────────────────────────────────────────────

def _harness_book(house, slug: str, source: str):
    book = make_book(house, slug, source=source)
    (book / "chapters").mkdir()
    (book / "chapters" / "chapter_01.txt").write_text(source, encoding="utf-8")
    state.save_config(book, {})
    return book


def test_prepare_questions_carries_house_prefill_and_covered_rules(house):
    book = _harness_book(house, "plain", "Old Dawson walked the five miles home.\n")
    out = flow.style_guide_prepare_questions(str(book))
    dialect = next(q for q in out["questions"] if q["id"] == "dialect")
    assert dialect["prefilled"] == "mexican_spanish"
    assert dialect["prefilled_source"] == "house_rule"
    assert {"question": "dialect", "rule_id": "mexican-spanish", "rule": "Write Mexican Spanish."} in out["house_covered"]
    assert set(out) <= set(flow.OUTPUT_SCHEMAS["style-guide prepare-questions"])


def test_prepare_draft_turns_off_a_rule_the_answers_depart_from(house):
    from src.judges.context import load_book_overrides

    book = _harness_book(house, "plain", "A plain story.\n")
    flow.style_guide_prepare_questions(str(book))
    write_json(book / ".harness" / "style_answers.json", {"dialect": "castilian_spanish"})
    out = flow.style_guide_prepare_draft(str(book))
    assert out["house_overrides"][0]["rule_id"] == "mexican-spanish"
    assert "mexican-spanish" in load_book_overrides(book)
    prompt = (book / ".harness" / "style_guide_prompt.txt").read_text(encoding="utf-8")
    assert "HOUSE RULES ALREADY IN THE TRANSLATOR'S PROMPT" in prompt
    assert "Write Mexican Spanish." not in prompt  # off for this book now


def test_prepare_draft_turns_the_rule_back_on_when_the_house_answer_returns(house):
    from src.judges.context import load_book_overrides

    book = _harness_book(house, "plain", "A plain story.\n")
    flow.style_guide_prepare_questions(str(book))
    write_json(book / ".harness" / "style_answers.json", {"dialect": "castilian_spanish"})
    flow.style_guide_prepare_draft(str(book))
    assert "mexican-spanish" in load_book_overrides(book)

    flow.style_guide_prepare_questions(str(book))  # the rule is off, so its prefill names no rule
    write_json(book / ".harness" / "style_answers.json", {"dialect": "mexican_spanish"})
    out = flow.style_guide_prepare_draft(str(book))
    assert [(o["rule_id"], o["question"]) for o in out["house_restored"]] == [("mexican-spanish", "dialect")]
    assert out["house_overrides"] == []
    assert "mexican-spanish" not in load_book_overrides(book)
    prompt = (book / ".harness" / "style_guide_prompt.txt").read_text(encoding="utf-8")
    assert "Write Mexican Spanish." in prompt


def test_glossary_prepare_prefills_and_commit_reports_departures(house):
    make_book(house, "old-horse", glossary=[{"english": "halter", "spanish": "jáquima"}])
    book = _harness_book(house, "new-horse", "He took the halter from the stall.\n")
    out = flow.glossary_prepare(str(book))
    assert {p["english"]: p["translation"] for p in out["prefilled"]} == {"halter": "jáquima", "stall": "casilla"}
    prompt = (book / ".harness" / "glossary_prompt.txt").read_text(encoding="utf-8")
    assert "PRE-FILLED RENDERINGS" in prompt and "stall → casilla" in prompt

    write_json(book / ".harness" / "glossary_draft.json", [
        {"english": "halter", "translation": "jáquima", "type": "technical"},
        {"english": "stall", "translation": "establo", "type": "technical"},
    ])
    committed = flow.glossary_commit(str(book))
    assert committed["house_departures"] == [
        {"english": "stall", "prefilled": "casilla", "committed": "establo", "origin": "house lexicon (group:horses)"},
    ]
    assert committed["house_check"]["counts"]["glossary_departures"] == 1


def test_address_prepare_carries_house_rules_and_series_pairs(house, monkeypatch):
    from tests.test_house.conftest import pair

    make_book(house, "hardy-1", pairs=[pair("Frank", "Chet", "tú", "tú")])
    book = _harness_book(house, "hardy-2", '"Hello, Chet," said Frank. "Hi, Frank," said Chet.\n')
    monkeypatch.setattr(
        "src.harness.address_sample.select_address_sample_chapters",
        lambda project_dir, max_chapters=6: [_Score()],
    )
    monkeypatch.setattr(flow, "_format_sample_chapters_block", lambda project_dir, scores: "(sample)")
    out = flow.address_map_prepare(str(book))
    assert out["house_address_rules"] and out["precedent_pairs"] == 1
    prompt = (book / ".harness" / "address_map_prompt.txt").read_text(encoding="utf-8")
    assert "Two adult strangers say usted both ways." in prompt
    assert "- Frank → Chet: tú | Chet → Frank: tú [hardy-1]" in prompt
    first, last = out["sections"]["series_precedent"]
    assert "[hardy-1]" in "\n".join(prompt.splitlines()[first - 1:last])


class _Score:
    def to_dict(self):
        return {"chapter_id": "chapter_01"}


def test_precedent_pair_line_keeps_every_rule_and_note():
    p = {"a": "Frank", "b": "Gus", "relationship": "strangers", "from": "hardy-1", "directions": {
        "a_to_b": [{"form": "tú", "when": "after the rescue", "notes": None},
                   {"form": "usted", "when": "default", "notes": "a stranger at first"}],
        "b_to_a": [{"form": "tú", "when": "default"}],
    }}
    assert flow._format_precedent_pair(p) == (
        "- Frank → Gus: tú (when: after the rescue) / usted (note: a stranger at first)"
        " | Gus → Frank: tú — strangers [hardy-1]"
    )


def test_prompt_sections_find_a_block_the_template_cut_short():
    prompt = "Intro line\nSAMPLE:\none two\nthree four\n\nReturn JSON.\nHOUSE\nrule a\n"
    sections = flow._prompt_sections(prompt, {
        "sample": "one two\nthree four\nfive six",  # the template kept the first two lines
        "house": "HOUSE\nrule a",
        "absent": "never inserted",
        "empty": "",
    })
    assert sections == {"sample": [3, 4], "house": [7, 8]}
