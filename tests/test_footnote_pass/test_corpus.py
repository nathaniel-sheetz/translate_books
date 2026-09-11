"""The style corpus: the placeholder/gloss split, and what stdout is allowed to be.

The split is load-bearing for Gate 1. A placeholder publishes nothing, so presenting
one as evidence of the book's footnote style would have the agent infer a category
from a note whose author never said anything.
"""

from __future__ import annotations

from src.footnote_pass import corpus as fp_corpus

from .conftest import footnote_record, write_annotations


def test_glosses_and_placeholders_are_split(project):
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco del Pacífico.", sub_id="u1"),
            footnote_record("chapter_01", 2, "[Sancerre]", sub_id="u2"),
            footnote_record("chapter_01", 1, "", sub_id="u3"),
            footnote_record("chapter_02", 0, "Caja donde viven las abejas.", sub_id="u4"),
        ],
    )
    out = fp_corpus.build_corpus(project)
    assert {n.es_idx for n in out.glosses} == {0}  # one per chapter, both es_idx 0
    assert len(out.glosses) == 2
    assert {n.es_idx for n in out.placeholders} == {1, 2}


def test_a_gloss_with_no_anchor_is_still_a_gloss(project):
    """The bracket is optional — endnotes falls back to the sentence end."""
    write_annotations(project, [footnote_record("chapter_01", 0, "Molusco del Pacífico.")])
    out = fp_corpus.build_corpus(project)
    assert len(out.glosses) == 1
    assert out.glosses[0].anchor is None
    assert out.glosses[0].display_text == "Molusco del Pacífico."


def test_glosses_carry_their_aligned_sentence(project):
    write_annotations(project, [footnote_record("chapter_01", 1, "[cuernitos] Cornículos.")])
    note = fp_corpus.build_corpus(project).glosses[0]
    assert note.es_sentence == "El pulgón suelta una gota por sus cuernitos."


def test_an_orphaned_gloss_reports_an_empty_sentence(project):
    write_annotations(project, [footnote_record("chapter_01", 99, "[x] Algo.")])
    out = fp_corpus.build_corpus(project)
    assert out.glosses[0].es_sentence == ""


def test_non_footnote_types_are_not_in_the_corpus(project):
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco.", sub_id="u1"),
            {**footnote_record("chapter_01", 1, "¿ostra?", sub_id="u2"), "type": "word_choice"},
        ],
    )
    out = fp_corpus.build_corpus(project)
    assert len(out.all_notes) == 1


def test_tombstoned_notes_are_not_in_the_corpus(project):
    """The append-only / latest-wins rule is ``store``'s; this just inherits it."""
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco.", sub_id="u1"),
            {**footnote_record("chapter_01", 0, "", sub_id="u1"), "removed": True},
        ],
    )
    assert fp_corpus.build_corpus(project).all_notes == []


def test_chapters_scope_restricts_the_corpus(project):
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco.", sub_id="u1"),
            footnote_record("chapter_02", 0, "[colmena] Caja.", sub_id="u2"),
        ],
    )
    out = fp_corpus.build_corpus(project, chapters=["chapter_02"])
    assert [n.chapter_id for n in out.all_notes] == ["chapter_02"]


def test_already_noted_keys_includes_placeholders(project):
    """A spot the reader marked is off limits even when nothing publishes there."""
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco.", sub_id="u1"),
            footnote_record("chapter_01", 2, "[Sancerre]", sub_id="u2"),
        ],
    )
    assert fp_corpus.already_noted_keys(project) == {
        ("chapter_01", 0),
        ("chapter_01", 2),
    }


def test_style_writes_a_corpus_file_and_returns_counts_only(project):
    """64 notes is not stdout material — the corpus is the artifact."""
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 0, "[ostión] Molusco del Pacífico.", sub_id="u1"),
            footnote_record("chapter_01", 2, "[Sancerre]", sub_id="u2"),
        ],
    )
    out = fp_corpus.style(project)
    assert out["status"] == "ok"
    assert out["counts"] == {"glosses": 1, "placeholders": 1, "total": 2, "orphaned": 0}
    assert out["by_origin"] == {"reader": 2}

    body = (project / ".harness" / "footnotes" / "style_corpus.md").read_text(
        encoding="utf-8"
    )
    assert "Molusco del Pacífico." in body
    assert "[Sancerre]" in body
    # The note text must be in the file and NOT in the payload.
    assert "Molusco" not in str(out)


def test_style_on_a_book_with_no_footnotes_says_so(project):
    out = fp_corpus.style(project)
    assert out["counts"]["total"] == 0
    assert "no footnote glosses" in out["instructions"]


def test_style_reports_orphans(project):
    write_annotations(project, [footnote_record("chapter_01", 99, "[x] Algo.")])
    out = fp_corpus.style(project)
    assert out["counts"]["orphaned"] == 1
    assert out["orphaned"] == ["chapter_01__99__u0000dead"]


def test_rendered_corpus_survives_a_book_with_nothing(project):
    rendered = fp_corpus.render_style_corpus(fp_corpus.StyleCorpus(), "testbook")
    assert "no footnotes to infer a style from" in rendered
    assert "## Placeholders" in rendered
