"""The dated markdown report."""

from __future__ import annotations

from src.annotations.report import render_report


def _doc(**kw):
    base = {
        "project": "testbook",
        "committed_at": "2026-07-28T12:00:00",
        "target_language": "Spanish",
        "backend": "subagent",
        "worker_model": "sonnet",
        "chapters": None,
        "results": [],
        "skipped": [],
        "failed": [],
        "missing": [],
    }
    base.update(kw)
    return base


def _result(**kw):
    base = {
        "key": "chapter_01__1__u1",
        "chapter_id": "chapter_01",
        "es_idx": 1,
        "type": "word_choice",
        "content": "poyo",
        "anchors": [],
        "es_sentence": "El poyo estaba frío.",
        "state": "needs_help",
        "recommendation": "«Marco» se entiende mejor.",
        "note_text": "«Poyo» es regional.",
        "new_content": "poyo\n— IA: «Poyo» es regional.",
        "mode": "append",
        "writable": True,
        "manual_reason": None,
        "confidence": "high",
        "evidence": ["style guide"],
    }
    base.update(kw)
    return base


def test_report_logs_content_as_of_run_time_verbatim():
    """The whole point: applying rewrites the note, so the before-state is on record."""
    text = render_report(_doc(results=[_result(content="[muserón] ¿muserola?")]))
    assert "[muserón] ¿muserola?" in text


def test_bracket_heavy_content_is_fenced_not_mangled():
    text = render_report(_doc(results=[_result(content="[a]; [b]; [c]")]))
    assert "```\n[a]; [b]; [c]\n```" in text


def test_summary_table_counts_each_bucket():
    text = render_report(
        _doc(
            results=[
                _result(key="a", writable=True),
                _result(key="b", state="already_resolved", writable=False, new_content=None),
                _result(key="c", writable=False, manual_reason="multi_anchor", new_content=None),
            ]
        )
    )
    assert "| **Total** | **3** | **1** | **1** | **1** |" in text


def test_spanish_is_the_default_body_language():
    text = render_report(_doc(results=[_result()]))
    assert "# Revisión de anotaciones" in text
    assert "Elección de palabra" in text


def test_english_fallback_for_other_languages():
    text = render_report(_doc(target_language="German", results=[_result()]))
    assert "# Annotation review" in text
    assert "Word choice" in text


def test_planned_text_is_shown_with_its_write_mode():
    text = render_report(_doc(results=[_result()]))
    assert "se añade al final de la nota" in text
    assert "— IA: «Poyo» es regional." in text

    footnote = render_report(
        _doc(
            results=[
                _result(
                    type="footnote",
                    mode="replace",
                    content="[Sancerre]",
                    new_content="[Sancerre] Ciudad de Francia.",
                )
            ]
        )
    )
    assert "se publica" in footnote


def test_withheld_note_still_shows_its_drafted_text():
    """A multi-anchor footnote's value to the reader is the gloss they paste in."""
    text = render_report(
        _doc(
            results=[
                _result(
                    type="footnote",
                    content="[a]; [b]",
                    writable=False,
                    manual_reason="multi_anchor",
                    new_content=None,
                    note_text="Una glosa útil.",
                )
            ]
        )
    )
    assert "dividirla a mano" in text
    assert "Una glosa útil." in text


def test_omitted_section_explains_every_skip():
    text = render_report(
        _doc(
            skipped=[
                {"key": "k1", "chapter_id": "chapter_02", "es_idx": 31,
                 "type": "word_choice", "content": "chancla", "reason": "orphaned"},
                {"key": "k2", "chapter_id": "chapter_03", "es_idx": 4,
                 "type": "footnote", "content": "[x] body", "reason": "imported"},
            ]
        )
    )
    assert "## Omitidas" in text
    assert "ya no existe en la alineación" in text
    assert "importada de Gutenberg" in text


def test_failures_are_surfaced():
    text = render_report(
        _doc(
            failed=[{"key": "k1", "type": "word_choice", "problem": "bad json"}],
            missing=[{"key": "k2", "type": "footnote"}],
        )
    )
    assert "## Fallos" in text
    assert "bad json" in text


def test_empty_run_says_so():
    assert "No se revisó ninguna anotación" in render_report(_doc())
