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


def test_translate_footnotes_writes_bodies_via_mocked_llm(tmp_path, monkeypatch):
    from src.footnote_import import FootnoteRecord, write_footnotes_sidecar, load_footnotes_sidecar

    proj = tmp_path / "book"
    proj.mkdir()
    write_footnotes_sidecar(proj, [
        FootnoteRecord(number=1, ref_marker="[1]", source_body="First note.", detected="backlink"),
        FootnoteRecord(number=2, ref_marker="[2]", source_body="Second note.", detected="backlink",
                       translated_body="Ya traducida."),
    ])

    def fake_llm(prompt, **kwargs):
        # Batch prompt includes note 1 only (2 already translated).
        assert "1|" in prompt
        return "1| Primera nota."

    monkeypatch.setattr("src.api_translator.call_llm", fake_llm)
    done = tf.translate_footnotes(
        proj, provider="anthropic", model="fake",
        source_language="English", target_language="Spanish", title="T",
    )
    assert done == 1
    loaded = load_footnotes_sidecar(proj)
    by_n = {n["number"]: n for n in loaded}
    assert by_n[1]["translated_body"] == "Primera nota."
    assert by_n[2]["translated_body"] == "Ya traducida."


def test_translate_footnotes_noop_when_all_done(tmp_path, monkeypatch):
    from src.footnote_import import FootnoteRecord, write_footnotes_sidecar

    proj = tmp_path / "book"
    proj.mkdir()
    write_footnotes_sidecar(proj, [
        FootnoteRecord(number=1, ref_marker="[1]", source_body="A", detected="x",
                       translated_body="Ya."),
    ])
    monkeypatch.setattr(
        "src.api_translator.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call LLM")),
    )
    assert tf.translate_footnotes(
        proj, provider="anthropic", model="fake",
        source_language="English", target_language="Spanish", title="T",
    ) == 0
