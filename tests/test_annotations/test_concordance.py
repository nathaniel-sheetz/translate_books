"""Book-wide term search: whole-word matching, guards, and rendering."""

from __future__ import annotations

from src.annotations.concordance import (
    BookIndex,
    format_concordance,
    is_searchable_term,
    search_terms,
)
from src.utils.text_utils import count_folded, fold, iter_folded

from tests.test_annotations.conftest import write_alignment


def test_whole_word_matching_rejects_substrings():
    """"test" must not match inside protestaba/detestable — the bug that made
    a bare-word annotation return 11 useless hits."""
    hay = fold("protestaba contra el test detestable")
    assert count_folded(hay, fold("test"), whole_word=False) == 3
    assert count_folded(hay, fold("test"), whole_word=True) == 1


def test_iter_folded_whole_word_spans_map_to_the_original():
    text = "protestaba contra el test"
    spans = list(iter_folded(text, fold("test"), whole_word=True))
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "test"


def test_search_is_accent_and_case_folded(project):
    index = BookIndex(project)
    result = index.search("OSTION")
    assert result["total"] == 2
    assert set(result["by_chapter"]) == {"chapter_01"}


def test_search_skips_the_annotated_sentence(project):
    index = BookIndex(project)
    full = index.search("ostión")
    skipped = index.search("ostión", skip=("chapter_01", 0))
    assert full["total"] == 2
    assert skipped["total"] == 1


def test_source_side_hits_carry_the_paired_translation(project):
    """This is what exposes a competing rendering of one English term."""
    index = BookIndex(project)
    result = index.search("oyster", side="en")
    assert result["total"] == 3
    renderings = {h["es"] for h in result["hits"]}
    assert "La ostra sabía distinta." in renderings
    assert "Comimos ostión en el puerto." in renderings


def test_short_and_long_terms_are_not_searched():
    assert is_searchable_term("en") is False          # function word
    assert is_searchable_term("ostión") is True
    assert is_searchable_term("error? esto es una frase larga del lector") is False


def test_search_terms_drops_empty_and_duplicate_queries(project):
    index = BookIndex(project)
    results = search_terms(index, ["ostión", "ostión", "", "zzzznotfound"], sides=("es",))
    assert [r["term"] for r in results] == ["ostión"]


def test_too_common_terms_report_counts_but_quote_nothing(tmp_path):
    project = tmp_path / "big"
    project.mkdir()
    write_alignment(
        project,
        "chapter_01",
        [(i, f"La casa numero {i} es grande.", f"House {i} is big.") for i in range(200)],
    )
    index = BookIndex(project)
    result = index.search("casa")
    assert result["total"] == 200
    assert result["too_common"] is True
    assert result["hits"] == []
    rendered = format_concordance([result])
    assert "200 occurrence(s)" in rendered
    assert "used throughout the book" in rendered


def test_format_concordance_distinguishes_nothing_found_from_not_searched():
    assert "no book-wide occurrences" in format_concordance([])


def test_snippets_are_contiguous_slices(project):
    """kwic_window re-joins on whitespace ("relámpago , sigue"); evidence must not."""
    index = BookIndex(project)
    result = index.search("ostión", skip=("chapter_01", 4))
    assert result["hits"][0]["snippet"] == "Comimos ostión en el puerto."


def test_missing_alignments_dir_yields_an_empty_index(tmp_path):
    project = tmp_path / "empty"
    project.mkdir()
    assert len(BookIndex(project)) == 0
