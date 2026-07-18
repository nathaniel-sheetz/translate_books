"""Tests for the pure helpers in scripts/translate_footnotes.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import translate_footnotes as tf  # noqa: E402


def test_parse_numbered_translations_basic():
    resp = "1| Primera nota.\n2| Segunda nota."
    assert tf.parse_numbered_translations(resp) == {1: "Primera nota.", 2: "Segunda nota."}


def test_parse_numbered_translations_joins_wrapped_lines():
    resp = "1| Una nota que se\nextiende en dos lineas.\n2| Otra."
    parsed = tf.parse_numbered_translations(resp)
    assert parsed[1] == "Una nota que se extiende en dos lineas."
    assert parsed[2] == "Otra."


def test_parse_numbered_translations_tolerates_noise():
    resp = "Here are the translations:\n1| Nota uno.\n\n2| Nota dos.\nDone."
    parsed = tf.parse_numbered_translations(resp)
    assert parsed[1].startswith("Nota uno.")
    assert parsed[2].startswith("Nota dos.")


def test_batch_notes_splits_on_budget():
    notes = [{"number": i, "source_body": "x" * 100} for i in range(1, 11)]
    batches = tf.batch_notes(notes, char_budget=250)
    assert sum(len(b) for b in batches) == 10
    assert all(sum(len(n["source_body"]) for n in b) <= 250 or len(b) == 1 for b in batches)
    assert len(batches) > 1


def test_build_footnotes_prompt_contains_numbers_and_context():
    notes = [{"number": 1, "source_body": "A note."}, {"number": 2, "source_body": "Another."}]
    prompt = tf.build_footnotes_prompt(
        notes, source_language="English", target_language="Spanish",
        title="My Book", glossary_text="cat -> gato", style_text="Formal register.",
    )
    assert "1| A note." in prompt and "2| Another." in prompt
    assert "cat -> gato" in prompt
    assert "Formal register." in prompt
    assert "Spanish" in prompt
