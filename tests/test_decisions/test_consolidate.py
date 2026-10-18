"""The consolidate step: merge, prepare, coverage audit and commit."""

from __future__ import annotations

import json

import pytest

from src.decisions import consolidate as dcons
from src.decisions import decisions_dir
from src.decisions import taxonomy as dtax

from tests.test_decisions.test_scan import TAXONOMY


def _cand(chapter, category, subject, **extra):
    return {
        "chapter_id": chapter,
        "category": category,
        "subject": subject,
        "subject_key": extra.pop("key", subject.casefold()),
        "question": extra.pop("question", f"What about {subject}?"),
        "options": extra.pop("options", [{"label": "a", "effect": "x"}]),
        "quotes": extra.pop("quotes", [f"{subject} said hello."]),
        "why_hard": "",
        "house_default": extra.pop("house_default", False),
    }


CANDIDATES = [
    _cand("chapter_01", "names", "Thunder"),
    _cand("chapter_02", "names", "Thunder"),
    _cand("chapter_02", "names", "thunder", key="thunder"),
    _cand("chapter_01", "names", "Old Bess"),
    _cand("chapter_01", "address-pairs", "Hobbs -> Polly"),
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    tax = tmp_path / "decisions_taxonomy.json"
    tax.write_text(json.dumps(TAXONOMY), encoding="utf-8")
    monkeypatch.setattr(dtax, "TAXONOMY_FILE", tax)
    project_dir = tmp_path / "projects" / "book-a"
    decisions_dir(project_dir).mkdir(parents=True)
    (decisions_dir(project_dir) / "candidates.json").write_text(
        json.dumps({"candidates": CANDIDATES}), encoding="utf-8"
    )
    return project_dir


def test_merge_subjects_folds_chapters_and_keeps_the_common_spelling():
    merged = dcons.merge_subjects(CANDIDATES)
    names = merged["names"]
    assert [s["subject"] for s in names] == ["Thunder", "Old Bess"]
    assert names[0]["chapters"] == ["chapter_01", "chapter_02"]


def test_prepare_writes_one_job_per_category(project):
    out = dcons.consolidate_prepare(project)
    assert out["status"] == "ok", out
    assert out["by_category"] == {"names": 2, "address-pairs": 1}
    body = (decisions_dir(project) / "consolidate.names.body.txt").read_text(encoding="utf-8")
    assert body.startswith("CATEGORY: names")
    assert 'subject: "Thunder" — 2 chapter(s): 01, 02' in body
    assert "Translated Thunder" not in body  # the book's own example is held out


def test_prepare_needs_scan_candidates(tmp_path):
    out = dcons.consolidate_prepare(tmp_path / "nothing")
    assert out["status"] == "error"


def test_check_draft_audits_coverage():
    subjects = {"Thunder": ["chapter_01"], "Old Bess": ["chapter_01"], "Rex": ["chapter_02"]}
    checked = dcons.check_draft(
        {
            "rules": [{
                "question": "Translate descriptive names?",
                "options": [{"label": "translate", "effect": "e"}, {"label": "keep", "effect": "e"}],
                "recommendation": "translate",
                "examples": ["thunder", "Nobody"],
                "covers": ["thunder", "Old Bess"],
                "destination": "decisions_gate",
            }],
            "items": [{"subjects": ["Old Bess"], "question": "q", "destination": "somewhere"}],
            "dropped": [{"subject": "Ghost", "reason": "invented"}],
        },
        subjects,
    )
    rule = checked["rules"][0]
    assert rule["covers"] == ["Thunder", "Old Bess"]
    assert rule["examples"] == ["Thunder"]
    assert rule["recommendation"] == "translate"
    assert checked["items"][0]["destination"] == "glossary"
    assert checked["unaccounted"] == ["Rex"]
    assert checked["duplicated"] == ["Old Bess"]
    assert checked["unknown"] == ["Ghost"]


def test_commit_routes_and_reports(project):
    dcons.consolidate_prepare(project)
    ddir = decisions_dir(project)
    (ddir / "consolidate.names.draft.json").write_text(json.dumps({
        "category": "names",
        "rules": [{
            "question": "Translate names that are ordinary words?",
            "options": [{"label": "translate", "effect": "e"}],
            "recommendation": "translate",
            "why": "w",
            "examples": ["Thunder"],
            "covers": ["Thunder", "Old Bess"],
            "destination": "decisions_gate",
        }],
    }), encoding="utf-8")
    (ddir / "consolidate.address-pairs.draft.json").write_text(json.dumps({
        "category": "address-pairs",
        "items": [{"subjects": ["Hobbs -> Polly"], "question": "usted?", "destination": "address_map"}],
    }), encoding="utf-8")

    out = dcons.consolidate_commit(project)
    assert out["status"] == "ok", out
    assert out["counts"]["gate_questions"] == 1
    assert out["counts"]["address_map"] == 1
    assert out["coverage"] == {"unaccounted": 0, "unknown": 0, "duplicated": 0}
    report = (ddir / "consolidated.md").read_text(encoding="utf-8")
    assert "1. **Translate names that are ordinary words?**" in report
    assert "Thunder (2 ch.)" in report


def test_fanout_needs_a_manifest(project):
    assert "consolidate-prepare" in dcons.consolidate_fanout(project)["error"]


def test_glossary_items_merge_across_categories():
    categories = [
        {"category": "names", "items": [
            {"subjects": ["Baby"], "question": "Translate the doll's name?", "options": [{"label": "Bebé", "effect": "e"}], "why": "", "destination": "glossary"},
        ]},
        {"category": "grammatical-sex", "items": [
            {"subjects": ["baby"], "question": "Which gender?", "options": [{"label": "la bebé", "effect": "f"}], "why": "w", "destination": "glossary"},
            {"subjects": ["kittens"], "question": "q", "options": [], "why": "", "destination": "glossary"},
            {"subjects": ["a pun"], "question": "q", "options": [], "why": "", "destination": "translation_note"},
        ]},
    ]
    merged = dcons.merge_items_across_categories(categories, "glossary")
    assert len(merged) == 2
    baby = next(m for m in merged if "Baby" in m["subjects"])
    assert baby["categories"] == ["names", "grammatical-sex"]
    assert [o["label"] for o in baby["options"]] == ["Bebé", "la bebé"]
    assert len(dcons.merge_items_across_categories(categories, "translation_note")) == 1


def _category(category, *, rules=(), items=(), settled=()):
    return {"category": category, "rules": list(rules), "items": list(items), "settled": list(settled)}


def _item(subjects, destination="glossary"):
    return {"subjects": list(subjects), "question": "q", "options": [], "why": "", "destination": destination}


def test_reconcile_takes_a_settled_term_off_another_categorys_flag():
    categories = [
        _category("house-vocabulary", settled=[
            {"subjects": ["ponies", "the roundup"], "by": "house_default", "answer": "the series term"},
        ]),
        _category("specialist-vocabulary", items=[
            _item(["Ponies", "filly"]),
            _item(["roundup"]),
        ]),
    ]
    cross = dcons.reconcile_across_categories(categories)
    items = categories[1]["items"]
    assert [it["subjects"] for it in items] == [["filly"]]  # the roundup-only item is gone
    assert items[0]["settled_elsewhere"][0]["settled_in"] == "house-vocabulary"
    assert {r["subject"] for r in cross["resolved"]} == {"Ponies", "roundup"}
    assert cross["check"] == []


def test_reconcile_leaves_a_different_decision_about_the_same_word():
    categories = [
        _category("dialect-register", settled=[
            {"subjects": ["Grandpa"], "by": "house_default", "answer": "standard spelling"},
        ]),
        _category("titles-honorifics", items=[_item(["Grandpa"])]),
    ]
    cross = dcons.reconcile_across_categories(categories)
    assert categories[1]["items"][0]["subjects"] == ["Grandpa"]
    assert cross["resolved"] == []
    assert [(c["asked_in"], c["other"]) for c in cross["check"]] == [("titles-honorifics", "dialect-register")]


def test_reconcile_points_a_flag_at_the_gate_question_that_covers_it():
    rule = {"question": "What sex is Misty's filly?", "options": [], "recommendation": None,
            "why": "", "examples": [], "covers": ["filly"], "destination": "decisions_gate"}
    categories = [
        _category("grammatical-sex", rules=[rule]),
        _category("specialist-vocabulary", items=[_item(["filly", "stallion"])]),
        _category("names", rules=[{**rule, "question": "Translate filly names?"}]),
    ]
    cross = dcons.reconcile_across_categories(categories)
    assert categories[1]["items"][0]["see_gate"] == ["What sex is Misty's filly?", "Translate filly names?"]
    two_rules = [c for c in cross["check"] if c["detail"].startswith("also a rule")]
    assert len(two_rules) == 1


def _gate_rule(question, covers, recommendation="a"):
    return {"question": question, "options": [{"label": "a", "effect": "e"}],
            "recommendation": recommendation, "why": "", "examples": covers[:1],
            "covers": covers, "destination": "decisions_gate"}


def test_rank_puts_reach_first_and_keeps_the_rest_below_the_cut():
    chapters = {f"s{i}": [f"chapter_{i:02d}"] for i in range(10)}
    chapters["dialect"] = ["chapter_01"]
    chapters["drawl"] = ["chapter_01"]
    categories = [
        _category("dialect-register", rules=[_gate_rule("Grandpa's speech?", ["dialect", "drawl"])]),
        _category("names", rules=[
            _gate_rule("Names?", ["s0", "s1", "s2"]),
            _gate_rule("Rhymes?", ["s3"], recommendation=None),
        ]),
    ]
    for cat in categories:
        cat["subjects"] = chapters
    ranked = dcons.rank_gate_questions(categories, cap=2)
    assert [r["question"] for r in ranked] == ["Names?", "Grandpa's speech?", "Rhymes?"]
    assert [r["chapters"] for r in ranked] == [3, 1, 1]
    assert [r["in_gate"] for r in ranked] == [True, True, False]

    doc = {"project": "p", "counts": {"subjects": 12, "gate_questions": 2, "below_cut": 1,
           "address_map": 0, "glossary": 0, "translation_notes": 0, "settled": 0,
           "house_rule_proposals": 0, "dropped": 0},
           "gate": ranked, "categories": [{**c, "items": [], "settled": [], "proposals": [],
           "dropped": [], "unaccounted": [], "unknown": [], "duplicated": []} for c in categories],
           "glossary": [], "translation_notes": []}
    report = dcons.render_report(doc)
    gate, below = report.split("## Answered by default")
    assert "2. **Grandpa's speech?**" in gate and "Rhymes?" not in gate
    assert "3. **Rhymes?**" in below and "needs an answer" in below


def test_overlapping_gate_questions_share_out_the_subject_and_sit_together():
    chapters = {f"c{i}": [f"chapter_{i:02d}" for i in range(n)] for i, n in ((0, 9), (1, 5), (2, 1), (3, 1))}
    chapters["mamsie"] = ["chapter_01"]
    categories = [
        _category("titles-honorifics", rules=[_gate_rule("Mother words?", ["mamsie", "c0"])]),
        _category("names", rules=[_gate_rule("Big names?", ["c1"]), _gate_rule("Pet names?", ["Mamsie", "c3"])]),
        _category("wordplay", rules=[_gate_rule("Puns?", ["c2"])]),
    ]
    for cat in categories:
        cat["subjects"] = {**chapters, "Mamsie": ["chapter_01"]}
    ranked = dcons.rank_gate_questions(categories, cap=2)
    assert [r["question"] for r in ranked] == ["Mother words?", "Pet names?", "Big names?", "Puns?"]
    pets = ranked[1]
    assert pets["covers"] == ["c3"] and pets["in_gate"]
    assert pets["chapters"] == 1  # counted again without Mamsie's chapter
    assert pets["shares"] == [{"subject": "Mamsie", "question": "Mother words?"}]
    assert "mamsie" in ranked[0]["covers"]

    doc = {"project": "p", "counts": {"subjects": 6, "gate_questions": 2, "below_cut": 2,
           "address_map": 0, "glossary": 0, "translation_notes": 0, "settled": 0,
           "house_rule_proposals": 0, "dropped": 0},
           "gate": ranked, "categories": [{**c, "items": [], "settled": [], "proposals": [],
           "dropped": [], "unaccounted": [], "unknown": [], "duplicated": []} for c in categories],
           "glossary": [], "translation_notes": []}
    assert "Overlaps question 1: Mamsie follows that answer" in dcons.render_report(doc)


def test_a_gate_question_left_with_no_subjects_leaves_the_gate():
    categories = [
        _category("titles-honorifics", rules=[_gate_rule("Mother words?", ["mamsie", "c0"])]),
        _category("names", rules=[_gate_rule("Pet names?", ["Mamsie"]), _gate_rule("Big names?", ["c1"])]),
    ]
    subjects = {"mamsie": ["chapter_01"], "Mamsie": ["chapter_01"],
                "c0": ["chapter_02", "chapter_03"], "c1": ["chapter_04"]}
    for cat in categories:
        cat["subjects"] = subjects
    ranked = dcons.rank_gate_questions(categories, cap=2)
    assert [r["question"] for r in ranked] == ["Mother words?", "Big names?", "Pet names?"]
    assert [r["in_gate"] for r in ranked] == [True, True, False]
    emptied = ranked[2]
    assert emptied["covers"] == [] and emptied["chapters"] == 0
    assert emptied["shares"] == [{"subject": "Mamsie", "question": "Mother words?"}]


def test_a_name_and_its_sex_are_two_questions():
    categories = [
        _category("names", rules=[_gate_rule("Translate animal names?", ["Skipper", "Misty"])]),
        _category("grammatical-sex", rules=[_gate_rule("Sex of unnamed creatures?", ["Skipper"])]),
    ]
    for cat in categories:
        cat["subjects"] = {"Skipper": ["chapter_01"], "Misty": ["chapter_02"]}
    ranked = dcons.rank_gate_questions(categories)
    assert all(r["covers"] and "shares" not in r for r in ranked)


def test_prepare_includes_the_house_dialogue_rules(project, monkeypatch):
    monkeypatch.setattr(dcons, "dialogue_rules_text", lambda: "THOUGHTS take guillemets.")
    dcons.consolidate_prepare(project)
    preamble = (decisions_dir(project) / "preamble.consolidate.txt").read_text(encoding="utf-8")
    assert "THOUGHTS take guillemets." in preamble
