"""The board and apply: choices, approval, the judge-apply path, the ledger and the record.

A made-up one-chapter book and a made-up Bible. The aligner is replaced by a
splitter that writes one row per sentence, so ``realign_chapter`` and the
annotation re-anchor that follows it run for real without the embedding model.
"""

import json
import re

import pytest

from src.quotations import apply as qapply
from src.quotations import board as qboard
from src.quotations import map as qmap
from src.quotations import quotations_dir
from src.quotations import record as qrecord
from src.quotations import resolve as qresolve
from src.quotations import scan as qscan
from src.quotations.index import BibleIndex

VERSES = {"43:008:012": "»Yo soy la luz del camino; el que Me sigue no andará en sombras."}
INDEX = BibleIndex({"version": "nbla", "verses": VERSES, "proper_words": [], "smallcaps_resolved": True})

SOURCE = 'Jesus said, "I am the light of the world." (John 8:12) Walk in the light.'
TRANSLATION = "Jesús dijo: «Yo soy la luz del mundo». (Juan 8:12) Caminen en la luz."
SENTENCES = ["Jesús dijo: «Yo soy la luz del mundo».", "(Juan 8:12) Caminen en la luz."]
EN = ['Jesus said, "I am the light of the world."', "(John 8:12) Walk in the light."]


def _split(text):
    return [s for s in re.split(r"(?<=[.»])\s+(?=\()", text) if s]


def _write_alignment(root, sentences):
    rows = [{"es_idx": i, "en_idx": i, "es": es, "en": EN[i] if i < len(EN) else "",
             "chunk_id": "chapter_01_chunk_000"} for i, es in enumerate(sentences)]
    (root / "alignments" / "chapter_01.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "alignments": rows}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def book(tmp_path, monkeypatch):
    root = tmp_path / "book"
    (root / "chunks").mkdir(parents=True)
    (root / "alignments").mkdir()
    (root / "chapters").mkdir()
    (root / "chunks" / "chapter_01_chunk_000.json").write_text(json.dumps({
        "id": "chapter_01_chunk_000", "chapter_id": "chapter_01", "position": 0,
        "source_text": SOURCE, "translated_text": TRANSLATION,
        "metadata": {"char_start": 0, "char_end": len(SOURCE), "overlap_start": 0, "overlap_end": 0,
                     "paragraph_count": 1, "word_count": 14},
        "status": "translated",
    }, ensure_ascii=False), encoding="utf-8")
    (root / "chapters" / "chapter_01.txt").write_text(TRANSLATION, encoding="utf-8")
    _write_alignment(root, SENTENCES)
    (root / "corrections.jsonl").write_text(
        json.dumps({"chunk_id": "chapter_01_chunk_000", "original_es": "luz", "corrected_es": "LUZ"}) + "\n",
        encoding="utf-8")
    (root / "annotations.jsonl").write_text(json.dumps({
        "project_id": "book", "chapter_id": "chapter_01", "es_idx": 0, "sub_id": "u1",
        "type": "word_choice", "content": "¿luz o lumbre?", "timestamp": "2026-10-01T10:00:00",
        "es_text": SENTENCES[0],
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    # The aligner: one row per sentence of the current chunk text.
    import src.sentence_aligner as aligner

    def fake_align(chunk_paths, project_id, chapter_id, source_lang, target_lang, output_path):
        text = json.loads(open(chunk_paths[0], encoding="utf-8").read())["translated_text"]
        _write_alignment(root, _split(text))
        return {}

    monkeypatch.setattr(aligner, "align_chapter_chunks", fake_align)
    monkeypatch.setattr(qmap, "load_index", lambda *_a, **_k: INDEX)
    monkeypatch.setattr(qrecord, "whole_books_quoted", lambda keys: [])

    qscan.scan_prepare(root, worker_model="claude-sonnet-5-5", cli="claude")
    (quotations_dir(root) / "chapter_01.scan.draft.json").write_text(json.dumps({
        "chapter_id": "chapter_01",
        "candidates": [{"kind": "quotation", "en_span": "I am the light of the world.",
                        "es_idx": [0], "es_span": "Yo soy la luz del mundo", "ref_en": "John 8:12"}],
    }), encoding="utf-8")
    assert qscan.scan_commit(root)["status"] == "ok"
    qresolve.resolve(root, index=INDEX)
    qmap.map_prepare(root, worker_model="claude-sonnet-5-5", cli="claude")
    (quotations_dir(root) / "chapter_01.map.draft.json").write_text(json.dumps({
        "chapter_id": "chapter_01",
        "mappings": [{"id": "c1", "replace": [{"es_idx": 0, "old": "Yo soy la luz del mundo",
                                               "pieces": ["Yo soy la luz del camino;"]}],
                      "echo": [{"es_idx": 1, "old": "Caminen", "new": "Anden", "en_evidence": "Walk"}]}],
    }), encoding="utf-8")
    assert qmap.map_commit(root)["status"] == "ok"
    return root


def _card(root):
    return qboard.load_mapped(root)["cards"][0]


def test_defaults_tick_the_reference_and_nbla_but_not_echo(book):
    state = qboard.build(book)
    card = state["chapters"][0]["cards"][0]
    assert card["choice"] == {"ref": True, "nbla": True, "echo": [], "paste": "",
                              "state": "approve", "hand": False, "ask": ""}
    assert card["recommendation"]["verdict"] == "approve"
    assert [e["what"] for e in card["edits"]] == ["ref", "quote"]
    assert all(e["lands"] for e in card["edits"])


def test_preview_writes_nothing(book):
    before = (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
    qboard.build(book)
    assert (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8") == before


def test_saving_a_change_drops_the_approval(book):
    key = _card(book)["key"]
    qboard.approve(book)
    assert qboard.load(book)["approved"]
    qboard.save(book, {key: {"ref": True, "nbla": True, "echo": [0]}})
    assert qboard.load(book)["approved"] is None


def test_apply_refuses_an_unapproved_board(book):
    out = qapply.apply(book)
    assert out["status"] == "error" and "not approved" in out["error"]


def test_apply_goes_through_the_judge_path(book):
    key = _card(book)["key"]
    qboard.save(book, {key: {"ref": True, "nbla": True, "echo": [0]}})
    qboard.approve(book)
    queue_before = (book / "corrections.jsonl").read_text(encoding="utf-8")

    out = qapply.apply(book)
    assert out["status"] == "ok", out
    assert out["cards_applied"] == 1 and out["edits_archived"] == 3
    chunk = json.loads((book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8"))
    assert chunk["translated_text"] == "Jesús dijo: «Yo soy la luz del camino». (Jn 8:12) Anden en la luz."
    assert chunk["source_text"] == SOURCE
    # The reader's own queue is never touched.
    assert (book / "corrections.jsonl").read_text(encoding="utf-8") == queue_before
    archived = [json.loads(line) for line in (book / "corrections_applied.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["source"] for r in archived} == {"quotation-pass"}
    assert all(isinstance(r["chunk_offset_start"], int) for r in archived)
    # Snapshot, recombine, realign.
    assert list((book / ".chunk_edits" / "chapter_01" / "chapter_01_chunk_000").glob("*.json"))
    assert "luz del camino" in (book / "chapters" / "chapter_01.txt").read_text(encoding="utf-8")
    assert out["chapters_realigned"] == ["chapter_01"]
    # The note on the edited sentence follows it.
    note = json.loads((book / "annotations.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert note["es_text"] == "Jesús dijo: «Yo soy la luz del camino»."
    # Ledger, record, approval cleared.
    assert qapply.applied_cards(book)[key]["nbla"] is True
    record = json.loads((book / "quotations.json").read_text(encoding="utf-8"))
    assert record["verses_unique"] == 1 and record["verse_occurrences"] == 1
    assert record["rows"][0]["after"] == ["Yo soy la luz del camino"]
    # Five quoted words in a thirteen-word book is over the 25% share.
    assert record["share"] > 0.25 and record["within_limits"] is False
    assert qboard.load(book)["approved"] is None
    assert qapply.verify(book) == {"status": "ok", "checked": 1, "missing": []}


def test_apply_twice_changes_nothing_more(book):
    qboard.approve(book)
    qapply.apply(book)
    text = (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
    qboard.approve(book)
    out = qapply.apply(book)
    assert out["cards_applied"] == 0
    assert (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8") == text


def test_a_board_whose_chunk_changed_since_approval_is_refused(book):
    qboard.approve(book)
    path = book / "chunks" / "chapter_01_chunk_000.json"
    chunk = json.loads(path.read_text(encoding="utf-8"))
    chunk["translated_text"] = chunk["translated_text"].replace("Jesús", "Jesucristo")
    path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
    out = qapply.apply(book)
    assert out["status"] == "error" and "changed since it was approved" in out["error"]


def test_matches_card_counts_without_an_edit(book, monkeypatch):
    # The book already has the NBLA wording: nothing to edit, but it is NBLA text.
    doc = qboard.load_mapped(book)
    edit = doc["cards"][0]["quote_edits"][0]
    edit["new"] = edit["old"]
    doc["cards"][0]["status"] = "matches"
    doc["cards"][0]["ref_edit"] = None
    qmap.mapped_path(book).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    qboard.approve(book)
    out = qapply.apply(book)
    assert out["edits_archived"] == 0
    assert qrecord.read_record(book)["verses_unique"] == 1


def test_verify_reports_a_quotation_overwritten_later(book):
    qboard.approve(book)
    qapply.apply(book)
    path = book / "chunks" / "chapter_01_chunk_000.json"
    chunk = json.loads(path.read_text(encoding="utf-8"))
    chunk["translated_text"] = chunk["translated_text"].replace("del camino", "del mundo")
    path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
    out = qapply.verify(book)
    assert out["status"] == "drift" and out["missing"][0]["expected"] == "Yo soy la luz del camino"


def test_context_window_takes_a_sentence_either_side():
    text = "Uno. Dos dice «cita». Tres. Cuatro."
    start = text.index("cita")
    lo, hi, cut_left, cut_right = qboard.context_window(text, start, start + 4)
    assert text[lo:hi].strip() == "Uno. Dos dice «cita». Tres."
    assert not cut_left and not cut_right
    long = "x" * 900 + " «cita» " + "y" * 900
    start = long.index("cita")
    lo, hi, cut_left, cut_right = qboard.context_window(long, start, start + 4)
    assert cut_left and cut_right and hi - lo == 4 + 2 * qboard.CONTEXT_CAP


def test_board_shows_each_change_in_context(book):
    card = qboard.build(book)["chapters"][0]["cards"][0]
    quote = card["quote_edits"][0]["context"]
    assert quote["before"] == TRANSLATION
    assert quote["after"] == "Jesús dijo: «Yo soy la luz del camino». (Juan 8:12) Caminen en la luz."
    assert card["ref_edit"]["context"]["after"].count("(Jn 8:12)") == 1
    assert card["echo"][0]["context"]["after"].endswith("Anden en la luz.")


# ── states, hand fixes and history ────────────────────────────────────────────

def _choose(root, **change):
    key = _card(root)["key"]
    base = qboard.default_choice(_card(root))
    qboard.save(root, {key: {**base, **change}})
    return key


def _rows(root):
    return qapply.read_decisions(root)


def test_a_pending_card_is_left_alone(book):
    _choose(book, state="pending")
    qboard.approve(book)
    out = qapply.apply(book)
    assert out["cards_applied"] == 0 and _rows(book) == []
    assert "luz del mundo" in (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")


def test_a_rejected_card_is_declined_once(book):
    _choose(book, state="reject")
    qboard.approve(book)
    qapply.apply(book)
    qboard.approve(book)
    qapply.apply(book)
    assert [r["action"] for r in _rows(book)] == ["declined"]


def test_a_card_fixed_by_hand_counts_without_an_edit(book):
    key = _choose(book, state="approve", hand=True)
    before = (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
    qboard.approve(book)
    out = qapply.apply(book)
    assert out["edits_archived"] == 0
    assert (book / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8") == before
    row = qapply.applied_cards(book)[key]
    assert row["hand"] is True and row["nbla"] is True and row["edits"] == []
    record = qrecord.read_record(book)
    assert record["verses_unique"] == 1 and record["rows"][0]["hand"] is True
    assert record["rows"][0]["before"] == []
    assert record["rows"][0]["words"] == _card(book)["quote_edits"][0]["nbla_words"]
    assert qapply.verify(book)["checked"] == 0


def test_an_applied_card_can_be_redone_by_hand(book):
    qboard.approve(book)
    qapply.apply(book)
    # The user rewrites the quotation in the book; verify would now miss it.
    path = book / "chunks" / "chapter_01_chunk_000.json"
    chunk = json.loads(path.read_text(encoding="utf-8"))
    chunk["translated_text"] = chunk["translated_text"].replace("del camino", "del sendero")
    path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
    assert qapply.verify(book)["status"] == "drift"

    card = qboard.build(book)["chapters"][0]["cards"][0]
    assert card["phase"] == "applied" and card["history"]["action"] == "applied"
    key = _choose(book, state="approve", hand=True)
    assert qboard.build(book)["chapters"][0]["cards"][0]["phase"] == "approve"
    qboard.approve(book)
    qapply.apply(book)
    assert qapply.applied_cards(book)[key]["hand"] is True
    assert qapply.verify(book)["status"] == "ok"
    assert qboard.build(book)["chapters"][0]["cards"][0]["phase"] == "applied"


def test_the_hand_word_count_falls_back_to_the_whole_verse(book):
    doc = qboard.load_mapped(book)
    doc["cards"][0]["quote_edits"] = []
    doc["cards"][0]["verse_keys"] = ["43:008:012"]
    qmap.mapped_path(book).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    key = _choose(book, state="approve", hand=True)
    qboard.approve(book)
    qapply.apply(book)
    row = qapply.applied_cards(book)[key]
    assert row["verse_keys"] == ["43:008:012"]
    assert row["nbla_words"] == len(VERSES["43:008:012"].split())


def test_the_board_links_each_card_to_its_sentence(book):
    from urllib.parse import parse_qs, quote, urlparse

    def landing(card):
        url = urlparse(card["reader_url"])
        assert url.path == "/read/book/chapter_01"
        query = parse_qs(url.query)
        assert query["hl"] == ["1"] and query["near"] == ["1"]
        # The chunk editor sends the user back to this card after a save.
        assert query["return_to"] == [f"/quotations/book#card-{quote(card['key'], safe='')}"]
        row = qboard.Book(book).chapter_rows("chapter_01")[int(query["esi"][0])]
        assert row["es"].startswith(query["anchor"][0])
        return row["es"]

    card = qboard.build(book)["chapters"][0]["cards"][0]
    assert "Yo soy la luz del mundo" in landing(card)
    # Once applied, the link finds the sentence by the text that went in.
    qboard.approve(book)
    qapply.apply(book)
    card = qboard.build(book)["chapters"][0]["cards"][0]
    assert "Yo soy la luz del camino" in landing(card)


def test_counts_say_what_approving_would_record(book):
    counts = qboard.build(book)["counts"]
    assert counts["by_phase"] == {"approve": 1} and counts["to_record"] == 1
    _choose(book, state="pending")
    counts = qboard.build(book)["counts"]
    assert counts["by_phase"] == {"pending": 1} and counts["to_record"] == 0


def test_recommit_after_apply_keeps_the_applied_card(book):
    qboard.approve(book)
    qapply.apply(book)
    applied = _card(book)
    assert qmap.map_commit(book)["status"] == "ok"
    assert _card(book) == applied
