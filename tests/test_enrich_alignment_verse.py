"""Tests for `_enrich_alignment` verse line-break tagging.

Verse lines are stored one per line inside a stanza, and the reader shows a
break before every line after the first. The aligner can merge two lines into
one row (`es_sentences`, joined by a space). The line inside that row used to
match nothing, so the in-order key walk stalled on it and every later verse
line in the chapter lost its break (home-geography chapter_42).
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import _enrich_alignment


STANZA = (
    "Intro paragraph.\n"
    "\n"
    "Barcos de hierro cruzan el océano.\n"
    "Casas de hierro, muros de hierro,\n"
    "Cañones de hierro, balas de hierro,\n"
    "Pararrayos de hierro en las agujas,\n"
    "Cables telegráficos de hierro,\n"
    "Hierro en todo lo que usamos.\n"
    "\n"
    "Prose after.\n"
)


def _row(idx, es, para_start=False, parts=None):
    row = {"es_idx": idx, "es": es, "en": "", "chunk_id": "chapter_01_chunk_000"}
    if para_start:
        row["para_start"] = True
    if parts:
        row["es_sentences"] = parts
        row["es_indices"] = list(range(idx, idx + len(parts)))
    return row


def _by_start(data, prefix):
    return next(a for a in data["alignments"] if a.get("es", "").startswith(prefix))


def test_one_line_per_row_all_break(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(STANZA, encoding="utf-8")
    lines = STANZA.split("\n\n")[1].split("\n")
    rows = [_row(0, "Intro paragraph.")]
    rows += [_row(i + 1, ln, para_start=(i == 0)) for i, ln in enumerate(lines)]
    rows.append(_row(len(rows), "Prose after.", para_start=True))
    data = {"alignments": rows}

    _enrich_alignment(data, chapter, "p")

    for ln in lines[1:]:
        assert _by_start(data, ln).get("verse_line_break") is True, ln
    assert "verse_inner_breaks" not in _by_start(data, "Cañones")


def test_merged_verse_row_does_not_stall_later_lines(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(STANZA, encoding="utf-8")
    a, b = "Cañones de hierro, balas de hierro,", "Pararrayos de hierro en las agujas,"
    merged = f"{a} {b}"
    data = {"alignments": [
        _row(0, "Intro paragraph."),
        _row(1, "Barcos de hierro cruzan el océano.", para_start=True),
        _row(2, "Casas de hierro, muros de hierro,"),
        _row(3, merged, parts=[a, b]),
        _row(5, "Cables telegráficos de hierro,"),
        _row(6, "Hierro en todo lo que usamos."),
        _row(7, "Prose after.", para_start=True),
    ]}

    _enrich_alignment(data, chapter, "p")

    row = _by_start(data, "Cañones")
    assert row.get("verse_line_break") is True
    # The break between the two merged lines sits on the joining space.
    assert row["verse_inner_breaks"] == [len(a)]
    assert merged[len(a)] == " "
    assert row["es"] == merged  # display-only: es stays byte-identical
    assert _by_start(data, "Cables").get("verse_line_break") is True
    assert _by_start(data, "Hierro en").get("verse_line_break") is True
    assert not _by_start(data, "Prose after").get("verse_line_break")


def test_merged_row_opening_a_stanza_breaks_inside_only(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "Intro paragraph.\n\nLine one here,\nLine two here.\n\nProse after.\n",
        encoding="utf-8",
    )
    data = {"alignments": [
        _row(0, "Intro paragraph."),
        _row(1, "Line one here, Line two here.", para_start=True,
             parts=["Line one here,", "Line two here."]),
        _row(3, "Prose after.", para_start=True),
    ]}

    _enrich_alignment(data, chapter, "p")

    row = _by_start(data, "Line one")
    assert not row.get("verse_line_break")
    assert row["verse_inner_breaks"] == [len("Line one here,")]


def test_merged_prose_row_gets_no_inner_breaks(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "Intro paragraph.\n\nFirst prose sentence. Second prose sentence.\n",
        encoding="utf-8",
    )
    data = {"alignments": [
        _row(0, "Intro paragraph."),
        _row(1, "First prose sentence. Second prose sentence.", para_start=True,
             parts=["First prose sentence.", "Second prose sentence."]),
    ]}

    _enrich_alignment(data, chapter, "p")

    assert "verse_inner_breaks" not in _by_start(data, "First prose")
