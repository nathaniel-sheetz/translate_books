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
import re

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
from web_ui.evaluations import (
    append_feedback,
    load_chapter_type_counts,
    merge_judge_result,
    save_chunk_evaluation,
)


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


def edit_chunk(proj_dir, old_text, new_text):
    """Rewrite a sentence in the chunk, as editing the prose in the reader does.

    The evaluators' verdicts were formed against the old text, which is what
    makes their quotes stop matching.
    """
    path = proj_dir / "chunks" / "chapter_01_chunk_000.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["translated_text"] = data["translated_text"].replace(old_text, new_text)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def realign(proj_dir, *, es_idx, es):
    """Replace the Spanish at one alignment row, as a realign can."""
    path = proj_dir / "alignments" / "chapter_01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["alignments"]:
        if row["es_idx"] == es_idx:
            row["es"] = es
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def mark_finding(proj_dir, eval_name, feedback_type, *, issue_index=0):
    """Mark a finding the way the reader's Review Mode does.

    Goes through :func:`append_feedback` rather than writing the JSONL by hand,
    so the record carries the ``issue_key`` a real mark carries and the test
    exercises the content-keyed lookup rather than the legacy positional one.
    """
    from web_ui.app import _resolve_issue_key

    append_feedback(
        proj_dir, "chapter_01_chunk_000",
        eval_name=eval_name,
        issue_index=issue_index,
        feedback_type=feedback_type,
        key=_resolve_issue_key(proj_dir, "chapter_01_chunk_000", eval_name, issue_index),
    )


def delete_note(proj_dir, key):
    """Delete the note the way the reader does: append a tombstone.

    ``annotations.jsonl`` is append-only, so a deletion is a ``removed`` record
    at the same key rather than a rewrite of the file.
    """
    chapter_id, es_idx, sub_id = key.split("__")
    with open(proj_dir / "annotations.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "project_id": "recbook", "chapter_id": chapter_id, "es_idx": int(es_idx),
            "sub_id": sub_id, "removed": True,
            "timestamp": "2026-03-04T10:00:00",
        }) + "\n")


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


def plant_two_judges(proj_dir, messages, *, judge="dialogue"):
    """Persist several judge issues in the given order, all on one sentence.

    The order is the point: ``issue_index`` is a position in this list, so
    re-planting the same findings the other way round is what an evaluator
    re-run does to a mark that trusted the position.
    """
    merge_judge_result(
        proj_dir, "chapter_01_chunk_000", judge,
        {
            "eval_name": judge,
            "eval_version": "1.0.0",
            "issues": [{
                "severity": "error",
                "message": message,
                "location": "El perro ladro fuerte.",
                "suggestion": "—El perro ladró fuerte.",
                "category": "STYLE_GUIDE",
            } for message in messages],
        },
    )


def favorite(client, fav_id, on=True, project="recbook", snapshot=None):
    """Toggle one item's heart the way the page does."""
    payload = {"id": fav_id, "favorite": on}
    if snapshot is not None:
        payload["snapshot"] = snapshot
    return client.post(
        f"/api/project/{project}/recommendations/favorite", json=payload,
    )


def favorite_records(proj_dir):
    """Every line of ``favorites.jsonl``, in the order it was appended."""
    path = proj_dir / "favorites.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fav_ids(client, **kwargs):
    """``{explanation: (fav_id, favorited?)}`` for one chapter's items."""
    return {
        item["explanation"]: (item["fav_id"], item["favorite"])
        for item in items_of(client, **kwargs)
    }


def items_of(client, project="recbook", chapter="chapter_01"):
    rv = client.get(f"/api/project/{project}/recommendations/{chapter}")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    return rv.get_json()["items"]


def patch_result(proj_dir, **fields):
    """Rewrite fields on the single reviewed result in ``results.json``.

    Nothing validates that file on the way back in - it is the reviewer's own
    output, hand-editable, and rows written by an older schema outlive the
    schema. So the shapes it can legally hold are wider than the ones the
    reviewer writes today, and the page has to survive them.
    """
    path = proj_dir / ".harness" / "annotations" / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["results"][0].update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def mark_finding_unlabelled(proj_dir, eval_name):
    """A feedback record with no ``feedback_type``, written by hand.

    :func:`append_feedback` refuses one - the label is validated against
    ``_ALLOWED_FEEDBACK_TYPES`` - but the file is append-only and older records
    are not re-validated on read, so this is a shape both readers of that file
    have to agree about.
    """
    from web_ui.app import _resolve_issue_key
    from web_ui.evaluations import _feedback_file

    path = _feedback_file(proj_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": "2026-03-04T10:00:00",
            "chunk_id": "chapter_01_chunk_000",
            "eval_name": eval_name,
            "issue_index": 0,
            "issue_key": _resolve_issue_key(
                proj_dir, "chapter_01_chunk_000", eval_name, 0
            ),
        }) + "\n")


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


def test_annotation_whose_note_was_deleted_is_kept_and_dated(client, book):
    """Deleting the note is what *finishing* looks like, not what losing it does.

    This is the case that made the screen misleading: it kept the reviews you
    ignored and dropped the ones you acted on. 56 of ``fabre2``'s 79 reviewed
    annotations were invisible here for exactly this reason.
    """
    key = plant_annotation(book)
    delete_note(book, key)

    (item,) = items_of(client)
    assert item["status"] == "deleted"
    assert item["status_at"] == "2026-03-04T10:00:00"
    # Nothing is lost with the note: results.json holds the whole review.
    assert item["excerpt"] == "[ladro]"
    assert item["explanation"] == "'ladro' is right here; no change needed."
    assert [d["label"] for d in item["detail"]] == [
        "state_reason", "note_text", "evidence", "evidence",
    ]


def test_deleted_note_still_shows_the_sentence_when_the_prose_is_unchanged(client, book):
    """Deleting a note says nothing about the prose, so live context stands."""
    key = plant_annotation(book)
    delete_note(book, key)

    (item,) = items_of(client)
    assert item["context"]["current"]["es"] == "El perro ladro fuerte."
    assert item["original_text"] == ""


# --- What became of an item -------------------------------------------------


def test_marked_finding_is_shown_with_what_became_of_it(client, book):
    """The reader hides a finding you have ruled on. This screen reports it."""
    plant_judge(book)
    mark_finding(book, "dialogue", "resolved")

    (item,) = items_of(client)
    assert item["status"] == "fixed"
    assert item["status_at"]
    assert item["explanation"] == _LONG_MESSAGE


def test_a_false_positive_keeps_its_sentence(client, book):
    """Calling a finding wrong changes nothing about the prose it quoted."""
    plant_coded(book)
    mark_finding(book, "blacklist", "false_positive")

    (item,) = items_of(client)
    assert item["status"] == "not_a_problem"
    assert item["context"]["current"]["es"] == "El gato negro dormia."


def test_review_mode_still_hides_marked_findings(client, book):
    """The regression that matters. Review Mode is a working surface: a finding
    you have ruled on must not come back as a tint."""
    plant_judge(book)
    mark_finding(book, "dialogue", "resolved")

    review = client.get("/api/project/recbook/review/chapter_01").get_json()
    assert review["by_es_idx"] == {}
    assert review["unanchored"] == []


def test_chapter_chips_still_count_outstanding_work_only(client, book):
    """`load_chapter_type_counts` is read by the chapter list and the Review tab,
    where a count means work to do. Only the recommendations shell asks for the
    split, and the default shape must not move."""
    plant_judge(book)
    mark_finding(book, "dialogue", "resolved")

    assert load_chapter_type_counts(book).get("chapter_01", {}).get("dialogue", 0) == 0
    split = load_chapter_type_counts(book, statuses=True)["chapter_01"]
    assert split["open"]["dialogue"] == 0
    assert split["history"]["dialogue"] == 1
    assert split["by_status"] == {"fixed": 1}


def test_shell_counts_history_apart_from_outstanding(client, book):
    plant_coded(book)                       # left alone
    plant_judge(book)                       # fixed
    mark_finding(book, "dialogue", "resolved")

    shell = client.get("/api/project/recbook/recommendations").get_json()["shell"]
    (chapter,) = shell["chapters"]
    assert chapter["counts"] == {"blacklist": 1}
    assert chapter["history"] == 1
    assert shell["total"] == 1
    assert shell["history_total"] == 1
    assert shell["status_totals"] == {"open": 1, "fixed": 1}
    # The kind filter has to know about a kind that exists only in history, or
    # unticking it would leave those cards on screen.
    assert shell["totals"] == {"blacklist": 1, "dialogue": 1}


def test_the_judge_being_wrong_is_the_one_thing_not_shown_by_default(client, book):
    """831 of the corpus's 1,132 marks say the judge misread the prose. They are
    on the page, with their counts, behind a box you have to tick."""
    plant_coded(book)                       # left alone, so an `open` box exists
    plant_judge(book)
    mark_finding(book, "dialogue", "false_positive")

    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert re.search(r'value="not_a_problem"\s*\n?\s*>', html)      # unticked
    assert re.search(r'value="open"\s*\n?\s*checked>', html)        # ticked


# --- Text that has moved on -------------------------------------------------


def test_a_finding_you_fixed_shows_the_text_as_it_read_then(client, book):
    """The excerpt is the snapshot: it *was* the prose, and the prose moved on."""
    plant_judge(book)
    mark_finding(book, "dialogue", "resolved")
    edit_chunk(book, "El perro ladro fuerte.", "—El perro ladró fuerte.")

    (item,) = items_of(client)
    assert item["es_idx"] is None                     # nothing left to anchor to
    assert item["unanchored_reason"] == "obsolete"
    assert item["original_text"] == "El perro ladro fuerte."
    assert item["status"] == "fixed"


def test_an_unplaceable_quote_is_not_dressed_up_as_former_prose(client, book):
    """`unplaceable` means the quote was never in the book. Calling it "as it
    read then" would invent a history that never happened."""
    plant_judge(book, location="Una frase que no esta en el texto.")

    (item,) = items_of(client)
    assert item["unanchored_reason"] == "unplaceable"
    assert item["original_text"] == ""


def test_annotation_whose_sentence_drifted_shows_the_stored_one(client, book):
    """``es_idx`` is a position, and a realign renumbers positions. Unless the
    live row still matches what the review read, the card shows the sentence the
    review read - never the stranger sitting at that index now."""
    plant_annotation(book)
    realign(book, es_idx=4, es="Un caballo relincho en el patio.")

    (item,) = items_of(client)
    assert item["original_text"] == "El perro ladro fuerte."
    assert item["context"] == {"before": None, "current": None, "after": None}
    assert item["match_start"] is None
    assert "Un caballo relincho" not in json.dumps(item, ensure_ascii=False)


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


# --- Favorites --------------------------------------------------------------
#
# The one thing this page writes. Every other mark on a recommendation is a
# verdict and means you are finished with it; a favorite means you are not.


def test_favoriting_a_finding_sticks(client, book):
    plant_judge(book)
    (fav_id, before), = fav_ids(client).values()
    assert before is False

    assert favorite(client, fav_id).get_json() == {"ok": True, "favorite": True}
    (_, after), = fav_ids(client).values()
    assert after is True


def test_unfavoriting_appends_rather_than_rewriting(client, book):
    """The file is append-only, like every other record store in this app."""
    plant_judge(book)
    (fav_id, _), = fav_ids(client).values()

    favorite(client, fav_id, True)
    favorite(client, fav_id, False)

    (_, standing), = fav_ids(client).values()
    assert standing is False
    lines = (book / "favorites.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["favorite"] for line in lines] == [True, False]


def test_a_favorite_follows_the_finding_not_its_slot(client, book):
    """The whole reason favorites key on ``issue_key``.

    ``issue_index`` is a position in the evaluator's issue list, rewritten
    wholesale on every run. A favorite keyed on it would stay pointing at slot 1
    and silently re-aim at whatever finding now sits there — the bug that had
    already mis-aimed 87 feedback marks in the local corpus.
    """
    plant_two_judges(book, ["The first defect.", "The second defect."])
    favorite(client, fav_ids(client)["The second defect."][0])
    assert fav_ids(client)["The second defect."][1] is True

    # The judge runs again and reports the same two defects the other way round.
    plant_two_judges(book, ["The second defect.", "The first defect."])

    marks = fav_ids(client)
    assert marks["The second defect."][1] is True, "the favorite lost its finding"
    assert marks["The first defect."][1] is False, "the favorite hit the wrong one"


def test_an_annotation_can_be_favorited(client, book):
    """Notes have carried a stable key all along, so there is nothing to derive."""
    key = plant_annotation(book)
    (item,) = [it for it in items_of(client) if it["source"] == "annotation"]
    assert item["fav_id"] == f"annotation:{key}"

    favorite(client, item["fav_id"])
    (item,) = [it for it in items_of(client) if it["source"] == "annotation"]
    assert item["favorite"] is True


def test_a_favorite_is_not_a_verdict(client, book):
    """It says come back to this, so it must not read as having dealt with it."""
    plant_judge(book)
    (fav_id, _), = fav_ids(client).values()
    favorite(client, fav_id)

    (item,) = items_of(client)
    assert item["status"] == "open"


def test_the_shell_says_which_chapters_hold_favorites(client, book):
    """Chapters fill lazily, so "favorites only" needs this to know what to
    fetch — without it, a favorite in a chapter you never scrolled to is simply
    not in the DOM and the filter shows an empty page."""
    plant_judge(book)
    assert 'data-favorites="0"' in client.get(
        "/recommendations/recbook").get_data(as_text=True)

    (fav_id, _), = fav_ids(client).values()
    favorite(client, fav_id)

    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert 'data-favorites="1"' in html


def test_the_page_offers_a_favorites_filter(client, book):
    plant_judge(book)
    html = client.get("/recommendations/recbook").get_data(as_text=True)
    # Not `rec-hide-cb`: the other two axes hide what you untick and this one
    # keeps only what you tick, so the shared handler must skip it.
    assert 'class="rec-fav-cb"' in html
    assert "Favorites only" in html


def test_a_malformed_favorite_id_is_rejected(client, book):
    """The id is composed server-side and echoed back, so it is checked on the
    way in rather than trusted with a path-shaped string."""
    for bad in ["finding:../../etc:ab12", "annotation:../secrets",
                "nonsense", "", None, {"id": "no"}]:
        rv = client.post("/api/project/recbook/recommendations/favorite",
                         json={"id": bad, "favorite": True})
        assert rv.status_code == 400, bad
    assert not (book / "favorites.jsonl").exists()


def test_the_favorite_flag_must_say_which_way(client, book):
    """A toggle with a missing field must not quietly read as unfavorite."""
    plant_judge(book)
    (fav_id, _), = fav_ids(client).values()
    for bad in [{}, {"favorite": "yes"}, {"favorite": 1}, {"favorite": None}]:
        rv = client.post("/api/project/recbook/recommendations/favorite",
                         json=dict({"id": fav_id}, **bad))
        assert rv.status_code == 400, bad


def test_favoriting_guards_the_project(client, book):
    assert favorite(client, "annotation:chapter_01__4__u1",
                    project="bad%20id").status_code == 400
    assert favorite(client, "annotation:chapter_01__4__u1",
                    project="nosuchbook").status_code == 404


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
    assert [name for name, value in re.findall(r'data-([a-z-]+)="([^"]*)"', html)
            if value == ""] == []


# --- Shapes the stored data can legally hold --------------------------------


def test_a_non_integer_es_idx_does_not_cost_the_chapter_its_cards(client, book):
    """One bad row must not take the chapter down with it.

    Findings reach the page through an ``int(key)`` guard; a note's index comes
    straight off ``results.json``, which nothing polices. A string index sorted
    against the ints beside it raised ``TypeError`` inside the route, so a
    single legacy row cost you every card in its chapter - the judge findings
    included, which had nothing to do with it.
    """
    plant_judge(book)
    plant_annotation(book)
    patch_result(book, es_idx="4")

    items = items_of(client)
    assert [item["source"] for item in items] == ["judge", "annotation"]
    # Coerced, not merely survived: the card still finds its own sentence.
    assert items[1]["es_idx"] == 4
    assert items[1]["context"]["current"]["es"] == "El perro ladro fuerte."


def test_an_unparseable_es_idx_renders_as_no_index_at_all(client, book):
    """Not a number is treated as no position, which the card already handles:
    it shows the sentence the review stored and no live context, rather than
    attaching itself to whichever row happens to sit at a guessed index."""
    plant_annotation(book)
    patch_result(book, es_idx="chapter_01")

    (note,) = items_of(client)
    assert note["es_idx"] is None
    assert note["context"]["current"] is None
    assert note["original_text"] == "El perro ladro fuerte."


def test_a_note_of_an_unknown_type_folds_into_flag(client, book):
    """The filter row is built from a fixed tuple of kinds and the CSS carries
    one hide-rule per entry, so a type from outside it would render a card no
    checkbox governs and a total no chip accounts for. ``flag`` is where an
    untyped note already lands, so an unrecognised one lands with it."""
    plant_annotation(book)
    patch_result(book, type="marginalia")

    (note,) = items_of(client)
    assert note["kind"] == "flag"

    shell = client.get("/api/project/recbook/recommendations").get_json()["shell"]
    assert "marginalia" not in shell["totals"]
    assert set(shell["totals"]) <= set(shell["kinds"]), "a total with no checkbox"
    assert shell["total"] == 1


def test_an_unlabelled_mark_reads_open_on_both_sides(client, book):
    """The count and the card must not disagree about what a record means.

    The chapter counter defaulted an unrecognised ``feedback_type`` to
    ``settled`` and the card defaulted it to ``open``, so the page offered a
    "Settled 1" checkbox that governed nothing while its only card was open.
    An unlabelled record is not a decision, so both read it as outstanding.
    """
    plant_judge(book)
    mark_finding_unlabelled(book, "dialogue")

    (item,) = items_of(client)
    assert item["status"] == "open"

    shell = client.get("/api/project/recbook/recommendations").get_json()["shell"]
    assert shell["status_totals"].get("settled") is None
    assert shell["status_totals"]["open"] == 1


# --- The favorite id --------------------------------------------------------


def test_the_id_admits_the_same_alphabet_as_the_rest_of_the_app():
    """``_safe_id`` allows a period - real project dirs use them - so an id
    holding one has to be savable here too, or the heart 400s with nothing the
    reader could do about it. Nothing composed here is ever joined to a path,
    so the dot buys a caller no reach."""
    from web_ui import favorites as favorites_store

    assert favorites_store.is_valid_id("finding:chapter.01_chunk_000:a1b2")
    assert favorites_store.is_valid_id("annotation:chapter.01__4__u1")


@pytest.mark.parametrize("bad", [
    "finding:chapter_01_chunk_000:a:b",   # `:` is the scheme own separator
    "finding:../../etc/passwd:a1b2",      # no path segments
    "annotation:chapter_01/../x",
    "finding:" + "a" * 121 + ":a1b2",     # past the chunk-half cap
    "verdict:chapter_01_chunk_000:a1b2",  # not a namespace this module composes
    "",
])
def test_the_id_still_rejects_what_it_always_did(bad):
    from web_ui import favorites as favorites_store

    assert not favorites_store.is_valid_id(bad)


def test_a_chunk_id_with_no_chunk_marker_costs_a_count_and_not_a_card(book):
    """``chapter_of`` is used for counting only, never for matching.

    ``chapter_id_from_chunk_id`` returns a marker-less id unchanged, so the
    chapter derived here is that id itself - a key no real chapter carries, and
    one the shell therefore never reads. The favorite still stands, because an
    item heart is decided by the id and not by this guess.
    """
    from web_ui import favorites as favorites_store

    fav_id = "finding:oddball:a1b2"
    favorites_store.append_favorite(book, fav_id, True)

    assert favorites_store.chapter_of(fav_id) == "oddball"
    assert favorites_store.favorites_by_chapter(book) == {"oddball": 1}
    assert fav_id in favorites_store.load_favorites(book)


def test_the_shell_can_count_a_favorite_whose_finding_is_gone(client, book):
    """The known limit of keying on a content hash, pinned rather than fixed.

    ``issue_key`` covers the message, which a judge rewords when it re-runs, so
    the favorite id no longer names anything. The record is still standing -
    nothing prunes an append-only file - so the shell count says 1 while the
    chapter renders no heart. The page corrects this after the chapter fills, by
    counting the hearts it actually rendered; this pins the server half, which
    is what that correction is correcting.
    """
    plant_judge(book, message="The first defect." + "x" * 700)
    (fav_id, _), = fav_ids(client).values()
    favorite(client, fav_id)

    # The judge runs again and words the same complaint differently.
    plant_judge(book, message="A quite differently worded defect." + "x" * 700)

    assert all(not item["favorite"] for item in items_of(client))
    html = client.get("/recommendations/recbook").get_data(as_text=True)
    assert 'data-favorites="1"' in html


# --- Whole-book route, continued --------------------------------------------


def test_one_unreadable_chapter_does_not_cost_the_book(client, book):
    """A broken alignment file is reported against its own chapter and the rest
    of the book still comes back - the promise the route docstring makes."""
    plant_judge(book)
    (book / "alignments" / "chapter_01.json").write_text("{ not json", encoding="utf-8")

    body = client.get("/api/project/recbook/recommendations").get_json()
    assert body["ok"] is True
    (chapter,) = body["chapters"]
    assert chapter["chapter_id"] == "chapter_01"
    assert chapter.get("error")
    assert "items" not in chapter


# --- The reader's own heart -------------------------------------------------
#
# The store was built for this: its docstring says favorites live at the project
# root rather than under `evaluations/` "because a favorite can be on a reader
# *annotation*… Being in the reader-sidecar family is also what the reader needs
# to write one itself." These pin the second half of that sentence.
#
# The reader composes no id. Both of its payloads carry the one the server made,
# which is the same one this screen composes for the same item — the alternative
# was a second identity scheme, and the two would disagree the first time a
# realign moved an es_idx.


def reader_notes(client, project="recbook", chapter="chapter_01"):
    rv = client.get(f"/api/annotations/{project}/{chapter}")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    return rv.get_json()["annotations"]


def reader_findings(client, project="recbook", chapter="chapter_01"):
    rv = client.get(f"/api/project/{project}/review/{chapter}")
    assert rv.status_code == 200, rv.get_data(as_text=True)
    body = rv.get_json()
    return [f for group in body["by_es_idx"].values() for f in group] + body["unanchored"]


def plant_bare_note(proj_dir, *, es_idx=4, sub_id="u1", content="[ladro]"):
    """A live note with no reviewed result — 22 of the corpus's 253 look like this."""
    with open(proj_dir / "annotations.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "project_id": "recbook", "chapter_id": "chapter_01", "es_idx": es_idx,
            "sub_id": sub_id, "type": "word_choice", "content": content,
            "timestamp": "2026-01-01T00:00:00",
        }, ensure_ascii=False) + "\n")
    return f"chapter_01__{es_idx}__{sub_id}"


def test_the_reader_gets_a_heart_for_each_note(client, book):
    plant_annotation(book)
    (note,) = reader_notes(client)

    assert note["fav_id"] == "annotation:chapter_01__4__u1"
    assert note["favorite"] is False

    favorite(client, note["fav_id"])
    assert reader_notes(client)[0]["favorite"] is True


def test_the_reader_gets_a_heart_for_each_finding(client, book):
    plant_judge(book)
    (finding,) = reader_findings(client)

    assert finding["fav_id"] == f"finding:chapter_01_chunk_000:{finding['issue_key']}"
    assert finding["favorite"] is False

    favorite(client, finding["fav_id"])
    assert reader_findings(client)[0]["favorite"] is True


def test_the_reader_and_this_screen_name_a_note_identically(client, book):
    """The pin that keeps one heart from becoming two.

    A note hearted in the sheet has to be the note this screen shows hearted,
    and the only way to guarantee that is for neither surface to compose the id.
    """
    plant_annotation(book)
    (note,) = reader_notes(client)
    (fav_id, _), = fav_ids(client).values()

    assert note["fav_id"] == fav_id

    favorite(client, note["fav_id"])
    assert all(favorited for _, favorited in fav_ids(client).values())


def test_the_reader_and_this_screen_name_a_finding_identically(client, book):
    plant_judge(book)
    (finding,) = reader_findings(client)
    (fav_id, _), = fav_ids(client).values()

    assert finding["fav_id"] == fav_id


def test_a_note_the_reviewer_never_saw_can_still_be_hearted(client, book):
    """This screen shows a note only once annotation-review has written a result
    for it, because that is where its text is kept. The reader has the note in
    front of it either way, so the heart cannot wait on a nightly pass."""
    plant_bare_note(book)

    (note,) = reader_notes(client)
    assert note["fav_id"] == "annotation:chapter_01__4__u1"
    assert favorite(client, note["fav_id"]).status_code == 200
    assert reader_notes(client)[0]["favorite"] is True

    assert items_of(client) == [], "the reviewer never saw it, so there is no card"


def test_a_legacy_note_gets_the_key_the_rest_of_the_app_uses(client, book):
    """Rows written before sub_ids round-trip as the sentinel, here too."""
    with open(book / "annotations.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "project_id": "recbook", "chapter_id": "chapter_01", "es_idx": 4,
            "type": "flag", "content": "old note", "timestamp": "2025-01-01T00:00:00",
        }) + "\n")

    (note,) = reader_notes(client)
    assert note["sub_id"] == "legacy"
    assert note["fav_id"] == "annotation:chapter_01__4__legacy"


def test_a_note_just_written_can_be_hearted_without_a_reload(client, book):
    """The save names the note, so the card it re-renders can carry a heart.

    Without this the one note you cannot favorite is the one you just wrote.
    """
    rv = client.post("/api/annotation", json={
        "project_id": "recbook", "chapter_id": "chapter_01", "es_idx": 4,
        "type": "word_choice", "content": "nueva nota",
    })
    body = rv.get_json()
    assert body["saved"] is True
    assert body["fav_id"] == f"annotation:chapter_01__4__{body['sub_id']}"
    assert favorite(client, body["fav_id"]).status_code == 200


# --- The snapshot -----------------------------------------------------------
#
# A mark is a pointer, and the reader points at things it is in the middle of
# getting rid of. The text travels with it so the mark is still legible when the
# note has been deleted, or when the next judge run rewords a finding into a
# different issue_key and the standing mark names nothing live.


_NOTE_SNAPSHOT = {
    "kind": "annotation",
    "chapter_id": "chapter_01",
    "type": "word_choice",
    "text": "[ladro] reads oddly here",
    "es_text": "El perro ladro fuerte.",
}


def test_a_snapshot_is_kept_with_the_mark(client, book):
    plant_annotation(book)
    (note,) = reader_notes(client)

    favorite(client, note["fav_id"], snapshot=_NOTE_SNAPSHOT)

    (record,) = favorite_records(book)
    assert record["snapshot"] == _NOTE_SNAPSHOT


def test_the_snapshot_outlives_the_note_it_describes(client, book):
    """The point of storing it at all."""
    key = plant_bare_note(book)
    favorite(client, f"annotation:{key}", snapshot=_NOTE_SNAPSHOT)

    delete_note(book, key)

    assert reader_notes(client) == []
    (record,) = favorite_records(book)
    assert record["snapshot"]["text"] == "[ladro] reads oddly here"


def test_unfavoriting_carries_no_snapshot(client, book):
    """An unfavorite says you are done with the item, so re-storing the text
    there would leave the file arguing with itself about the standing record."""
    plant_annotation(book)
    (note,) = reader_notes(client)

    favorite(client, note["fav_id"], True, snapshot=_NOTE_SNAPSHOT)
    favorite(client, note["fav_id"], False, snapshot=_NOTE_SNAPSHOT)

    kept, dropped = favorite_records(book)
    assert "snapshot" in kept
    assert "snapshot" not in dropped


def test_a_snapshot_keeps_only_what_the_scheme_knows(client, book):
    plant_annotation(book)
    (note,) = reader_notes(client)

    favorite(client, note["fav_id"], snapshot={
        "kind": "annotation",
        "text": "kept",
        "recommendation": "not a field of this kind",
        "es_text": {"not": "a string"},
        "chapter_id": None,
    })

    (record,) = favorite_records(book)
    assert record["snapshot"] == {"kind": "annotation", "text": "kept"}


def test_an_unknown_kind_stores_no_snapshot(client, book):
    plant_annotation(book)
    (note,) = reader_notes(client)

    favorite(client, note["fav_id"], snapshot={"kind": "chunk", "text": "x"})

    (record,) = favorite_records(book)
    assert record["favorite"] is True
    assert "snapshot" not in record


def test_a_runaway_snapshot_field_is_capped(client, book):
    plant_annotation(book)
    (note,) = reader_notes(client)

    favorite(client, note["fav_id"],
             snapshot={"kind": "annotation", "text": "x" * 9000})

    (record,) = favorite_records(book)
    assert len(record["snapshot"]["text"]) == 2000


def test_a_malformed_snapshot_still_records_the_mark(client, book):
    """The mark is the point and the text is a convenience: a heart must not
    fail over the prose it was carrying, the way the id alphabet must not."""
    plant_annotation(book)
    (note,) = reader_notes(client)

    for bad in ("a string", 7, ["a", "list"], {"no": "kind"}):
        book.joinpath("favorites.jsonl").unlink(missing_ok=True)
        assert favorite(client, note["fav_id"], snapshot=bad).get_json() == {
            "ok": True, "favorite": True,
        }
        (record,) = favorite_records(book)
        assert "snapshot" not in record


def test_a_finding_snapshot_keeps_the_judges_own_words(client, book):
    """The case the mark cannot survive on its own: `issue_key` hashes the
    message, so a judge that rewords itself leaves the heart naming nothing."""
    plant_judge(book, message="The first defect." + "x" * 700)
    (finding,) = reader_findings(client)

    favorite(client, finding["fav_id"], snapshot={
        "kind": "finding",
        "chapter_id": "chapter_01",
        "eval_name": "dialogue",
        "category": "STYLE_GUIDE",
        "severity": "error",
        "message": finding["message"],
        "suggestion": finding["suggestion"],
        "excerpt": finding["excerpt"],
    })

    plant_judge(book, message="A quite differently worded defect." + "x" * 700)

    assert all(not f["favorite"] for f in reader_findings(client))
    (record,) = favorite_records(book)
    assert record["snapshot"]["message"].startswith("The first defect.")
    assert record["snapshot"]["eval_name"] == "dialogue"
