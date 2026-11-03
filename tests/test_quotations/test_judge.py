"""The quotation judge: its coded facts, its drafts, and its verdicts staying honest.

Same made-up book and Bible as test_apply; no real verse text belongs in here.
"""

import json

import pytest

from src.judges.llm_io import JudgeParseError
from src.quotations import board as qboard
from src.quotations import judge as qjudge
from tests.test_quotations.test_apply import book  # noqa: F401  (fixture)


@pytest.mark.parametrize("old, new, head, kind, place", [
    ("luz", "lumbre", "Dijo: «Yo soy la ", "quotation", "inside"),
    ("luz", "lumbre", "Dijo: «Yo soy». Y la ", "quotation", "outside"),
    ("«la luz»", "la lumbre", "Dijo: ", "quotation", "inside"),
    ("luz", "lumbre", "", "epigraph", "epigraph"),
    # A stray closing mark earlier must not make the depth go negative and hide an open one.
    ("luz", "lumbre", "» y «la ", "quotation", "inside"),
])
def test_where_the_words_sit(old, new, head, kind, place):
    assert qjudge.edit_facts(old, new, head, kind)["place"] == place


@pytest.mark.parametrize("old, new, change", [
    ("la luz", "La luz", "case"),
    ("la luz, grande", "la luz grande", "punctuation"),
    ("la luz, grande", "La luz grande", "case_and_punctuation"),
    ("la luz", "la lumbre", "words"),
])
def test_what_kind_of_change(old, new, change):
    assert qjudge.change_kind(old, new) == change


@pytest.mark.parametrize("old, new, head, block", [
    ("¿Quién es la luz?", "¿Quién es la lumbre", "«", qjudge.BLOCK_UNBALANCED),
    ("la luz", "[la lumbre", "«", qjudge.BLOCK_UNBALANCED),
    ("Dijo: Hay luz", "Dijo: «Hay luz»", "«", qjudge.BLOCK_NESTED),
    ("su mano", "Su mano y Su luz", "Vio ", qjudge.BLOCK_CAPITAL),
    ("a él", "a Él", "Vio ", qjudge.BLOCK_CAPITAL),
    ("oró al Señor", "oró al SEÑOR", "Y entró y ", qjudge.BLOCK_CAPITAL),
])
def test_a_coded_defect_blocks(old, new, head, block):
    assert block in qjudge.edit_facts(old, new, head, "quotation")["blocks"]


@pytest.mark.parametrize("old, new, head", [
    # Repairing a stray mark is not unbalancing one.
    ("Dijo: ‹Hay luz.", "Dijo: Hay luz.", "«"),
    # The NBLA's capital belongs inside the marks.
    ("a él", "a Él", "Dijo: «Vio "),
    ("oró al Señor", "oró al SEÑOR", "Dijo: «Y "),
    # A title capital is the verse's to give; only pronouns and SEÑOR are blocked.
    ("el juez de todos", "el Juez de todos", "Dios es "),
    # A capital that opens the span is the sentence's.
    ("Él vio", "Él miró", ""),
    # ‹ › are how a quotation sits inside « ».
    ("Dijo: Hay luz", "Dijo: ‹Hay luz›", "«"),
])
def test_no_block(old, new, head):
    assert qjudge.edit_facts(old, new, head, "quotation")["blocks"] == []


def test_an_item_sees_the_sentences_around_the_edit():
    rows = {4: "Antes.", 5: "Dijo: «la luz».", 6: "Después."}
    card = {"key": "k", "chapter_id": "chapter_01", "ref": "Juan 8:12", "kind": "quotation"}
    item = qjudge.item_view(card, [{"es_idx": 5, "old": "la luz", "new": "la lumbre"},
                                   {"es_idx": 5, "old": "Dijo", "new": "Dijo"}], rows, [])
    assert len(item["edits"]) == 1
    edit = item["edits"][0]
    assert edit["before"] == "Antes. Dijo: «la luz». Después."
    assert edit["after"] == "Antes. Dijo: «la lumbre». Después."
    assert edit["place"] == "inside" and edit["found"] is True


def test_an_open_mark_is_read_from_the_rows_before():
    rows = {1: "«Primera frase.", 2: "La luz vino."}
    edit = qjudge.item_view({"key": "k", "chapter_id": "c"}, [{"es_idx": 2, "old": "luz", "new": "lumbre"}], rows, [])
    assert edit["edits"][0]["place"] == "inside"


def test_a_mark_far_back_does_not_reach_this_row():
    rows = {1: "«Sin cerrar.", 2: "a", 3: "b", 4: "c", 5: "d", 6: "La luz vino."}
    edit = qjudge.item_view({"key": "k", "chapter_id": "c"}, [{"es_idx": 6, "old": "luz", "new": "lumbre"}], rows, [])
    assert edit["edits"][0]["place"] == "outside"


def _answer(**over):
    return {"item": 1, "verdict": "partial", "confidence": 0.8, "fix": "su, not Su",
            "fix_kind": "blend", "reason": "Why.", **over}


def test_a_draft_resolves_back_onto_the_keys():
    out = qjudge.parse_draft(json.dumps([_answer(), _answer(item=2, verdict="toss_up", fix="", fix_kind="")]), ["a", "b"])
    assert out["a"]["verdict"] == "partial" and out["a"]["fix_kind"] == "blend"
    assert out["b"]["verdict"] == "toss_up"


def test_a_draft_may_drop_the_brackets():
    lines = "\n".join(json.dumps(_answer(item=i)) for i in (1, 2))
    assert set(qjudge.parse_draft(lines, ["a", "b"])) == {"a", "b"}
    with pytest.raises(JudgeParseError):
        qjudge.parse_draft(json.dumps(_answer(item=1)) + "\nnot json", ["a"])


def test_words_a_row_away_are_found_and_words_nowhere_are_not_passed_off_as_a_sentence():
    card = {"key": "k", "chapter_id": "c", "kind": "quotation"}
    rows = {4: "La luz vino.", 5: "Otra frase."}
    near = qjudge.item_view(card, [{"es_idx": 5, "old": "luz", "new": "lumbre"}], rows, [])["edits"][0]
    assert near["found"] is True and "La lumbre vino." in near["after"]
    gone = qjudge.item_view(card, [{"es_idx": 5, "old": "mar", "new": "río"}], rows, [])["edits"][0]
    assert gone["found"] is False and gone["before"] == "(not available)"
    assert "Otra frase" not in gone["after"]


def test_an_unknown_fix_kind_is_dropped_not_fatal():
    assert qjudge.parse_draft(json.dumps([_answer(fix_kind="rewrite")]), ["a"])["a"]["fix_kind"] == ""


@pytest.mark.parametrize("draft", [
    "not json",
    '{"item": 1}',
    json.dumps([_answer(verdict="uncertain")]),
    json.dumps([_answer(confidence=1.5)]),
    json.dumps([_answer(confidence=True)]),
    json.dumps([_answer(confidence="high")]),
    json.dumps([_answer(item=True)]),
    json.dumps([_answer(), _answer()]),
    json.dumps([_answer(item=2)]),
    json.dumps([]),
])
def test_a_bad_draft_is_refused_whole(draft):
    with pytest.raises(JudgeParseError):
        qjudge.parse_draft(draft, ["a"])


def test_a_blocked_card_cannot_come_back_apply():
    item = {"blocks": [qjudge.BLOCK_CAPITAL], "hash": "h"}
    out = qjudge.clamp({"verdict": "apply", "confidence": 0.9, "fix": "", "fix_kind": "", "reason": "r"}, item)
    assert out["verdict"] == "partial" and out["model_verdict"] == "apply"
    assert out["fix"] and out["fix_kind"] == "repair" and out["blocks"] == [qjudge.BLOCK_CAPITAL]
    kept = qjudge.clamp({"verdict": "reject", "confidence": 0.9, "fix": "", "fix_kind": "", "reason": "r"}, item)
    assert kept["verdict"] == "reject" and "model_verdict" not in kept


def _examples():
    return [{"place": "outside", "old": "EJEMPLO", "new": "b", "before": "c", "after": "d",
             "verdict": "reject", "confidence": 0.9, "reason": "r"}]


def test_the_prompt_splits_where_the_cache_needs_it():
    item = qjudge.item_view({"key": "k", "chapter_id": "c", "ref": "Juan 8:12", "kind": "quotation"},
                            [{"es_idx": 1, "old": "luz", "new": "lumbre"}], {1: "La luz."}, [])
    preamble, body = qjudge.prompt_parts([item], _examples())
    assert "EJEMPLO" in preamble and "lumbre" not in preamble
    assert "Item 1: Juan 8:12" in body and "EJEMPLO" not in body and "{{" not in body
    assert "{{" not in preamble


def test_an_item_with_a_defect_says_so():
    item = qjudge.item_view({"key": "k", "chapter_id": "c", "ref": "Juan 8:12", "kind": "quotation"},
                            [{"es_idx": 1, "old": "su luz", "new": "Su luz y Su paz"}], {1: "Vio su luz."}, [])
    assert "cannot be apply" in qjudge.prompt_parts([item], [])[1]


def test_the_tracked_examples_are_well_formed():
    from pathlib import Path

    path = Path(qjudge._PROMPTS_DIR) / "quotation_judge_examples.example.json"
    examples = json.loads(path.read_text(encoding="utf-8"))
    assert examples and qjudge.render_examples(examples)
    assert {ex["verdict"] for ex in examples} <= set(qjudge.VERDICTS)
    assert all(ex.get("fix_kind", "") in ("", *qjudge.FIX_KINDS) for ex in examples)


def _card(root):
    return qboard.load_mapped(root)["cards"][0]


def test_prepare_covers_cards_that_change_text(book):  # noqa: F811
    out = qjudge.judge_prepare(book, worker_model="claude-sonnet-5-5", cli="claude")
    assert out["status"] == "ok" and out["jobs"] == 1 and out["items"] >= 1
    manifest = json.loads((qjudge.judge_dir(book) / "manifest.json").read_text(encoding="utf-8"))
    assert _card(book)["key"] in manifest["jobs"][0]["keys"]
    assert manifest["cli"] == "claude" and manifest["worker_model"] == "claude-sonnet-5-5"


def test_the_wave_commits_verdicts_and_changes_nothing_else(book):  # noqa: F811
    mapped_before = qboard.load_mapped(book)
    fingerprint_before = qboard.fingerprint(book, mapped_before, qboard.load(book)["choices"])
    qjudge.judge_prepare(book, worker_model="claude-sonnet-5-5", cli="claude")
    manifest = json.loads((qjudge.judge_dir(book) / "manifest.json").read_text(encoding="utf-8"))
    job = manifest["jobs"][0]
    answers = [_answer(item=i, verdict="apply", fix="", fix_kind="") for i in range(1, len(job["keys"]) + 1)]
    with open(job["draft_path"], "w", encoding="utf-8") as fh:
        json.dump(answers, fh)

    out = qjudge.judge_commit(book)

    assert out["status"] == "ok" and sum(out["counts"].values()) == len(job["keys"])
    doc = qjudge.load_verdicts(book)
    card = _card(book)
    assert qjudge.verdict_for(card, doc)["confidence"] == 0.8
    assert qboard.load_mapped(book) == mapped_before
    assert qboard.fingerprint(book, mapped_before, qboard.load(book)["choices"]) == fingerprint_before


def test_a_revised_card_loses_its_verdict(book):  # noqa: F811
    card = _card(book)
    doc = {"verdicts": {card["key"]: {"hash": qjudge.edits_hash(card["quote_edits"]), "verdict": "apply"}}}
    assert qjudge.verdict_for(card, doc)["verdict"] == "apply"
    changed = {**card, "quote_edits": [{**card["quote_edits"][0], "new": "otra cosa"}]}
    assert qjudge.verdict_for(changed, doc) is None


def test_a_missing_draft_is_reported_not_guessed(book):  # noqa: F811
    qjudge.judge_prepare(book, worker_model="claude-sonnet-5-5", cli="claude")
    out = qjudge.judge_commit(book)
    assert out["status"] == "partial" and out["missing"] == ["job_001"]
    assert qjudge.load_verdicts(book)["verdicts"] == {}


def test_a_wave_runs_through_the_shared_runner(book):  # noqa: F811
    qjudge.judge_prepare(book, worker_model="claude-sonnet-5-5", cli="claude")
    manifest = json.loads((qjudge.judge_dir(book) / "manifest.json").read_text(encoding="utf-8"))
    answer = json.dumps([_answer(item=i) for i in range(1, len(manifest["jobs"][0]["keys"]) + 1)])
    prompts = []

    def runner(cmd, *, input_text, cwd):
        prompts.append(input_text)
        return 0, answer, ""

    out = qjudge.judge_fanout(book, runner=runner)
    assert out["counts"]["wrote"] == 1 and "Item 1:" in prompts[0]
    assert qjudge.judge_commit(book)["status"] == "ok"
