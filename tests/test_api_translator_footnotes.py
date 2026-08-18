"""Coverage for footnote STRUCTURE PRESERVATION prompt wiring."""

from src.api_translator import _structure_preservation_instructions


def test_structure_preservation_includes_footnote_bullet_when_tokens_present():
    text = _structure_preservation_instructions(
        "Hello [FOOTNOTE:1] world.", always_include_images=False
    )
    assert "[FOOTNOTE:N]" in text or "FOOTNOTE" in text


def test_structure_preservation_omits_footnote_bullet_without_tokens():
    text = _structure_preservation_instructions(
        "Hello world.", always_include_images=False
    )
    assert "FOOTNOTE" not in text


def test_structure_preservation_includes_caption_bullet_when_always_include_images():
    """The caption bullet rides the image cache-prefix gate, so a chunk with
    no captions still gets the instruction when always_include_images is on."""
    text = _structure_preservation_instructions(
        "Hello world.", always_include_images=True
    )
    assert "[CAPTION]" in text


def test_structure_preservation_omits_caption_bullet_without_captions():
    text = _structure_preservation_instructions(
        "Hello world.", always_include_images=False
    )
    assert "[CAPTION]" not in text


def test_structure_preservation_includes_caption_bullet_when_chunk_has_captions():
    text = _structure_preservation_instructions(
        "Hello.\n\n[CAPTION] A lamb.\n\nMore.", always_include_images=False
    )
    assert "[CAPTION]" in text
