"""Tests for `_enrich_alignment` image-placeholder placement.

The reader pipeline parses the combined chapter text to discover where
[IMAGE:...] placeholders go and inserts pseudo-alignment records of
type "image". Originally the parser required each placeholder to occupy
its OWN paragraph (separated by blank lines). When chunk boundaries — or
LLM retranslates — collapse the surrounding blank line, the placeholder
ends up glued to a real paragraph and gets silently dropped.

These tests pin down the fix: a placeholder is detected and inserted
even when it shares a paragraph with adjacent text, so missing blank
lines never cause an image to disappear from the reader.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import _enrich_alignment


def _make_alignments(*sentences):
    """Build a minimal alignments dict where each `(es, para_start)` tuple
    becomes one alignment row. The first row implicitly starts a paragraph
    in the original text (the parser skips paragraphs[0] for events)."""
    rows = []
    for idx, (es, para_start) in enumerate(sentences):
        row = {"es_idx": idx, "es": es, "en": "", "chunk_id": "chapter_01_chunk_000"}
        if para_start:
            row["para_start"] = True
        rows.append(row)
    return {"alignments": rows}


def _images(data):
    return [a for a in data["alignments"] if a.get("type") == "image"]


def test_placeholder_in_own_paragraph_still_inserted(tmp_path):
    """Baseline: the original happy path keeps working."""
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "First paragraph.\n"
        "\n"
        "[IMAGE:images/001.jpg:Alt text]\n"
        "\n"
        "Second paragraph.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("First paragraph.", False),
        ("Second paragraph.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    imgs = _images(data)
    assert len(imgs) == 1
    assert imgs[0]["src"] == "/projects/test-project/images/001.jpg"
    assert imgs[0]["alt"] == "Alt text"
    # Image should sit immediately before the second paragraph's first sentence.
    second_idx = next(i for i, a in enumerate(data["alignments"]) if a.get("es") == "Second paragraph.")
    assert data["alignments"][second_idx - 1].get("type") == "image"


def test_placeholder_glued_to_previous_paragraph_is_inserted(tmp_path):
    """Regression for chunk-boundary bug: a placeholder following a single
    newline after prose (no blank line) must still appear in the reader."""
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "First paragraph.\n"
        "\n"
        "Tail of chunk zero.\n"
        "[IMAGE:images/037.jpg:DE REPENTE]\n"
        "\n"
        "Head of chunk one.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("First paragraph.", False),
        ("Tail of chunk zero.", True),
        ("Head of chunk one.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    imgs = _images(data)
    assert len(imgs) == 1, f"expected one image, got {imgs!r}"
    assert imgs[0]["src"].endswith("images/037.jpg")
    assert imgs[0]["alt"] == "DE REPENTE"

    # The image belongs with "Tail..." (text that precedes it inside the
    # collapsed paragraph), so it should be inserted at the start of the
    # paragraph that follows — i.e. immediately before "Head of chunk one.".
    head_idx = next(i for i, a in enumerate(data["alignments"]) if a.get("es") == "Head of chunk one.")
    assert data["alignments"][head_idx - 1].get("type") == "image"


def test_multiple_placeholders_in_one_paragraph(tmp_path):
    """Two placeholders separated only by single newlines (no blank line
    between them) should both be inserted, in order."""
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "First paragraph.\n"
        "\n"
        "Some text.\n"
        "[IMAGE:images/a.jpg:A]\n"
        "[IMAGE:images/b.jpg:B]\n"
        "\n"
        "Next paragraph.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("First paragraph.", False),
        ("Some text.", True),
        ("Next paragraph.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    imgs = _images(data)
    assert [i["src"] for i in imgs] == [
        "/projects/test-project/images/a.jpg",
        "/projects/test-project/images/b.jpg",
    ]


def test_paragraph_with_only_placeholder_still_works(tmp_path):
    """A pure-image paragraph (no leading text) should not emit a spurious
    para event that would shift downstream image placement."""
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "First paragraph.\n"
        "\n"
        "[IMAGE:images/x.jpg:X]\n"
        "\n"
        "[IMAGE:images/y.jpg:Y]\n"
        "\n"
        "Last paragraph.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("First paragraph.", False),
        ("Last paragraph.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    imgs = _images(data)
    assert [i["alt"] for i in imgs] == ["X", "Y"]
    # Both images should land before "Last paragraph."
    last_idx = next(i for i, a in enumerate(data["alignments"]) if a.get("es") == "Last paragraph.")
    assert data["alignments"][last_idx - 2].get("type") == "image"
    assert data["alignments"][last_idx - 1].get("type") == "image"


# ---------------------------------------------------------------------------
# [CAPTION] tagging
#
# The reader needs to know which rows belong to a caption paragraph so it can
# style them. The flag is added; `es` must NOT be touched, because
# chunk_offset_start/end are computed against the real chunk text and
# corrections_apply slices on them.
# ---------------------------------------------------------------------------

def test_caption_row_is_flagged(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "Primer parrafo.\n\n"
        "[IMAGE:images/001.jpg]\n\n"
        "[CAPTION] El cordero.\n\n"
        "Segundo parrafo.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("Primer parrafo.", False),
        ("[CAPTION] El cordero.", True),
        ("Segundo parrafo.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    rows = [a for a in data["alignments"] if a.get("type") != "image"]
    assert rows[0].get("caption") is None
    assert rows[1].get("caption") is True
    assert rows[2].get("caption") is None


def test_caption_flag_spans_the_whole_paragraph(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "Primer parrafo.\n\n"
        "[IMAGE:images/001.jpg]\n\n"
        "[CAPTION] Primera oracion. Segunda oracion.\n\n"
        "Cuerpo.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("Primer parrafo.", False),
        ("[CAPTION] Primera oracion.", True),
        ("Segunda oracion.", False),
        ("Cuerpo.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    rows = [a for a in data["alignments"] if a.get("type") != "image"]
    assert rows[1].get("caption") is True
    # Continuation sentence of the same caption paragraph.
    assert rows[2].get("caption") is True
    # New paragraph ends the caption run.
    assert rows[3].get("caption") is None


def test_caption_tagging_leaves_es_byte_identical(tmp_path):
    chapter = tmp_path / "chapter_01.txt"
    chapter.write_text(
        "Primer parrafo.\n\n[IMAGE:images/001.jpg]\n\n[CAPTION] El cordero.\n",
        encoding="utf-8",
    )
    data = _make_alignments(
        ("Primer parrafo.", False),
        ("[CAPTION] El cordero.", True),
    )

    _enrich_alignment(data, chapter, "test-project")

    row = [a for a in data["alignments"] if a.get("caption")][0]
    assert row["es"] == "[CAPTION] El cordero."
