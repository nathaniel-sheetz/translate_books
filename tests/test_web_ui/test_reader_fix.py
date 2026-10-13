"""Applying a judge finding from the reader.

Three server pieces: the review payload says whether a finding is a one-sentence
swap (``fix``), the correction row remembers which finding a Save came from
(``from_finding``), and the ``resolved`` mark the Save writes records how the
suggestion was used. The mark stays the human label, so every precision script
scores it exactly as it scores a hand-clicked Resolved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import web_ui.app as app_module
from web_ui.app import app
from web_ui.evaluations import append_feedback, merge_judge_result

CHUNK_ID = "chapter_01_chunk_000"
# Two sentences in one chunk, each its own alignment row.
_TRANSLATED = "El gato negro. El perro grande."
_REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "fixproj"
    (proj_dir / "alignments").mkdir(parents=True)
    (proj_dir / "chunks").mkdir(parents=True)
    _write_alignment(proj_dir, "El gato negro.", "El perro grande.")
    (proj_dir / "chunks" / f"{CHUNK_ID}.json").write_text(
        json.dumps({"id": CHUNK_ID, "translated_text": _TRANSLATED}, ensure_ascii=False),
        encoding="utf-8",
    )
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_alignment(proj_dir, first, second):
    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "fixproj",
        "high_confidence_pct": 100.0,
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "es": first, "en": "The black cat.",
             "confidence": "high", "chunk_id": CHUNK_ID},
            {"es_idx": 1, "en_idx": 1, "es": second, "en": "The big dog.",
             "confidence": "high", "chunk_id": CHUNK_ID},
        ],
    }
    (proj_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
    )


def _judge(proj_dir, location, suggestion, judge="editorial"):
    merge_judge_result(
        proj_dir, CHUNK_ID, judge,
        {
            "eval_name": judge,
            "eval_version": "1.0.0",
            "issues": [{
                "severity": "warning",
                "message": "[word-choice] too plain",
                "location": location,
                "suggestion": suggestion,
            }],
        },
    )


def _review(client):
    return client.get("/api/project/fixproj/review/chapter_01").get_json()


def _only(body, es_idx):
    findings = body["by_es_idx"][str(es_idx)]
    assert len(findings) == 1
    return findings[0]


# ── the dry-run verdict on the review payload ────────────────────────────────


def test_a_one_sentence_swap_is_applicable(client, project):
    _judge(project, "perro grande", "perro enorme")

    f = _only(_review(client), 1)
    assert f["fix"] == {"old": "perro grande", "new": "perro enorme"}
    assert f["fix_reason"] is None


def test_a_swap_across_two_sentences_is_left_to_edit(client, project):
    _judge(project, "negro. El perro", "negro. El can")

    f = _only(_review(client), 0)
    assert f["fix"] is None
    assert f["fix_reason"] == "spans_sentences"


def test_a_sentence_with_a_queued_save_is_left_to_edit(client, project):
    """The chunk still holds the excerpt; the sentence on screen no longer does."""
    _judge(project, "perro grande", "perro enorme")
    _write_alignment(project, "El gato negro.", "El can grande.")

    f = _only(_review(client), 1)
    assert f["fix"] is None
    assert f["fix_reason"] == "sentence_edited"


def test_a_sentence_that_already_holds_the_suggestion_is_left_to_edit(client, project):
    """An insertion keeps its excerpt, so the excerpt still stands once after it."""
    _judge(project, "perro grande", "perro grande y fuerte")
    _write_alignment(project, "El gato negro.", "El perro grande y fuerte.")

    f = _only(_review(client), 1)
    assert f["fix"] is None
    assert f["fix_reason"] == "sentence_edited"


def test_a_sentence_that_gained_the_suggestions_ending_is_left_to_edit(client, project):
    """The swap would read "El can grande y fuerte y fuerte."."""
    _judge(project, "perro grande", "can grande y fuerte")
    _write_alignment(project, "El gato negro.", "El perro grande y fuerte.")

    f = _only(_review(client), 1)
    assert f["fix"] is None
    assert f["fix_reason"] == "sentence_edited"


def test_a_swap_across_a_grouped_rows_line_break_is_not_called_an_edit():
    """The row reads with a space where the chunk breaks the line; nothing changed."""
    chunk = "El gato negro.\nEl perro grande."
    row = {
        "es": "El gato negro. El perro grande.",
        "chunk_offset_start": 0,
        "chunk_offset_end": len(chunk),
    }
    issue = {
        "severity": "warning",
        "message": "[word-choice] too plain",
        "location": "negro.\nEl perro",
        "suggestion": "negro.\nEl can",
    }

    assert app_module._reader_fix(issue, chunk, row) == (None, "spans_sentences")


def test_an_excerpt_the_sentence_now_holds_twice_is_left_to_edit(client, project):
    """Once in the chunk, twice on screen: there is no telling which to swap."""
    _judge(project, "perro grande", "perro enorme")
    _write_alignment(project, "El gato negro.", "El perro grande, perro grande.")

    f = _only(_review(client), 1)
    assert f["fix"] is None
    assert f["fix_reason"] == "sentence_edited"


def test_an_edit_elsewhere_in_the_sentence_still_leaves_the_swap(client, project):
    """A queued save that did not touch the excerpt costs the finding nothing."""
    _judge(project, "perro grande", "perro enorme")
    _write_alignment(project, "El gato negro.", "El perro grande ladra.")

    f = _only(_review(client), 1)
    assert f["fix"] == {"old": "perro grande", "new": "perro enorme"}
    assert f["fix_reason"] is None


def test_a_swap_of_the_whole_sentence_is_applicable(client, project):
    """The span may run to both ends of the row; only past them is too far."""
    _judge(project, "El perro grande.", "El perro enorme.")

    f = _only(_review(client), 1)
    assert f["fix"] == {"old": "El perro grande.", "new": "El perro enorme."}


@pytest.mark.parametrize("location, suggestion, es_idx, reason", [
    ("perro grande", "use a stronger adjective", 1, "suggestion_not_literal"),
    ("El", "Un", 0, "excerpt_ambiguous"),
    ("perro", "perro grande", 1, "suggestion_restates_context"),
])
def test_the_classifiers_own_reason_passes_through(
    client, project, location, suggestion, es_idx, reason,
):
    _judge(project, location, suggestion)

    f = _only(_review(client), es_idx)
    assert f["fix"] is None
    assert f["fix_reason"] == reason


def test_a_binned_finding_carries_no_fix(client, project):
    _judge(project, "El gato ... negro.", "El gato pardo.")

    body = _review(client)
    assert body["by_es_idx"] == {}
    assert "fix" not in body["unanchored"][0]


def test_the_review_agrees_with_the_dry_run(client, project):
    """One classifier, two callers: what the reader offers is what apply plans."""
    from src.judges.fixes import ProposedFix, classify_fix

    _judge(project, "perro grande", "perro enorme")
    issue = json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["judges"]["editorial"]["issues"][0]
    planned = classify_fix(issue, _TRANSLATED)

    assert isinstance(planned, ProposedFix)
    assert _only(_review(client), 1)["fix"] == {
        "old": planned.excerpt, "new": planned.suggestion,
    }


# ── the correction row ───────────────────────────────────────────────────────


def _save(client, **extra):
    return client.post("/api/correction", json={
        "project_id": "fixproj",
        "chapter_id": "chapter_01",
        "es_idx": 1,
        "original_es": "El perro grande.",
        "corrected_es": "El perro enorme.",
        "en_reference": "The big dog.",
        **extra,
    })


def _last_correction(proj_dir):
    lines = (proj_dir / "corrections.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_a_save_from_a_finding_remembers_the_finding(client, project):
    rv = _save(client, from_finding={
        "eval_name": "editorial", "chunk_id": CHUNK_ID, "issue_key": "abc123",
        "category": "NATURALNESS", "suggestion": "perro enorme",
        "mode": "apply", "suggestion_used": "verbatim",
    })
    assert rv.get_json()["saved"] is True

    record = _last_correction(project)
    assert record["from_finding"] == {
        "eval_name": "editorial", "chunk_id": CHUNK_ID, "issue_key": "abc123",
        "category": "NATURALNESS", "suggestion": "perro enorme",
        "mode": "apply", "suggestion_used": "verbatim",
    }
    # `source: "judge:..."` means run_judges.py spliced it in unread; the splice
    # guards are calibrated on those rows, and a previewed, editable Save is not
    # one of them.
    assert "source" not in record


def test_a_plain_save_carries_no_finding(client, project):
    _save(client)
    assert "from_finding" not in _last_correction(project)


@pytest.mark.parametrize("junk", [
    "editorial", ["editorial"], {}, {"mode": "apply"}, {"eval_name": 7},
])
def test_a_malformed_finding_never_costs_the_save(client, project, junk):
    rv = _save(client, from_finding=junk)
    assert rv.get_json()["saved"] is True
    assert "from_finding" not in _last_correction(project)


def test_unknown_values_are_dropped_from_the_finding(client, project):
    _save(client, from_finding={
        "eval_name": "editorial", "mode": "bulk", "suggestion_used": "sort of",
        "note": "not a field",
    })
    assert _last_correction(project)["from_finding"] == {"eval_name": "editorial"}


def test_values_of_the_wrong_type_are_dropped_from_the_finding(client, project):
    """A list cannot be looked up in a set, and the Save must not learn that
    the hard way."""
    rv = _save(client, from_finding={
        "eval_name": "editorial", "mode": ["apply"],
        "suggestion_used": {"how": "verbatim"},
    })
    assert rv.get_json()["saved"] is True
    assert _last_correction(project)["from_finding"] == {"eval_name": "editorial"}


def test_a_long_suggestion_is_cut_short(client, project):
    _save(client, from_finding={"eval_name": "editorial", "suggestion": "a" * 5000})
    assert len(_last_correction(project)["from_finding"]["suggestion"]) == 2000


# ── the mark ─────────────────────────────────────────────────────────────────


def _mark(client, **payload):
    return client.post(
        f"/api/project/fixproj/evaluations/{CHUNK_ID}/feedback", json=payload,
    )


def _last_mark(proj_dir):
    path = proj_dir / "evaluations" / "_feedback.jsonl"
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_the_mark_records_which_button_and_what_became_of_the_suggestion(client, project):
    _judge(project, "perro grande", "perro enorme")

    rv = _mark(client, eval_name="editorial", issue_index=0, feedback_type="resolved",
               via="reader_apply", suggestion_used="edited")
    assert rv.status_code == 200

    record = _last_mark(project)
    assert record["feedback_type"] == "resolved"
    assert record["via"] == "reader_apply"
    assert record["suggestion_used"] == "edited"
    # And the finding has left the working surface.
    assert _review(client)["by_es_idx"] == {}


@pytest.mark.parametrize("used", ["manual", "none"])
def test_a_save_from_edit_is_marked_as_one(client, project, used):
    _judge(project, "perro grande", "perro enorme")

    rv = _mark(client, eval_name="editorial", issue_index=0, feedback_type="resolved",
               via="reader_edit", suggestion_used=used)
    assert rv.status_code == 200

    record = _last_mark(project)
    assert record["via"] == "reader_edit"
    assert record["suggestion_used"] == used


def test_a_hand_clicked_resolved_keeps_its_old_shape(client, project):
    _judge(project, "perro grande", "perro enorme")
    _mark(client, eval_name="editorial", issue_index=0, feedback_type="resolved")

    record = _last_mark(project)
    assert "via" not in record and "suggestion_used" not in record


@pytest.mark.parametrize("payload", [
    {"feedback_type": "resolved", "via": "bulk"},
    {"feedback_type": "resolved", "suggestion_used": "mostly"},
    {"feedback_type": "resolved", "via": ["reader_apply"]},
    {"feedback_type": "resolved", "suggestion_used": {"how": "verbatim"}},
    # These describe a fix; a finding called noise was not fixed.
    {"feedback_type": "false_positive", "via": "reader_apply"},
    {"feedback_type": "false_positive", "suggestion_used": "none"},
])
def test_a_mark_that_does_not_add_up_is_refused(client, project, payload):
    _judge(project, "perro grande", "perro enorme")

    rv = _mark(client, eval_name="editorial", issue_index=0, **payload)
    assert rv.status_code == 400
    assert not (project / "evaluations" / "_feedback.jsonl").exists()


def test_the_metrics_score_it_as_a_real_finding(client, project):
    """`resolved`, not the machine `applied`: a person ruled on this one."""
    from scripts.editorial_metrics import load_marks

    _judge(project, "perro grande", "perro enorme")
    _mark(client, eval_name="editorial", issue_index=0, feedback_type="resolved",
          via="reader_apply", suggestion_used="verbatim")

    marks = load_marks(project)
    assert list(marks[CHUNK_ID].values()) == ["resolved"]


def test_append_feedback_refuses_the_extras_on_another_label(tmp_path):
    with pytest.raises(ValueError):
        append_feedback(tmp_path, CHUNK_ID, "editorial", 0, "applied", via="reader_apply")


# ── the two findings surfaces ────────────────────────────────────────────────


def test_only_the_bin_still_offers_a_plain_resolved():
    """On a sentence, saving is what marks a finding real; the bin has no
    sentence to save, so it keeps the label."""
    reader_js = (_REPO / "web_ui" / "static" / "reader.js").read_text(encoding="utf-8")
    four = "['resolved', 'false_positive', 'bad_message', 'missing_context_gap']"
    three = "['false_positive', 'bad_message', 'missing_context_gap']"

    assert reader_js.count(four) == 1
    assert reader_js.index("function overflowFindingEl") < reader_js.index(four)
    assert reader_js.count(three) == 1
    assert reader_js.index(three) < reader_js.index("function overflowFindingEl")


def test_the_new_sheet_offers_no_plain_resolved_on_a_sentence():
    """Its one feedback call sits behind the filter that takes the label out."""
    sheet_js = (
        _REPO / "web_ui" / "static" / "reader_sheet_v2.js"
    ).read_text(encoding="utf-8")
    without = "FB.filter((pair) => pair[0] !== 'resolved')"
    call = "core().submitFeedback("

    assert sheet_js.count(call) == 1
    assert sheet_js.count(without) == 1
    assert sheet_js.index(without) < sheet_js.index(call)
