"""Reference footnotes: one endnote per quotation inside « », holding its reference.

A made-up chapter and made-up cards, written straight to mapped.json and the
ledger: placement reads only those, the chapter text and the alignment. Nothing
here is Bible text.
"""

import json

import pytest

from src.annotations import store
from src.quotations import footnotes as qfoot
from src.quotations import quotations_dir

SENTENCES = [
    "Dijo una vez: «La lámpara alumbra el sendero».",               # 0 applied, ends ».
    "Si alguien dice «el río corre al mar», escúchalo.",            # 1 ends »,
    "«El viento sopla», dice el sabio, «y nadie lo ve» —dijo él—.",  # 2 split, before a raya
    "Otro dijo: «La sal es “buena”»”.",                             # 3 stacked closing marks
    "Y el agua es clara sin comillas.",                             # 4 unmarked
    "Repite: «La lámpara alumbra el sendero».",                     # 5 the same verse again
    "Como dice Sal 30:5, «el gozo viene al alba».",                 # 6 reference already written
    "Rechazamos su suave reinado.",                                 # 7 a hymn, with the user's note
    "Dijo: «Nadie es justo».",                                      # 8 declined card
    "Algo distinto: «Fuego del cielo».",                            # 9 superseded card
    "Cuenta: «Dos panes y peces».",                                 # 10 a verse range
    "Todos temen al «Juez de todos».",                              # 11 declined over a capital
]


def _card(key, es_idx, ref, span, new=None):
    edits = [{"old": span, "new": new or span}]
    return {"key": key, "chapter_id": "chapter_01", "kind": "quotation", "ref": ref, "status": "proposed",
            "es_idx": [es_idx], "es_span": span, "quote_edits": edits}


CARDS = [
    _card("A", 0, "Psalms 27:1", "La lámpara alumbra el sendero"),
    _card("B", 1, "Psalms 28:1", "el río corre al mar"),
    {**_card("C", 2, "Psalms 29:1", "El viento sopla"),
     "quote_edits": [{"old": "El viento sopla", "new": "El viento sopla"},
                     {"old": "y nadie lo ve", "new": "y nadie lo ve"}]},
    _card("D", 3, "Psalms 31:1", "La sal es “buena”"),
    _card("E", 4, "Psalms 32:1", "el agua es clara"),
    _card("F", 5, "Psalms 27:1", "La lámpara alumbra el sendero"),
    _card("G", 6, "Psalms 30:5", "el gozo viene al alba"),
    _card("H", 8, "Psalms 14:3", "Nadie es justo"),
    _card("I", 9, "Psalms 33:1", "Fuego del cielo"),
    _card("J", 10, "2 Kings 4:1-3", "Dos panes y peces"),
    {**_card("K", 0, "John 1:1", "La lámpara"), "kind": "allusion"},
    {**_card("L", 11, "Hebrews 12:23", "Juez de todos", new="juez de todos"),
     "quote_edits": [{"old": "Juez de todos", "new": "juez de todos", "pieces": ["Juez de todos,"]}]},
]

HYMN = {"project_id": "book", "chapter_id": "chapter_01", "es_idx": 7, "sub_id": "uhymn001",
        "type": "footnote", "content": "[reinado.] «Un himno» por alguien", "timestamp": "2026-10-01T10:00:00",
        "es_text": SENTENCES[7]}
EMPTY_AT_A = {"project_id": "book", "chapter_id": "chapter_01", "es_idx": 0, "sub_id": "uempty01",
              "type": "footnote", "content": "[sendero».]", "timestamp": "2026-10-01T10:00:00"}
REF_ONLY = {"project_id": "book", "chapter_id": "chapter_01", "es_idx": 4, "sub_id": "urefonly",
            "type": "footnote", "content": "[comillas.] Salmo 27:1", "timestamp": "2026-10-01T10:00:00"}


@pytest.fixture
def book(tmp_path):
    root = tmp_path / "book"
    (root / "chapters").mkdir(parents=True)
    (root / "alignments").mkdir()
    qdir = quotations_dir(root)
    qdir.mkdir(parents=True)
    (root / "chapters" / "chapter_01.txt").write_text("\n\n".join(SENTENCES), encoding="utf-8")
    rows = [{"es_idx": i, "en_idx": i, "es": s, "en": ""} for i, s in enumerate(SENTENCES)]
    (root / "alignments" / "chapter_01.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "alignments": rows}, ensure_ascii=False), encoding="utf-8")
    (qdir / "mapped.json").write_text(json.dumps({"cards": CARDS}, ensure_ascii=False), encoding="utf-8")
    (qdir / "resolved.json").write_text(json.dumps({"range_separator": "-", "candidates": []}), encoding="utf-8")
    ledger = [{"ts": "2026-11-01T10:00:00", "key": k, "chapter_id": "chapter_01", "action": "applied",
               "edits": [{"what": "quote", "old": e["old"], "new": e["new"]} for e in c["quote_edits"]]}
              for c in CARDS for k in [c["key"]] if k not in ("H", "I", "L")]
    for key in ("H", "L"):
        ledger.append({"ts": "2026-11-01T10:00:00", "key": key, "chapter_id": "chapter_01", "action": "declined"})
    ledger.append({"ts": "2026-11-01T10:00:00", "key": "I", "chapter_id": "chapter_01", "action": "superseded",
                   "superseded_by": "J"})
    (qdir / "decisions.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ledger) + "\n",
                                          encoding="utf-8")
    store.append_records(root, [HYMN, EMPTY_AT_A, REF_ONLY])
    return root


def _items(root):
    return {it["key"]: it for it in qfoot.items(root)["items"]}


def _active(root, origin=None):
    notes = store.load_active(root, types=("footnote",))
    return [n for n in notes if origin is None or n.get("origin") == origin]


def _approve_and_write(root):
    assert qfoot.approve(root)["status"] == "ok"
    out = qfoot.write(root)
    assert out["status"] == "ok", out
    return out


@pytest.mark.parametrize("key, head_end, tail_start", [
    ("A", "sendero».", ""),
    ("B", "mar»,", " escúchalo."),
    ("C", "nadie lo ve»", " —dijo él—."),
    ("D", "“buena”»”.", ""),
    ("F", "sendero».", ""),
    ("H", "justo».", ""),
])
def test_the_marker_follows_the_closing_mark_and_its_punctuation(book, key, head_end, tail_start):
    item = _items(book)[key]
    assert not item["unplaced"]
    assert item["head"].endswith(head_end)
    assert item["tail"].startswith(tail_start)


def test_which_cards_get_a_note(book):
    items = _items(book)
    # E is woven into the prose, I was replaced, K is an allusion.
    assert set(items) == {"A", "B", "C", "D", "F", "G", "H", "J", "L"}
    out = qfoot.items(book)
    assert out["skipped"]["unmarked"] == 1 and out["skipped"]["superseded"] == 1
    # A range takes an en dash in the note, whatever the book writes.
    assert items["A"]["note"] == "Sal 27:1" and items["J"]["note"] == "2 R 4:1–3"


def test_the_book_wording_is_nbla_when_applied_or_already_the_verse(book):
    items = _items(book)
    assert items["A"]["nbla"] is True
    # Declined, and the book's words are not the verse's.
    assert items["H"]["nbla"] is False
    # Declined over a capital, but the book already has the verse's words.
    assert items["L"]["nbla"] is True


def test_defaults_skip_a_written_reference_and_flag_a_repeat(book):
    items = _items(book)
    assert items["G"]["ref_written"] and not qfoot.default_tick(items["G"])
    assert items["F"]["repeat"] and qfoot.default_tick(items["F"])
    assert not items["A"]["repeat"]


def test_a_note_on_the_same_spot_or_with_only_the_same_reference_is_replaced(book):
    items = _items(book)
    assert [r["sub_id"] for r in items["A"]["replaces"]] == ["uempty01"]
    # The reference-only note goes to the nearest note for its verse.
    assert [r["sub_id"] for r in items["F"]["replaces"]] == ["urefonly"]
    assert not any(r["sub_id"] == "uhymn001" for it in items.values() for r in it["replaces"])


def test_nothing_is_written_without_an_approval(book):
    before = store.annotations_path(book).read_text(encoding="utf-8")
    out = qfoot.write(book)
    assert out["status"] == "error"
    assert store.annotations_path(book).read_text(encoding="utf-8") == before
    assert qfoot.write(book, dry_run=True)["to_add"] == 8


def test_write_adds_the_ticked_notes_and_leaves_the_hymn_alone(book):
    out = _approve_and_write(book)
    assert out["added"] == 8 and out["retired"] == 2 and not out["refused"]
    ours = {(n["es_idx"], n["content"]) for n in _active(book, qfoot.ORIGIN)}
    assert (0, "[sendero».] Sal 27:1") in ours and (1, "[mar»,] Sal 28:1") in ours
    assert all(n["es_idx"] != 6 for n in _active(book, qfoot.ORIGIN))  # G stays unticked
    others = [n for n in _active(book) if n.get("origin") != qfoot.ORIGIN]
    assert [n["sub_id"] for n in others] == ["uhymn001"]
    assert others[0]["content"] == HYMN["content"]
    assert qfoot.approve(book)["status"] == "error"  # a second write would do nothing


def test_unticking_retires_only_its_own_note(book):
    _approve_and_write(book)
    assert qfoot.save_choices(book, {"B": False})["status"] == "ok"
    plan = qfoot.plan(book)
    assert plan["add"] == [] and [(r["es_idx"], r["origin"]) for r in plan["retire"]] == [(1, qfoot.ORIGIN)]
    _approve_and_write(book)
    assert all(n["es_idx"] != 1 for n in _active(book, qfoot.ORIGIN))
    assert [n["sub_id"] for n in _active(book) if n.get("origin") != qfoot.ORIGIN] == ["uhymn001"]


def test_a_changed_tick_drops_the_approval(book):
    assert qfoot.approve(book)["status"] == "ok"
    qfoot.save_choices(book, {"G": True})
    assert qfoot.load_board(book)["approved"] is None
    assert qfoot.write(book)["status"] == "error"


def test_a_quotation_edited_by_hand_is_found_by_its_last_words(book):
    qdir = quotations_dir(book)
    # The card still has the words before the edit: "Que" was dropped from the start.
    cards = [c for c in CARDS if c["key"] != "H"] + [_card("H", 8, "Psalms 14:3", "Que Nadie es justo")]
    (qdir / "mapped.json").write_text(json.dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8")
    item = _items(book)["H"]
    assert not item["unplaced"] and item["by_tail"] and item["head"].endswith("justo».")
    assert not _items(book)["A"]["by_tail"]


def test_an_unplaced_card_cannot_be_ticked(book):
    qdir = quotations_dir(book)
    cards = CARDS + [_card("Z", 0, "Psalms 40:1", "palabras que no están")]
    (qdir / "mapped.json").write_text(json.dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8")
    item = _items(book)["Z"]
    assert item["unplaced"] and not qfoot.default_tick(item)
    assert qfoot.save_choices(book, {"Z": True})["status"] == "error"
