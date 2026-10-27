"""Tests for the reader home-page library (``/read/``).

Each book is a cover (posters or cards — one markup, two views) with its status
and the badges that explain it, most actionable first (``card_badges``): setup
gaps while pending, chapters translated while translating, then the work that
remains (annotations awaiting review, flags in the selected categories, unread
chapters, blank footnote marks). Complete and archived books carry no badges.
"""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
from web_ui.project_cards import (
    build_project_card,
    card_badges,
    clear_card_cache,
    derive_status,
    find_cover,
)
from web_ui.evaluations import (
    REVIEW_TYPES,
    append_feedback,
    mark_evaluation_stale,
    merge_judge_result,
    save_chunk_evaluation,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def card_project(tmp_path, monkeypatch):
    """A project with one fully translated chunk, style guide and glossary present."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "cardproj"
    (proj_dir / "chunks").mkdir(parents=True)
    (proj_dir / "alignments").mkdir(parents=True)

    (proj_dir / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({"id": "chapter_01_chunk_000",
                    "translated_text": "El gato negro. El perro grande."}),
        encoding="utf-8",
    )
    (proj_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "alignments": []}), encoding="utf-8"
    )
    (proj_dir / "style.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
    (proj_dir / "glossary.json").write_text(
        json.dumps({"terms": [{"english": "cat", "spanish": "gato"}]}), encoding="utf-8"
    )
    (proj_dir / "project.json").write_text(
        json.dumps({"title": "Card Project"}), encoding="utf-8"
    )

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    clear_card_cache()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _card(proj_dir):
    clear_card_cache()
    return build_project_card(proj_dir, proj_dir.name)


def _annotations(proj_dir, records):
    with open(proj_dir / "annotations.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _reviewed(proj_dir, chapter_ids):
    (proj_dir / "reviewed.json").write_text(
        json.dumps({ch: "2026-01-01T00:00:00" for ch in chapter_ids}), encoding="utf-8"
    )


def _blacklist_finding(proj_dir, chunk_id="chapter_01_chunk_000"):
    issue = NormalizedIssue(
        eval_name="blacklist",
        eval_version="1.0.0",
        issue_index=0,
        severity="error",
        message="'negro': flagged term",
        suggestion="reconsider",
        location=NormalizedLocation(
            raw="char 8-13", side="target", char_start=8, char_end=13, match="negro",
        ),
    )
    save_chunk_evaluation(proj_dir, chunk_id, results=[], aggregated={},
                          normalized_issues=[issue])


def _flag_badge(n, label):
    """The flags badge's count and label as the template renders them."""
    return f'<b class="chip-count">{n}</b><span class="chip-label">{label}</span>'


def _dialogue_finding(proj_dir, chunk_id="chapter_01_chunk_000"):
    merge_judge_result(proj_dir, chunk_id, "dialogue", {
        "eval_name": "dialogue",
        "eval_version": "1.0.0",
        "issues": [{"severity": "warning", "message": "use raya",
                    "location": "El perro grande."}],
    })


# ── 1. Setup chips ───────────────────────────────────────────────────────────

def test_setup_chips_hidden_when_style_guide_and_glossary_present(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "No style guide" not in html
    assert "No glossary" not in html


def test_setup_chips_appear_when_missing(client, card_project):
    (card_project / "style.json").unlink()
    (card_project / "glossary.json").unlink()
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "No style guide" in html
    assert "No glossary" in html


def test_empty_glossary_counts_as_missing(client, card_project):
    (card_project / "glossary.json").write_text(json.dumps({"terms": []}), encoding="utf-8")
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "No glossary" in html


def test_null_glossary_terms_do_not_500_the_home_page(client, card_project):
    (card_project / "glossary.json").write_text(json.dumps({"terms": None}), encoding="utf-8")
    clear_card_cache()

    assert _card(card_project)["glossary_count"] == 0
    rv = client.get("/read/")
    assert rv.status_code == 200
    assert "No glossary" in rv.data.decode("utf-8")


def test_non_list_glossary_terms_do_not_500_the_home_page(client, card_project):
    (card_project / "glossary.json").write_text(json.dumps({"terms": {}}), encoding="utf-8")
    clear_card_cache()

    assert _card(card_project)["glossary_count"] == 0
    rv = client.get("/read/")
    assert rv.status_code == 200
    assert "No glossary" in rv.data.decode("utf-8")


def test_home_page_ships_archive_error_i18n(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "archive_error:" in html
    assert "Could not update archive status." in html


# ── 2. Progress ──────────────────────────────────────────────────────────────

def test_progress_hidden_when_fully_translated(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "card-progress" not in html
    assert " translated" not in html


def test_progress_counts_whole_chapters(client, card_project):
    # chapter_01 has a second, untranslated chunk; chapter_02 is done.
    (card_project / "chunks" / "chapter_01_chunk_001.json").write_text(
        json.dumps({"id": "chapter_01_chunk_001", "translated_text": ""}), encoding="utf-8"
    )
    (card_project / "chunks" / "chapter_02_chunk_000.json").write_text(
        json.dumps({"id": "chapter_02_chunk_000", "translated_text": "Hecho."}), encoding="utf-8"
    )
    clear_card_cache()

    card = build_project_card(card_project, "cardproj")
    assert (card["chapters_translated"], card["chapters_total"]) == (1, 2)
    assert (card["translated_chunks"], card["total_chunks"]) == (2, 3)

    html = client.get("/read/").data.decode("utf-8")
    assert "card-progress" in html
    assert 'title="1/2 translated"' in html


def test_not_chunked_chip_when_no_chunks(client, card_project):
    (card_project / "chunks" / "chapter_01_chunk_000.json").unlink()
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "Not chunked" in html
    assert "card-progress" not in html


# ── 3. Awaiting review ───────────────────────────────────────────────────────

def test_awaiting_review_sums_the_three_review_types(card_project):
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
        {"chapter_id": "chapter_01", "es_idx": 1, "type": "inconsistency", "content": "b"},
        {"chapter_id": "chapter_01", "es_idx": 2, "type": "flag", "content": "c"},
        {"chapter_id": "chapter_01", "es_idx": 3, "type": "footnote", "content": "[x] a gloss"},
    ])
    assert _card(card_project)["awaiting_review"] == 3


def test_awaiting_review_honors_tombstones(card_project):
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
        {"chapter_id": "chapter_01", "es_idx": 1, "type": "flag", "content": "b"},
        {"chapter_id": "chapter_01", "es_idx": 1, "removed": True},
    ])
    assert _card(card_project)["awaiting_review"] == 1


def test_unknown_annotation_type_counts_as_flag(card_project):
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "something_new", "content": "a"},
    ])
    assert _card(card_project)["awaiting_review"] == 1


# ── 4. Empty footnotes ───────────────────────────────────────────────────────

def test_empty_footnotes_counts_only_anchor_only_notes(card_project):
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "content": "[Sancerre]"},
        {"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote",
         "content": "[Sancerre] a town in the Loire"},
        {"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote", "content": ""},
    ])
    card = _card(card_project)
    assert card["empty_footnotes"] == 2
    # Footnotes never count as awaiting review — they feed endnotes instead.
    assert card["awaiting_review"] == 0


def test_empty_footnote_chip_rendered(client, card_project):
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "footnote", "content": "[Sancerre]"},
    ])
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert 'title="1 empty note"' in html   # singular for one


# ── 5. Flag counts ───────────────────────────────────────────────────────────

def test_flag_counts_include_coded_and_judge_findings(card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)

    counts = _card(card_project)["flag_counts"]
    assert counts["blacklist"] == 1
    assert counts["dialogue"] == 1
    assert counts["grammar"] == 0


def test_flag_counts_include_stale_chunks(card_project):
    """A marked chunk still contributes its findings to the chapter chip.

    Blanking the chip was the same mistake as hiding the findings: the edit
    ages the *summary*, not the individual defects, and a silent chip read as
    "nothing to see here" on chapters that had plenty.
    """
    _blacklist_finding(card_project)
    mark_evaluation_stale(card_project, "chapter_01_chunk_000", "text edited")

    assert _card(card_project)["flag_counts"]["blacklist"] == 1


def test_summary_by_chapter_no_longer_carries_a_stale_bucket(client, card_project):
    """The rollup reports severities only.

    ``load_project_summary`` stopped emitting a ``stale`` key when the marker
    stopped suppressing counts, and the badge renderer that read it went with
    it. A permanently-zero bucket in the response would be the last piece of
    the retired gate still pretending to mean something.
    """
    # The rollup reads severities out of ``aggregated``, which a judge merge
    # fills in; the coded helper above persists normalized issues only.
    _dialogue_finding(card_project)
    mark_evaluation_stale(card_project, "chapter_01_chunk_000", "text edited")

    body = client.get("/api/project/cardproj/evaluations/summary").get_json()
    assert body["ok"] is True
    assert body["by_chapter"]["chapter_01"] == {"errors": 0, "warnings": 1, "info": 0}


def test_flag_counts_skip_dismissed_findings(card_project):
    _blacklist_finding(card_project)
    append_feedback(card_project, "chapter_01_chunk_000", "blacklist", 0, "resolved")

    assert _card(card_project)["flag_counts"]["blacklist"] == 0


def test_flag_counts_exclude_non_review_categories(card_project):
    issues = [
        NormalizedIssue(
            eval_name=name, eval_version="1.0.0", issue_index=0, severity="warning",
            message="m", suggestion=None,
            location=NormalizedLocation(raw="char 0-3", side="target",
                                        char_start=0, char_end=3, match="El "),
        )
        for name in ("length", "paragraph", "glossary")
    ]
    save_chunk_evaluation(card_project, "chapter_01_chunk_000", results=[],
                          aggregated={}, normalized_issues=issues)

    assert sum(_card(card_project)["flag_counts"].values()) == 0


def test_flag_counts_exclude_source_side_and_spanless_findings(card_project):
    issues = [
        NormalizedIssue(
            eval_name="blacklist", eval_version="1.0.0", issue_index=0,
            severity="error", message="source side", suggestion=None,
            location=NormalizedLocation(raw="char 0-3", side="source",
                                        char_start=0, char_end=3, match="The"),
        ),
        NormalizedIssue(
            eval_name="grammar", eval_version="1.0.0", issue_index=1,
            severity="error", message="no span", suggestion=None,
            location=NormalizedLocation(raw="whole chunk", side="target",
                                        char_start=None, char_end=None, match=None),
        ),
    ]
    save_chunk_evaluation(card_project, "chapter_01_chunk_000", results=[],
                          aggregated={}, normalized_issues=issues)

    assert sum(_card(card_project)["flag_counts"].values()) == 0


# ── 6. Category cookie filters the rendered total ────────────────────────────

def test_flag_chip_counts_all_categories_by_default(client, card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert _flag_badge(2, "flags") in html


def test_flag_chip_respects_category_cookie(client, card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    client.set_cookie("reader_review_types_off",
                      ",".join(t for t in REVIEW_TYPES if t != "dialogue"))
    html = client.get("/read/").data.decode("utf-8")
    assert _flag_badge(1, "flag") in html
    # Only the selected category is ticked in the picker. Scoped to that popup:
    # the Status picker beside it has ticked boxes of its own.
    popup = html.split('id="review-types-popup"')[1].split('class="project-grid ')[0]
    assert popup.count('class="review-type-cb"') == len(REVIEW_TYPES)
    assert popup.count("checked") == 1


def test_unknown_cookie_categories_fall_back_to_all(client, card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    client.set_cookie("reader_review_types_off", "nonsense")
    html = client.get("/read/").data.decode("utf-8")
    assert _flag_badge(2, "flags") in html


def test_legacy_selection_cookie_does_not_hide_categories(client, card_project):
    """The pre-inversion cookie is ignored, not read as a selection.

    It listed what was *on*, so every category added after it was written —
    the editorial judge, and whatever comes next — read as deselected and
    vanished from the reader while its dashboard badges still showed.
    """
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    client.set_cookie("reader_review_types", "dialogue")
    html = client.get("/read/").data.decode("utf-8")
    assert _flag_badge(2, "flags") in html
    popup = html.split('id="review-types-popup"')[1].split('class="project-grid ')[0]
    assert popup.count("checked") == len(REVIEW_TYPES)


# ── 7. Cache invalidation ────────────────────────────────────────────────────

def test_cache_invalidates_when_a_chunk_gains_a_translation(card_project):
    chunk = card_project / "chunks" / "chapter_01_chunk_001.json"
    chunk.write_text(json.dumps({"id": "chapter_01_chunk_001", "translated_text": ""}),
                     encoding="utf-8")
    clear_card_cache()

    first = build_project_card(card_project, "cardproj")
    assert (first["translated_chunks"], first["total_chunks"]) == (1, 2)

    chunk.write_text(json.dumps({"id": "chapter_01_chunk_001",
                                 "translated_text": "Un texto nuevo."}), encoding="utf-8")
    second = build_project_card(card_project, "cardproj")
    assert (second["translated_chunks"], second["total_chunks"]) == (2, 2)


def test_cache_hit_returns_the_same_card(card_project):
    clear_card_cache()
    first = build_project_card(card_project, "cardproj")
    assert build_project_card(card_project, "cardproj") is first


def test_cache_invalidates_when_an_annotation_is_added(card_project):
    clear_card_cache()
    assert build_project_card(card_project, "cardproj")["awaiting_review"] == 0

    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
    ])
    assert build_project_card(card_project, "cardproj")["awaiting_review"] == 1


# ── 8. Untranslated projects skip the work chips ─────────────────────────────

def test_untranslated_project_renders_no_work_chips(client, card_project):
    (card_project / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({"id": "chapter_01_chunk_000", "translated_text": ""}), encoding="utf-8"
    )
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
    ])
    _blacklist_finding(card_project)
    clear_card_cache()

    card = build_project_card(card_project, "cardproj")
    assert card["awaiting_review"] == 0
    assert sum(card["flag_counts"].values()) == 0

    html = client.get("/read/").data.decode("utf-8")
    # Only the progress badge: the alignment left over from the fixture must
    # not surface as an unread chapter in a book with nothing translated.
    assert 'title="0/1 translated"' in html
    assert "unread" not in _grid(html)
    assert "project-chip-flags" not in html


def test_complete_book_shows_no_badges(client, card_project):
    _reviewed(card_project, ["chapter_01"])
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert 'data-status="complete"' in html
    assert 'class="chip ' not in html
    assert "Nothing pending" not in html


def test_flags_badge_ships_at_zero_for_the_picker_to_reveal(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    flags = html.split("project-chip-flags")[1].split("</span></span>")[0] + "</span>"
    assert "data-zero hidden" in flags
    assert _flag_badge(0, "flags") in flags


def test_review_badge_when_work_remains(client, card_project):
    _reviewed(card_project, ["chapter_01"])
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
    ])
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "Nothing pending" not in html
    assert "1 to review" in html


# ── 8b. Unread chapters ──────────────────────────────────────────────────────

def test_unread_counts_chapters_with_no_reviewed_mark(card_project):
    (card_project / "alignments" / "chapter_02.json").write_text(
        json.dumps({"chapter_id": "chapter_02", "alignments": []}), encoding="utf-8"
    )
    assert _card(card_project)["unread_chapters"] == 2

    _reviewed(card_project, ["chapter_01"])
    assert _card(card_project)["unread_chapters"] == 1

    _reviewed(card_project, ["chapter_01", "chapter_02"])
    assert _card(card_project)["unread_chapters"] == 0


def test_unread_ignores_reviewed_marks_for_chapters_that_are_gone(card_project):
    # reviewed.json outlives a re-import, so a stale key must not push the count
    # negative or mask a chapter that really is unread.
    _reviewed(card_project, ["chapter_01", "chapter_99"])
    assert _card(card_project)["unread_chapters"] == 0


def test_unread_survives_a_malformed_reviewed_file(card_project):
    (card_project / "reviewed.json").write_text("{not json", encoding="utf-8")
    assert _card(card_project)["unread_chapters"] == 1


def test_marking_a_chapter_reviewed_busts_the_card_cache(card_project):
    # reviewed.json is the only file that moves when you mark a chapter read, so
    # without it in the fingerprint the home page would keep a stale count.
    assert build_project_card(card_project, "cardproj")["unread_chapters"] == 1
    _reviewed(card_project, ["chapter_01"])
    assert build_project_card(card_project, "cardproj")["unread_chapters"] == 0


def test_badges_follow_priority_order(client, card_project):
    # Review notes, then flags, then unread: what to act on first comes first,
    # so a poster that only has room for two keeps the right two.
    _annotations(card_project, [
        {"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice", "content": "a"},
    ])
    _dialogue_finding(card_project)
    clear_card_cache()

    chips = _grid(client.get("/read/").data.decode("utf-8")).split('class="chips"')[1]
    assert chips.index("1 to review") < chips.index("project-chip-flags") < chips.index("1 unread")


# ── 9. Card navigation (tile link, ⋮ menu) ───────────────────────────────────

def _grid(html):
    """The library grid: the cards, without the header's pickers."""
    return html.split('id="project-grid"')[1].split('id="new-project-modal"')[0]


def _header(html):
    """The first card, up to its ⋮ menu."""
    return html.split('<article class="project-card')[1].split('class="card-menu"')[0]


def _menu(html):
    """The first card's ⋮ menu: button plus popup."""
    return html.split('class="card-menu"')[1].split("</article>")[0]


def test_card_title_links_to_the_reader(client, card_project):
    header = _header(client.get("/read/").data.decode("utf-8"))
    assert 'class="card-stretch" href="/read/cardproj"' in header
    assert "Card Project" in header


def test_card_shows_one_title_in_the_ui_language(client, card_project):
    (card_project / "project.json").write_text(
        json.dumps({"title": "Card Project", "spanish_title": "Proyecto"}),
        encoding="utf-8",
    )
    clear_card_cache()

    header = _header(client.get("/read/").data.decode("utf-8"))
    assert "Card Project" in header and "Proyecto" not in header

    client.set_cookie("reader_lang", "es")
    header = _header(client.get("/read/").data.decode("utf-8"))
    assert "Proyecto" in header and "Card Project" not in header
    # One link for the whole tile, named for the book.
    assert header.count("<a ") == 1
    assert 'aria-label="Proyecto"' in header


def test_spanish_ui_falls_back_to_the_english_title(client, card_project):
    client.set_cookie("reader_lang", "es")
    header = _header(client.get("/read/").data.decode("utf-8"))
    assert '<h2 class="card-title">Card Project</h2>' in header


def test_card_title_falls_back_to_the_dashboard_without_alignments(client, card_project):
    (card_project / "alignments" / "chapter_01.json").unlink()
    clear_card_cache()

    header = _header(client.get("/read/").data.decode("utf-8"))
    assert 'href="/project/cardproj"' in header
    assert "/read/cardproj" not in header


def test_badges_go_where_the_tile_goes(client, card_project):
    # Needs review (its one chapter is unread): the badges open the reader too.
    header = _header(client.get("/read/").data.decode("utf-8"))
    assert 'class="chips" data-href="/read/cardproj"' in header


def test_a_pending_books_badges_go_to_the_dashboard(client, card_project):
    # Pending with alignments: the tile opens the reader, but "No style guide"
    # is fixed on the dashboard.
    (card_project / "style.json").unlink()
    clear_card_cache()

    header = _header(client.get("/read/").data.decode("utf-8"))
    assert 'class="card-stretch" href="/read/cardproj"' in header
    assert 'class="chips" data-href="/project/cardproj"' in header


def test_card_menu_holds_the_dashboard_link_and_the_archive_toggle(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    menu = _menu(html)
    assert 'aria-label="More actions"' in menu
    assert 'href="/project/cardproj"' in menu
    assert ">Dashboard<" in menu
    assert "card-archive-btn" in menu
    assert ">Archive<" in menu
    assert 'data-archived="0"' in menu
    # The dropdown is gone: status is derived now, not chosen.
    assert "status-select" not in html


def test_card_menu_offers_unarchive_for_an_archived_project(client, card_project):
    (card_project / "project.json").write_text(
        json.dumps({"title": "Card Project", "archived": True}), encoding="utf-8"
    )
    clear_card_cache()

    menu = _menu(client.get("/read/").data.decode("utf-8"))
    assert ">Unarchive<" in menu
    assert 'data-archived="1"' in menu


# ── 10. The cookie endpoint ──────────────────────────────────────────────────

def test_set_review_types_round_trips(client, card_project):
    rv = client.post("/api/set-review-types", json={"types": ["dialogue", "grammar"]})
    assert rv.status_code == 200
    # Normalized to REVIEW_TYPES order, not the caller's.
    assert rv.get_json()["types"] == ["grammar", "dialogue"]
    # Stored inverted, so a category added later is on until it is unticked.
    off = client.get_cookie("reader_review_types_off").decoded_value.split(",")
    assert set(off) == {t for t in REVIEW_TYPES if t not in ("grammar", "dialogue")}

    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()
    html = client.get("/read/").data.decode("utf-8")
    assert _flag_badge(1, "flag") in html


def test_set_review_types_rejects_unknown_categories(client):
    rv = client.post("/api/set-review-types", json={"types": ["dialogue", "bogus"]})
    assert rv.status_code == 400
    assert "bogus" in rv.get_json()["error"]


def test_set_review_types_rejects_non_list(client):
    assert client.post("/api/set-review-types", json={"types": "dialogue"}).status_code == 400


# ── 11. Derived status ───────────────────────────────────────────────────────

def _status(**over):
    """derive_status with a "set up, translated, read, nothing open" baseline."""
    kwargs = dict(archived=False, setup_complete=True, total_chunks=1,
                  fully_translated=True, chapter_count=1, read_chapters=1,
                  awaiting_review=0, empty_footnotes=0, judge_findings=0)
    kwargs.update(over)
    return derive_status(**kwargs)


def test_status_is_complete_when_nothing_is_outstanding():
    assert _status() == "complete"


def test_missing_setup_is_pending():
    assert _status(setup_complete=False) == "pending"


def test_an_unchunked_project_is_pending():
    # Importing a book must not mark it anything further along.
    assert _status(total_chunks=0, fully_translated=False,
                   chapter_count=0, read_chapters=0) == "pending"


def test_missing_setup_outranks_finished_translation():
    assert _status(setup_complete=False, read_chapters=0) == "pending"


def test_untranslated_project_needs_translation():
    assert _status(fully_translated=False, chapter_count=0, read_chapters=0) == "needs_translation"


def test_partial_translation_needs_translation_even_when_read():
    """Reading ahead doesn't move a book past the translation stage."""
    assert _status(fully_translated=False, chapter_count=3, read_chapters=3) == "needs_translation"


def test_an_unread_chapter_needs_review():
    assert _status(chapter_count=3, read_chapters=1) == "needs_review"


def test_a_never_opened_translated_project_needs_review():
    assert _status(chapter_count=3, read_chapters=0) == "needs_review"


def test_translated_with_no_chapters_needs_review_not_complete():
    # No alignments yet means nothing has been read, so it can't be finished.
    assert _status(chapter_count=0, read_chapters=0) == "needs_review"


def test_annotations_awaiting_review_need_review():
    assert _status(awaiting_review=1) == "needs_review"


def test_blank_footnotes_need_review():
    assert _status(empty_footnotes=1) == "needs_review"


def test_judge_findings_need_review():
    assert _status(judge_findings=1) == "needs_review"


def test_archived_wins_over_a_would_be_complete_project():
    assert _status(archived=True) == "archived"


def test_archived_wins_over_a_never_opened_project():
    assert _status(archived=True, setup_complete=False, total_chunks=0) == "archived"


def test_card_status_tracks_what_has_been_read(card_project):
    assert _card(card_project)["status"] == "needs_review"

    _reviewed(card_project, ["chapter_01"])
    assert _card(card_project)["status"] == "complete"


def test_missing_setup_makes_a_card_pending(card_project):
    _reviewed(card_project, ["chapter_01"])
    (card_project / "style.json").unlink()
    assert _card(card_project)["status"] == "pending"


def test_an_untranslated_chunk_makes_a_card_need_translation(card_project):
    _reviewed(card_project, ["chapter_01"])
    (card_project / "chunks" / "chapter_01_chunk_001.json").write_text(
        json.dumps({"id": "chapter_01_chunk_001", "translated_text": ""}), encoding="utf-8"
    )
    assert _card(card_project)["status"] == "needs_translation"


def test_coded_flags_alone_do_not_stop_a_project_completing(card_project):
    """The coded checkers are too noisy to gate on."""
    _reviewed(card_project, ["chapter_01"])
    _blacklist_finding(card_project)
    card = _card(card_project)
    assert card["flag_counts"]["blacklist"] == 1
    assert card["status"] == "complete"


def test_a_live_judge_finding_holds_a_card_at_needs_review(card_project):
    _reviewed(card_project, ["chapter_01"])
    _dialogue_finding(card_project)
    assert _card(card_project)["status"] == "needs_review"

    append_feedback(card_project, "chapter_01_chunk_000", "dialogue", 0, "resolved")
    assert _card(card_project)["status"] == "complete"


def test_reviewed_chapters_that_no_longer_exist_do_not_count_as_read(card_project):
    _reviewed(card_project, ["chapter_99"])
    assert _card(card_project)["status"] == "needs_review"


def test_the_legacy_status_field_still_archives(card_project):
    """Projects written before the switch carry status, not archived."""
    (card_project / "project.json").write_text(
        json.dumps({"title": "Card Project", "status": "archived"}), encoding="utf-8"
    )
    card = _card(card_project)
    assert card["archived"] is True
    assert card["status"] == "archived"


def test_an_explicit_archived_false_beats_the_legacy_field(card_project):
    (card_project / "project.json").write_text(
        json.dumps({"title": "Card Project", "status": "archived", "archived": False}),
        encoding="utf-8",
    )
    assert _card(card_project)["status"] == "needs_review"


def test_the_old_hand_set_statuses_no_longer_stick(card_project):
    """A stale "complete" from the dropdown era must not outrank the files."""
    (card_project / "project.json").write_text(
        json.dumps({"title": "Card Project", "status": "complete"}), encoding="utf-8"
    )
    assert _card(card_project)["status"] == "needs_review"


def test_archiving_via_the_api_updates_the_rendered_card(client, card_project):
    rv = client.patch("/api/project/cardproj/archived", json={"archived": True})
    assert rv.get_json() == {"ok": True, "archived": True, "status": "archived"}

    html = client.get("/read/").data.decode("utf-8")
    assert 'data-status="archived"' in html
    assert "card-archived" in html


# ── 12. The status filter ────────────────────────────────────────────────────

def test_status_picker_hides_archived_by_default(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    picker = html.split('id="status-filter-popup"')[1].split('id="review-topbar-group"')[0]
    assert picker.count('class="status-filter-cb"') == 5
    assert picker.count("checked") == 4
    # Archived is the one left unticked.
    archived_box = picker.split('value="archived"')[1].split(">")[0]
    assert "checked" not in archived_box


def test_status_filter_cookie_is_honored(client, card_project):
    client.set_cookie("reader_status_filter", "complete,archived")
    html = client.get("/read/").data.decode("utf-8")
    picker = html.split('id="status-filter-popup"')[1].split('id="review-topbar-group"')[0]
    assert picker.count("checked") == 2
    assert "checked" in picker.split('value="complete"')[1].split(">")[0]


def test_an_emptied_status_cookie_means_no_filter(client, card_project):
    """Unticking every box is "show everything", not "show nothing"."""
    client.set_cookie("reader_status_filter", "")
    html = client.get("/read/").data.decode("utf-8")
    picker = html.split('id="status-filter-popup"')[1].split('id="review-topbar-group"')[0]
    assert picker.count("checked") == 0
    # The cards still render; reader_projects.js reads an empty selection as
    # "no filter" and leaves every card visible.
    assert 'class="project-card' in html


def test_a_legacy_in_progress_cookie_ticks_both_successors(client, card_project):
    """A cookie from before the split must not hide the two new statuses."""
    client.set_cookie("reader_status_filter", "in_progress,complete")
    html = client.get("/read/").data.decode("utf-8")
    picker = html.split('id="status-filter-popup"')[1].split('id="review-topbar-group"')[0]
    assert picker.count("checked") == 3
    for status in ("needs_translation", "needs_review", "complete"):
        assert "checked" in picker.split(f'value="{status}"')[1].split(">")[0]


def test_unknown_status_cookie_values_are_dropped(client, card_project):
    client.set_cookie("reader_status_filter", "complete,nonsense")
    html = client.get("/read/").data.decode("utf-8")
    picker = html.split('id="status-filter-popup"')[1].split('id="review-topbar-group"')[0]
    assert picker.count("checked") == 1


def test_set_status_filter_round_trips(client):
    rv = client.post("/api/set-status-filter", json={"statuses": ["archived", "pending"]})
    assert rv.status_code == 200
    # Normalized to PROJECT_STATUSES order, not the caller's.
    assert rv.get_json()["statuses"] == ["pending", "archived"]


def test_set_status_filter_rejects_unknown_statuses(client):
    rv = client.post("/api/set-status-filter", json={"statuses": ["pending", "bogus"]})
    assert rv.status_code == 400
    assert "bogus" in rv.get_json()["error"]


def test_set_status_filter_rejects_non_list(client):
    assert client.post("/api/set-status-filter", json={"statuses": "pending"}).status_code == 400


# ── 13. Badges per status (card_badges) ──────────────────────────────────────

def _badge_card(**over):
    card = {
        "status": "needs_review",
        "has_style_guide": True,
        "glossary_count": 3,
        "total_chunks": 10,
        "translated_chunks": 10,
        "chapters_total": 5,
        "chapters_translated": 5,
        "awaiting_review": 0,
        "unread_chapters": 0,
        "empty_footnotes": 0,
        "flag_counts": {t: 0 for t in REVIEW_TYPES},
    }
    card.update(over)
    return card


def _kinds(card, selected=REVIEW_TYPES):
    return [b["kind"] for b in card_badges(card, selected)]


def test_review_badges_in_priority_order():
    card = _badge_card(awaiting_review=4, unread_chapters=2, empty_footnotes=1,
                       flag_counts={**{t: 0 for t in REVIEW_TYPES}, "dialogue": 3})
    assert _kinds(card) == ["review", "flags", "unread", "footnotes"]
    assert card_badges(card, REVIEW_TYPES)[1]["count"] == 3


def test_flags_badge_is_kept_at_zero_and_others_are_dropped():
    assert card_badges(_badge_card(unread_chapters=1), REVIEW_TYPES) == [
        {"kind": "flags", "count": 0},
        {"kind": "unread", "count": 1},
    ]


def test_flags_badge_sums_only_the_selected_categories():
    card = _badge_card(flag_counts={**{t: 0 for t in REVIEW_TYPES},
                                    "dialogue": 2, "dictionary": 5})
    flags = card_badges(card, ["dialogue"])[0]
    assert flags == {"kind": "flags", "count": 2}
    # Nothing selected reads as everything, the same as the picker.
    assert card_badges(card, [])[0]["count"] == 7


def test_translation_badges_lead_with_chapters_translated():
    card = _badge_card(status="needs_translation", translated_chunks=4,
                       chapters_translated=4, chapters_total=15, unread_chapters=4)
    badges = card_badges(card, REVIEW_TYPES)
    assert badges[0] == {"kind": "translated", "count": "4/15"}
    assert [b["kind"] for b in badges] == ["translated", "unread"]


def test_pending_badges_name_the_missing_setup():
    card = _badge_card(status="pending", has_style_guide=False, glossary_count=0,
                       total_chunks=0, translated_chunks=0)
    assert card_badges(card, REVIEW_TYPES) == [
        {"kind": "no_style_guide", "count": None},
        {"kind": "no_glossary", "count": None},
        {"kind": "not_chunked", "count": None},
    ]


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_finished_books_have_no_badges(status):
    assert card_badges(_badge_card(status=status, unread_chapters=3), REVIEW_TYPES) == []


# ── 14. Covers ───────────────────────────────────────────────────────────────

def _image(path, size=(900, 1350), mode="RGB"):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, (120, 60, 30) if mode == "RGB" else (120, 60, 30, 128)).save(path)


def test_cover_prefers_the_epub_cover_then_the_source_cover(card_project):
    images = card_project / "images"
    assert find_cover(card_project) is None

    _image(images / "frontispiece.jpg")
    assert find_cover(card_project) is None   # an illustration is not a cover

    _image(images / "pg2770.cover.medium.jpg")
    assert find_cover(card_project).name == "pg2770.cover.medium.jpg"

    _image(images / "cover.png")
    assert find_cover(card_project).name == "cover.png"


def test_adding_a_cover_busts_the_card_cache(card_project):
    clear_card_cache()
    assert build_project_card(card_project, "cardproj")["cover"] is None
    _image(card_project / "images" / "cover.jpg")
    card = build_project_card(card_project, "cardproj")
    assert card["cover"] == "cover.jpg"
    assert card["cover_version"] > 0


def test_card_renders_the_thumbnail_or_a_generated_cover(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "cover-gen cloth-" in html
    assert "cover-thumb" not in html

    _image(card_project / "images" / "cover.jpg")
    clear_card_cache()
    html = client.get("/read/").data.decode("utf-8")
    assert 'src="/projects/cardproj/cover-thumb?v=' in html
    assert "cover-gen" not in _grid(html)


def test_cover_thumb_is_a_small_jpeg(client, card_project):
    from io import BytesIO

    from PIL import Image

    _image(card_project / "images" / "cover.png", size=(1600, 2400), mode="RGBA")
    rv = client.get("/projects/cardproj/cover-thumb")
    assert rv.status_code == 200
    assert rv.mimetype == "image/jpeg"
    with Image.open(BytesIO(rv.data)) as im:
        assert im.size[0] <= 420 and im.size[1] <= 630
        assert im.mode == "RGB"
    assert "max-age" in rv.headers["Cache-Control"]

    again = client.get("/projects/cardproj/cover-thumb",
                       headers={"If-None-Match": rv.headers["ETag"]})
    assert again.status_code == 304


def test_cover_thumb_404s_without_a_cover(client, card_project):
    assert client.get("/projects/cardproj/cover-thumb").status_code == 404


def test_cover_thumb_rejects_a_bad_id(client, card_project):
    assert client.get("/projects/..%2Fx/cover-thumb").status_code in (400, 404)


def test_a_corrupt_cover_404s_rather_than_500s(client, card_project):
    images = card_project / "images"
    images.mkdir()
    (images / "cover.jpg").write_bytes(b"not a jpeg at all")
    assert client.get("/projects/cardproj/cover-thumb").status_code == 404


def test_an_oversized_cover_404s_rather_than_500s(client, card_project, monkeypatch):
    from PIL import Image

    _image(card_project / "images" / "cover.png", size=(1600, 2400))
    # Past twice the limit, Pillow raises DecompressionBombError.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert client.get("/projects/cardproj/cover-thumb").status_code == 404


# ── 15. Library view (posters / cards) ───────────────────────────────────────

def test_library_defaults_to_posters(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert 'class="project-grid library-posters"' in html
    assert 'data-view="posters"' in html
    posters_btn = html.split('data-view="posters"')[1].split(">")[0]
    assert 'aria-pressed="true"' in posters_btn


def test_set_library_view_round_trips(client, card_project):
    rv = client.post("/api/set-library-view", json={"view": "cards"})
    assert rv.status_code == 200
    assert rv.get_json() == {"ok": True, "view": "cards"}
    html = client.get("/read/").data.decode("utf-8")
    assert 'class="project-grid library-cards"' in html


def test_set_library_view_rejects_unknown_views(client):
    assert client.post("/api/set-library-view", json={"view": "shelves"}).status_code == 400


def test_unknown_library_view_cookie_falls_back_to_posters(client, card_project):
    client.set_cookie("reader_library_view", "shelves")
    html = client.get("/read/").data.decode("utf-8")
    assert 'class="project-grid library-posters"' in html


# ── 16. Order ────────────────────────────────────────────────────────────────

def _sibling(card_project, pid, title, translated=True, reviewed=False):
    proj = card_project.parent / pid
    (proj / "chunks").mkdir(parents=True)
    (proj / "alignments").mkdir()
    (proj / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({"id": "chapter_01_chunk_000", "translated_text": "Hola." if translated else ""}),
        encoding="utf-8")
    (proj / "alignments" / "chapter_01.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "alignments": []}), encoding="utf-8")
    (proj / "style.json").write_text("{}", encoding="utf-8")
    (proj / "glossary.json").write_text(json.dumps({"terms": [{"english": "a"}]}), encoding="utf-8")
    (proj / "project.json").write_text(json.dumps({"title": title}), encoding="utf-8")
    if reviewed:
        _reviewed(proj, ["chapter_01"])
    return proj


def test_books_needing_work_come_first_then_by_title(client, card_project):
    # cardproj ("Card Project") needs review: its one chapter is unread.
    _sibling(card_project, "done", "Aardvark", reviewed=True)           # complete
    _sibling(card_project, "wip", "Zebra", translated=False)            # needs translation
    _sibling(card_project, "other", "Ábaco")                            # needs review
    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    clear_card_cache()

    grid = _grid(client.get("/read/").data.decode("utf-8"))
    order = [chunk.split('"')[0] for chunk in grid.split('data-status="')[1:]]
    titles = [h.split("<")[0] for h in grid.split('<h2 class="card-title">')[1:]]
    assert order == ["needs_review", "needs_review", "needs_translation", "complete"]
    # Accents fold for sorting: "Ábaco" files under A, ahead of "Card Project".
    assert titles == ["Ábaco", "Card Project", "Zebra", "Aardvark"]
