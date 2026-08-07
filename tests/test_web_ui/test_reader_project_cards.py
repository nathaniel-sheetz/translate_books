"""Tests for the reworked reader home-page project cards (``/read/``).

The card progressively discloses: setup facts only while something is missing,
chunk progress only while translation is unfinished, and — once any chunk is
translated — the work that remains (annotations awaiting review, blank footnote
marks, evaluator/judge flags in the selected categories).
"""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
from web_ui.project_cards import build_project_card, clear_card_cache
from web_ui.evaluations import (
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


# ── 2. Progress ──────────────────────────────────────────────────────────────

def test_progress_hidden_when_fully_translated(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "project-progress" not in html
    assert "1/1 chunks" not in html


def test_progress_shown_while_partially_translated(client, card_project):
    (card_project / "chunks" / "chapter_01_chunk_001.json").write_text(
        json.dumps({"id": "chapter_01_chunk_001", "translated_text": ""}), encoding="utf-8"
    )
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "project-progress" in html
    assert "1/2 chunks" in html


def test_not_chunked_chip_when_no_chunks(client, card_project):
    (card_project / "chunks" / "chapter_01_chunk_000.json").unlink()
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "Not chunked" in html
    assert "project-progress" not in html


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
    assert "1 empty notes" in html


# ── 5. Flag counts ───────────────────────────────────────────────────────────

def test_flag_counts_include_coded_and_judge_findings(card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)

    counts = _card(card_project)["flag_counts"]
    assert counts["blacklist"] == 1
    assert counts["dialogue"] == 1
    assert counts["grammar"] == 0


def test_flag_counts_skip_stale_chunks(card_project):
    _blacklist_finding(card_project)
    mark_evaluation_stale(card_project, "chapter_01_chunk_000", "text edited")

    assert _card(card_project)["flag_counts"]["blacklist"] == 0


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
    assert '<span class="chip-flag-count">2</span> <span class="chip-flag-label">flags</span>' in html


def test_flag_chip_respects_category_cookie(client, card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    client.set_cookie("reader_review_types", "dialogue")
    html = client.get("/read/").data.decode("utf-8")
    assert '<span class="chip-flag-count">1</span> <span class="chip-flag-label">flag</span>' in html
    # Only the selected category is ticked in the picker.
    assert html.count('class="review-type-cb"') == 6
    assert html.count("checked") == 1


def test_unknown_cookie_categories_fall_back_to_all(client, card_project):
    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()

    client.set_cookie("reader_review_types", "nonsense")
    html = client.get("/read/").data.decode("utf-8")
    assert '<span class="chip-flag-count">2</span> <span class="chip-flag-label">flags</span>' in html


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
    assert "project-work-chips" not in html
    assert "0/1 chunks" in html


def test_clean_chip_when_nothing_is_pending(client, card_project):
    _reviewed(card_project, ["chapter_01"])
    clear_card_cache()

    html = client.get("/read/").data.decode("utf-8")
    assert "Nothing pending" in html
    assert "project-chip-flags" in html   # present but hidden, for the JS to re-sum


def test_no_clean_chip_when_work_remains(client, card_project):
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


def test_unread_chip_renders_first_and_suppresses_the_clean_chip(client, card_project):
    html = client.get("/read/").data.decode("utf-8")
    assert "1 unread" in html
    # A book you have not read through has work outstanding: the read-through.
    assert "Nothing pending" not in html
    chips = html.split('class="project-chips project-work-chips"')[1]
    assert chips.index("1 unread") < chips.index("project-chip-flags")


# ── 9. The cookie endpoint ───────────────────────────────────────────────────

def test_set_review_types_round_trips(client, card_project):
    rv = client.post("/api/set-review-types", json={"types": ["dialogue", "grammar"]})
    assert rv.status_code == 200
    # Normalized to REVIEW_TYPES order, not the caller's.
    assert rv.get_json()["types"] == ["grammar", "dialogue"]

    _blacklist_finding(card_project)
    _dialogue_finding(card_project)
    clear_card_cache()
    html = client.get("/read/").data.decode("utf-8")
    assert '<span class="chip-flag-count">1</span> <span class="chip-flag-label">flag</span>' in html


def test_set_review_types_rejects_unknown_categories(client):
    rv = client.post("/api/set-review-types", json={"types": ["dialogue", "bogus"]})
    assert rv.status_code == 400
    assert "bogus" in rv.get_json()["error"]


def test_set_review_types_rejects_non_list(client):
    assert client.post("/api/set-review-types", json={"types": "dialogue"}).status_code == 400
