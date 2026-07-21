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
