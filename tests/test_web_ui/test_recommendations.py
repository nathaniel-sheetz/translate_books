"""The read-only recommendations screen (``/recommendations/<id>``).

What these tests pin is *reading*, not acting. Every other surface for an LLM
opinion in this repo exists to do something with it — the reader's Review Mode
tints one sentence at a time, the inbox offers a checkbox and truncates the
recommendation to 600 characters. This page's whole job is to show the model's
prose in full against the sentence it concerns, so the assertions are about
exactly that: the untruncated explanation, the reasoning the inbox throws away,
and the two sentences either side coming from the right rows.

The sparse-index case is the one that would fail silently. An N:1 alignment
group consumes the indices of the sentences it swallowed, so ``es_idx`` is
non-contiguous in most of this corpus; a neighbour lookup written as
``es_idx ± 1`` would return nothing at all on those chapters and simply render
no context, on the sentences that were hardest to align. The fixture's alignment
is deliberately sparse for that reason.
"""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
from web_ui.evaluations import merge_judge_result, save_chunk_evaluation


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# Six sentences in one chunk. The pipeline stores this verbatim as the chunk's
# `translated_text`, and the alignment rows below index into it.
_TRANSLATED = (
    "El gato negro dormia. "
    "La casa era grande. "
    "Tenia dos pisos. "
    "Nadie vivia alli. "
    "El perro ladro fuerte. "
    "Nadie respondio."
)

# Four rows over those six sentences: the second is an N:1 group that swallowed
# sentences 1-3 and consumed their indices, which is what `_group_nto1` leaves
# behind and why `es_idx` runs 0, 1, 4, 5 here. The row *after* es_idx 1 is
# es_idx 4; `es_idx + 1` names nothing at all.
_ROWS = [
    {"es_idx": 0, "es": "El gato negro dormia.", "en": "The black cat slept."},
    {"es_idx": 1, "es_indices": [1, 2, 3],
     "es": "La casa era grande. Tenia dos pisos. Nadie vivia alli.",
     "en": "The house was large, with two floors, and nobody lived there."},
    {"es_idx": 4, "es": "El perro ladro fuerte.", "en": "The dog barked loudly."},
    {"es_idx": 5, "es": "Nadie respondio.", "en": "Nobody answered."},
]


@pytest.fixture
def book(tmp_path, monkeypatch):
    """A one-chapter book with a sparse alignment and one chunk."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "recbook"
    (proj_dir / "alignments").mkdir(parents=True)
    (proj_dir / "chunks").mkdir(parents=True)

    (proj_dir / "project.json").write_text(
        json.dumps({"title": "Rec Book"}), encoding="utf-8"
    )
    (proj_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps({
            "chapter_id": "chapter_01",
            "project_id": "recbook",
            "alignments": [
                dict(row, en_idx=i, confidence="high",
                     chunk_id="chapter_01_chunk_000")
                for i, row in enumerate(_ROWS)
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (proj_dir / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({"id": "chapter_01_chunk_000", "translated_text": _TRANSLATED},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


# The prose a truncating surface would cut: longer than the inbox's 600-char cap.
_LONG_MESSAGE = (
    "The dialogue in this passage uses an em dash where the style guide "
    "requires a raya, and the vocative is unpunctuated. " + ("x" * 700)
)


def plant_judge(proj_dir, *, location="El perro ladro fuerte.",
                message=_LONG_MESSAGE, judge="dialogue"):
    """Persist one judge issue anchored by its verbatim excerpt."""
    merge_judge_result(
        proj_dir, "chapter_01_chunk_000", judge,
        {
            "eval_name": judge,
            "eval_version": "1.0.0",
            "issues": [{
                "severity": "error",
                "message": message,
                "location": location,
                "suggestion": "—El perro ladró fuerte.",
                "category": "STYLE_GUIDE",
            }],
        },
    )


def plant_coded(proj_dir):
    """Persist one target-side coded finding: 'negro' at char 8 of the chunk."""
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
    save_chunk_evaluation(
        proj_dir, "chapter_01_chunk_000",
        results=[], aggregated={}, normalized_issues=[issue],
        enabled_evals=["blacklist"],
    )


def plant_annotation(proj_dir, *, es_idx=4, content="[ladro]", edited=None):
    """A live note plus the reviewed result for it, as ``commit`` wrote it.

    ``edited`` rewrites the note's live content only, which is how a reader edit
    since the review looks on disk — the result still describes the old text.
    """
    key = f"chapter_01__{es_idx}__u1"
    (proj_dir / "annotations.jsonl").write_text(
        json.dumps({
            "project_id": "recbook", "chapter_id": "chapter_01", "es_idx": es_idx,
            "sub_id": "u1", "type": "word_choice", "content": edited or content,
            "timestamp": "2026-01-01T00:00:00",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    adir = proj_dir / ".harness" / "annotations"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "results.json").write_text(json.dumps({
        "project": "recbook",
        "committed_at": "2026-01-02T00:00:00",
        "results": [{
            "key": key,
            "chapter_id": "chapter_01",
            "es_idx": es_idx,
            "sub_id": "u1",
            "type": "word_choice",
            "content": content,
            "anchors": ["ladro"],
            "manual_reason": None,
            "es_sentence": "El perro ladro fuerte.",
            "state": "needs_help",
            "state_reason": "only the marked span is given, no judgement stated",
            "recommendation": "'ladro' is right here; no change needed.",
            "note_text": "Se mantiene 'ladro'.",
            "confidence": "high",
            "evidence": ["style guide: standard educated Mexican Spanish",
                         "checked the concordance for other uses"],
            "writable": True,
            "mode": "append",
            "new_content": content + "\n— IA: se mantiene.",
        }],
        "skipped": [],
    }, ensure_ascii=False), encoding="utf-8")
    return key


def items_of(client, project="recbook", chapter="chapter_01"):
    rv = client.get(f"/api/project/{project}/recommendations/{chapter}")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    return rv.get_json()["items"]


# --- What the model said, in full -------------------------------------------


def test_judge_message_is_not_truncated(client, book):
    plant_judge(book)

    (item,) = items_of(client)
    assert item["source"] == "judge"
    assert item["kind"] == "dialogue"
    assert item["severity"] == "error"
    # The whole point of the screen: the inbox caps a recommendation at 600
    # characters, and this one is longer than that on purpose.
    assert item["explanation"] == _LONG_MESSAGE
    assert len(item["explanation"]) > 600
    assert item["suggestion"] == "—El perro ladró fuerte."
    assert item["category"] == "STYLE_GUIDE"


def test_judge_finding_tints_the_whole_sentence(client, book):
    """A judge reports an excerpt, never offsets, so there is no span to mark."""
    plant_judge(book)

    (item,) = items_of(client)
    assert item["match_start"] is None
    assert item["match_end"] is None


def test_coded_finding_carries_its_in_sentence_span(client, book):
    plant_coded(book)

    (item,) = items_of(client)
    assert item["source"] == "coded"
    assert item["kind"] == "blacklist"
    sentence = item["context"]["current"]["es"]
    assert sentence[item["match_start"]:item["match_end"]] == "negro"


# --- Context, on a sparse index ---------------------------------------------


def test_context_neighbours_come_from_adjacent_rows_not_adjacent_indices(client, book):
    """The row after ``es_idx`` 1 is ``es_idx`` 4. ``es_idx + 1`` is a hole."""
    plant_judge(book, location="Tenia dos pisos.")

    (item,) = items_of(client)
    context = item["context"]
    # The excerpt sits inside the N:1 group, so it anchors to the group's row.
    assert context["current"]["es_idx"] == 1
    assert context["before"]["es_idx"] == 0
    assert context["before"]["es"] == "El gato negro dormia."
    # 2 and 3 were consumed by that group; the next real row is 4.
    assert context["after"]["es_idx"] == 4
    assert context["after"]["es"] == "El perro ladro fuerte."


def test_context_carries_the_english_source_line(client, book):
    plant_judge(book)

    (item,) = items_of(client)
    assert item["context"]["current"]["en"] == "The dog barked loudly."


def test_first_sentence_has_no_before(client, book):
    plant_judge(book, location="El gato negro dormia.")

    (item,) = items_of(client)
    assert item["context"]["before"] is None
    assert item["context"]["after"]["es_idx"] == 1


# --- Annotations ------------------------------------------------------------


def test_annotation_renders_the_reasoning_the_inbox_drops(client, book):
    key = plant_annotation(book)

    (item,) = items_of(client)
    assert item["source"] == "annotation"
    assert item["kind"] == "word_choice"
    assert item["key"] == key
    assert item["confidence"] == "high"
    assert item["explanation"] == "'ladro' is right here; no change needed."

    labels = [(d["label"], d["text"]) for d in item["detail"]]
    assert ("state_reason", "only the marked span is given, no judgement stated") in labels
    assert ("note_text", "Se mantiene 'ladro'.") in labels
    assert [text for label, text in labels if label == "evidence"] == [
        "style guide: standard educated Mexican Spanish",
        "checked the concordance for other uses",
    ]


def test_annotation_gets_context_and_an_anchor_highlight(client, book):
    plant_annotation(book)

    (item,) = items_of(client)
    context = item["context"]
    assert context["current"]["es"] == "El perro ladro fuerte."
    assert context["before"]["es_idx"] == 1
    assert context["after"]["es_idx"] == 5
    assert context["current"]["es"][item["match_start"]:item["match_end"]] == "ladro"


def test_annotation_edited_since_the_review_is_shown_and_flagged(client, book):
    """Shown, not hidden: a read-only screen reports the staleness."""
    plant_annotation(book, edited="[ladro] pero quiza 'aullo'")

    (item,) = items_of(client)
    assert item["stale"] is True


def test_annotation_whose_note_was_deleted_is_dropped(client, book):
    plant_annotation(book)
    (book / "annotations.jsonl").write_text("", encoding="utf-8")

    assert items_of(client) == []


# --- Ordering and the unanchored tail ---------------------------------------


def test_items_run_in_document_order_with_notes_after_findings(client, book):
    plant_coded(book)                                    # es_idx 0
    plant_judge(book, location="La casa era grande.")    # es_idx 1 (N:1 group)
    plant_annotation(book, es_idx=1, content="[casa]")   # es_idx 1 as well

    items = items_of(client)
    assert [(it["es_idx"], it["source"]) for it in items] == [
        (0, "coded"), (1, "judge"), (1, "annotation"),
    ]


def test_unplaceable_finding_lands_in_the_tail_with_no_context(client, book):
    plant_judge(book, location="Una frase que no esta en el texto.")

    (item,) = items_of(client)
    assert item["es_idx"] is None
    assert item["unanchored_reason"] == "unplaceable"
    assert item["context"] == {"before": None, "current": None, "after": None}
    # Lossless: the excerpt and the explanation still reach the page.
    assert item["excerpt"] == "Una frase que no esta en el texto."
    assert item["explanation"] == _LONG_MESSAGE


def test_unanchored_findings_sort_after_anchored_ones(client, book):
    plant_judge(book, location="El gato negro dormia.", judge="dialogue")
    plant_judge(book, location="No aparece en ninguna parte.", judge="address")

    items = items_of(client)
    assert [it["unanchored_reason"] for it in items] == [None, "unplaceable"]


# --- The page shell ---------------------------------------------------------


def test_page_lists_only_chapters_that_have_something_to_read(client, book):
    plant_judge(book)
    (book / "alignments" / "chapter_02.json").write_text(
        json.dumps({"chapter_id": "chapter_02", "alignments": []}), encoding="utf-8"
    )

    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert 'data-chapter="chapter_01"' in html
    assert 'data-chapter="chapter_02"' not in html


def test_page_offers_a_filter_chip_per_kind_present(client, book):
    plant_judge(book)
    plant_annotation(book)

    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert 'value="dialogue"' in html
    assert 'value="word_choice"' in html
    # Nothing blacklisted in this book, so no chip that filters nothing.
    assert 'value="blacklist"' not in html


def test_book_with_no_reviewed_annotations_renders(client, book):
    """7 of the 21 books here have never been reviewed. That is not an error."""
    plant_judge(book)
    assert not (book / ".harness" / "annotations" / "results.json").exists()

    rv = client.get("/recommendations/recbook")
    assert rv.status_code == 200
    assert 'data-chapter="chapter_01"' in rv.get_data(as_text=True)


def test_book_with_nothing_at_all_renders_the_empty_state(client, book):
    rv = client.get("/recommendations/recbook")
    assert rv.status_code == 200
    assert "empty-state" in rv.get_data(as_text=True)


# --- Whole-book route -------------------------------------------------------


def test_whole_book_route_returns_every_chapter(client, book):
    plant_judge(book)
    plant_annotation(book)

    body = client.get("/api/project/recbook/recommendations").get_json()
    assert body["ok"] is True
    assert [c["chapter_id"] for c in body["chapters"]] == ["chapter_01"]
    assert len(body["chapters"][0]["items"]) == 2
    assert body["shell"]["total"] == 2


# --- Guards -----------------------------------------------------------------


def test_bad_project_id_is_rejected(client, book):
    assert client.get("/recommendations/bad%20id").status_code == 400
    assert client.get("/api/project/bad%20id/recommendations").status_code == 400
    assert client.get(
        "/api/project/recbook/recommendations/bad%20chapter"
    ).status_code == 400


def test_missing_project_is_a_404(client, book):
    assert client.get("/recommendations/nosuchbook").status_code == 404
    assert client.get("/api/project/nosuchbook/recommendations").status_code == 404
    assert client.get(
        "/api/project/nosuchbook/recommendations/chapter_01"
    ).status_code == 404


def test_missing_chapter_is_a_404(client, book):
    assert client.get(
        "/api/project/recbook/recommendations/chapter_99"
    ).status_code == 404


# --- Navigation -------------------------------------------------------------


def test_dashboard_links_to_the_screen(client, book):
    html = client.get("/project/recbook").get_data(as_text=True)
    assert 'href="/recommendations/recbook"' in html


def test_book_page_links_to_the_screen(client, book):
    html = client.get("/read/recbook").get_data(as_text=True)
    assert 'href="/recommendations/recbook"' in html


# --- i18n -------------------------------------------------------------------


def test_spanish_renders_every_string(client, book):
    """`get_strings` falls back a whole table at a time, so a key present only
    in `en` renders as an empty string here rather than in English."""
    plant_judge(book)
    client.set_cookie("reader_lang", "es")

    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert '<html lang="es"' in html
    assert "Lo que dijeron los modelos" in html
    # Every string the JS writes is stamped into a data-* attribute; an empty
    # one is the exact shape of a missing Spanish key.
    import re
    assert [name for name, value in re.findall(r'data-([a-z-]+)="([^"]*)"', html)
            if value == ""] == []
