"""Tests for Gutenberg footnote import (src/footnote_import.py)."""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.footnote_import import (
    apply_drop,
    apply_import,
    convert_chapter_footnotes,
    find_footnotes,
    load_footnotes_sidecar,
    records_from_matches,
    write_footnote_annotations,
    write_footnotes_sidecar,
)

FIX = Path(__file__).parent / "fixtures"


def _soup(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8"), "html.parser")


# ---------------------------------------------------------------------------
# Detection / extraction
# ---------------------------------------------------------------------------

def test_detect_format_a_interspersed():
    soup = _soup("footnotes_format_a.html")
    matches = find_footnotes(soup)
    assert len(matches) == 2
    assert matches[0].number == 1 and matches[1].number == 2
    assert matches[0].source_body.startswith("To distinguish him from Jacko II.")
    # <i>…</i> in a note body becomes _underscore_ italics.
    assert "_Le Cheval_" in matches[1].source_body
    assert matches[0].detected == "backlink"


def test_detect_format_b_collected():
    soup = _soup("footnotes_format_b.html")
    matches = find_footnotes(soup)
    assert len(matches) == 2
    assert "Richard" in matches[0].source_body
    assert "place of education" in matches[0].source_body
    assert "Bernard was founder of the family" in matches[1].source_body


def test_detect_generic_is_class_independent():
    soup = _soup("footnotes_generic.html")
    matches = find_footnotes(soup)
    # No footnote/citation classes at all — detection rests on the back-link.
    assert len(matches) == 2
    assert matches[0].source_body == "First generic note body."
    assert matches[1].source_body == "Second generic note body."


def test_apply_import_inserts_tokens_and_removes_defs():
    soup = _soup("footnotes_format_a.html")
    matches = find_footnotes(soup)
    apply_import(matches)
    text = soup.get_text()
    assert "[FOOTNOTE:1]" in text and "[FOOTNOTE:2]" in text
    # The reference marker sits right after the anchored word.
    assert "knighthood.[FOOTNOTE:2]" in text.replace("\n", "")
    # Note bodies are gone from the body flow.
    assert "To distinguish him" not in text
    assert "Le Cheval" not in text


def test_apply_drop_leaves_no_residue():
    soup = _soup("footnotes_format_b.html")
    matches = find_footnotes(soup)
    apply_drop(matches)
    text = soup.get_text()
    assert "[1]" not in text and "[2]" not in text
    assert "[FOOTNOTE" not in text
    assert "Richard" not in text  # note body removed


def test_sidecar_roundtrip(tmp_path):
    soup = _soup("footnotes_format_a.html")
    records = records_from_matches(find_footnotes(soup))
    write_footnotes_sidecar(tmp_path, records)
    loaded = load_footnotes_sidecar(tmp_path)
    assert [r["number"] for r in loaded] == [1, 2]
    assert loaded[0]["translated_body"] is None
    assert loaded[0]["detected"] == "backlink"


# ---------------------------------------------------------------------------
# Conversion: [FOOTNOTE:N] -> annotations
# ---------------------------------------------------------------------------

def test_convert_single_footnote_anchors_on_preceding_word():
    combined = "Era una tarde tranquila.[FOOTNOTE:1] Todo estaba en calma."
    es_map = {0: "Era una tarde tranquila.", 1: "Todo estaba en calma."}
    bodies = {1: "Nota traducida."}
    clean, records = convert_chapter_footnotes("chapter_01", "proj", combined, es_map, bodies)

    assert "[FOOTNOTE:1]" not in clean
    assert len(records) == 1
    rec = records[0]
    assert rec["es_idx"] == 0
    assert rec["sub_id"] == "gb1"
    assert rec["origin"] == "gutenberg"
    assert rec["content"] == "[tranquila.] Nota traducida."


def test_convert_two_footnotes_one_sentence():
    combined = "Odry[FOOTNOTE:1] susurro Fan misteriosamente[FOOTNOTE:2]."
    es_map = {0: "Odry susurro Fan misteriosamente."}
    bodies = {1: "Primera nota.", 2: "Segunda nota."}
    clean, records = convert_chapter_footnotes("chapter_01", "proj", combined, es_map, bodies)

    assert "[FOOTNOTE" not in clean
    assert len(records) == 2
    assert {r["es_idx"] for r in records} == {0}
    assert {r["sub_id"] for r in records} == {"gb1", "gb2"}
    assert records[0]["content"] == "[Odry] Primera nota."
    assert records[1]["content"].endswith("Segunda nota.")


def test_convert_skips_untranslated_body():
    combined = "Hola mundo.[FOOTNOTE:1]"
    es_map = {0: "Hola mundo."}
    clean, records = convert_chapter_footnotes("chapter_01", "proj", combined, es_map, bodies={})
    assert records == []
    assert "[FOOTNOTE" not in clean


# ---------------------------------------------------------------------------
# annotations.jsonl writing + idempotency (round-trips through endnotes loader)
# ---------------------------------------------------------------------------

def test_write_annotations_is_idempotent(tmp_path):
    from src.endnotes import _load_footnote_annotations

    combined = "Odry[FOOTNOTE:1] susurro Fan[FOOTNOTE:2] misteriosamente."
    es_map = {0: "Odry susurro Fan misteriosamente."}
    bodies = {1: "Primera nota.", 2: "Segunda nota."}
    _, records = convert_chapter_footnotes("chapter_01", tmp_path.name, combined, es_map, bodies)

    write_footnote_annotations(tmp_path, "chapter_01", records)
    write_footnote_annotations(tmp_path, "chapter_01", records)  # re-run

    loaded = _load_footnote_annotations(tmp_path, "chapter_01")
    total = sum(len(v) for v in loaded.values())
    assert total == 2  # not 4 — prior import was tombstoned
    assert len(loaded[0]) == 2


def test_converted_anchor_places_endnote_marker_exactly(tmp_path):
    """The anchor convert() derives must be found by endnotes, landing the
    marker at exactly the original token position."""
    import json

    from src.endnotes import build_endnote_artifacts

    combined = "Era una tarde tranquila.[FOOTNOTE:1] Todo estaba en calma."
    es_map = {0: "Era una tarde tranquila.", 1: "Todo estaba en calma."}
    clean, records = convert_chapter_footnotes("chapter_01", tmp_path.name, combined, es_map, {1: "Nota."})
    write_footnote_annotations(tmp_path, "chapter_01", records)

    (tmp_path / "alignments").mkdir()
    (tmp_path / "alignments" / "chapter_01.json").write_text(
        json.dumps({"alignments": [
            {"es_idx": 0, "es": "Era una tarde tranquila."},
            {"es_idx": 1, "es": "Todo estaba en calma."},
        ]}),
        encoding="utf-8",
    )

    injected, entries = build_endnote_artifacts(tmp_path, [("chapter_01", clean)])
    assert "tranquila.{{ENDNOTE:1}}" in injected["chapter_01"]
    assert len(entries) == 1
    assert entries[0].text == "Nota."


def test_import_preserves_manual_annotation(tmp_path):
    from src.endnotes import _load_footnote_annotations

    # A hand-authored footnote (no sub_id / origin) on the same sentence.
    ann = tmp_path / "annotations.jsonl"
    ann.write_text(
        '{"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", '
        '"content": "[Odry] Nota manual."}\n',
        encoding="utf-8",
    )

    combined = "Odry[FOOTNOTE:1] fin."
    es_map = {0: "Odry fin."}
    _, records = convert_chapter_footnotes("chapter_01", tmp_path.name, combined, es_map, {1: "Importada."})
    write_footnote_annotations(tmp_path, "chapter_01", records)

    loaded = _load_footnote_annotations(tmp_path, "chapter_01")
    contents = loaded[0]
    assert any("Nota manual." in c for c in contents)   # manual kept
    assert any("Importada." in c for c in contents)     # import added
    assert len(contents) == 2
