"""Tests for the reader Review-Mode endpoint (/api/project/<id>/review/<ch>)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
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


# Two sentences in one chunk; the reader renders `es`, the pipeline stores the
# byte-identical `translated_text`. "negro" (a coded finding) sits at char 8.
_TRANSLATED = "El gato negro. El perro grande."


@pytest.fixture
def review_project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "revproj"
    (proj_dir / "alignments").mkdir(parents=True)
    (proj_dir / "chunks").mkdir(parents=True)

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "revproj",
        "high_confidence_pct": 100.0,
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "es": "El gato negro.", "en": "The black cat.",
             "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro grande.", "en": "The big dog.",
             "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        ],
    }
    (proj_dir / "alignments" / "chapter_01.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
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


def _save_blacklist_finding(proj_dir):
    """Persist one target-side coded finding whose match is 'negro' at char 8."""
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
    )


def test_review_anchors_coded_finding_to_word(client, review_project):
    _save_blacklist_finding(review_project)

    rv = client.get("/api/project/revproj/review/chapter_01")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
    assert body["type_counts"].get("blacklist") == 1

    findings = body["by_es_idx"]["0"]
    assert len(findings) == 1
    f = findings[0]
    assert f["eval_name"] == "blacklist"
    assert f["chunk_id"] == "chapter_01_chunk_000"
    # Word-level span, resolved by text search inside the sentence.
    assert f["match"] == "negro"
    assert (f["match_start"], f["match_end"]) == (8, 13)
    assert _TRANSLATED[f["match_start"]:f["match_end"]] == "negro"


def test_review_anchors_judge_excerpt_sentence_level(client, review_project):
    merge_judge_result(
        review_project, "chapter_01_chunk_000", "dialogue",
        {
            "eval_name": "dialogue",
            "eval_version": "1.0.0",
            "issues": [{
                "severity": "warning",
                "message": "dialogue formatting",
                "location": "El perro grande.",
                "suggestion": "use raya",
            }],
        },
    )

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["type_counts"].get("dialogue") == 1
    # Judge excerpt anchors to the second sentence, sentence-level (no span).
    findings = body["by_es_idx"]["1"]
    assert len(findings) == 1
    assert findings[0]["eval_name"] == "dialogue"
    assert findings[0]["match_start"] is None


def test_review_omits_dismissed_findings(client, review_project):
    _save_blacklist_finding(review_project)
    # Any feedback record for (eval_name, issue_index) dismisses the finding.
    append_feedback(review_project, "chapter_01_chunk_000", "blacklist", 0, "resolved")

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["by_es_idx"] == {}
    assert body["type_counts"] == {}


def test_review_skips_stale_chunk(client, review_project):
    _save_blacklist_finding(review_project)
    mark_evaluation_stale(review_project, "chapter_01_chunk_000", "text edited")

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["by_es_idx"] == {}
    assert body["stale_chunks"] == 1


def test_review_ignores_non_highlightable_types(client, review_project):
    # A length (aggregate, side=none) finding must not surface in the reader.
    issue = NormalizedIssue(
        eval_name="length",
        eval_version="1.0.0",
        issue_index=0,
        severity="warning",
        message="length ratio off",
        suggestion=None,
        location=NormalizedLocation(raw="translation", side="none"),
    )
    save_chunk_evaluation(
        review_project, "chapter_01_chunk_000",
        results=[], aggregated={}, normalized_issues=[issue],
    )

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["by_es_idx"] == {}
    assert body["type_counts"] == {}


def test_review_bad_ids_rejected(client, review_project):
    assert client.get("/api/project/..%2Fx/review/chapter_01").status_code in (400, 404)
    assert client.get("/api/project/revproj/review/nope").status_code == 404


# ── Findings the reader cannot place on a sentence ───────────────────────────
#
# Everything here guards the same invariant: a finding this endpoint counts is
# a finding the reader can reach. Either it anchors to a sentence, or it comes
# back in `unanchored` for the end-of-chapter overflow bin — never dropped.


@pytest.fixture
def make_project(tmp_path, monkeypatch):
    """Build a project with an explicit chunk text and alignment rows.

    The rows are given as ``(es_idx, es)`` so a test can deliberately leave a
    sentence of the chunk *uncovered*, which is what separates a whitespace gap
    (bridgeable) from a stretch of real prose (not).
    """
    projects_dir = tmp_path / "projects"

    def build(project_id: str, translated: str, rows: list[tuple[int, str]]):
        proj_dir = projects_dir / project_id
        (proj_dir / "alignments").mkdir(parents=True)
        (proj_dir / "chunks").mkdir(parents=True)
        alignment = {
            "chapter_id": "chapter_01",
            "project_id": project_id,
            "alignments": [
                {"es_idx": idx, "en_idx": idx, "es": es, "en": "",
                 "confidence": "high", "chunk_id": "chapter_01_chunk_000"}
                for idx, es in rows
            ],
        }
        (proj_dir / "alignments" / "chapter_01.json").write_text(
            json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
        )
        (proj_dir / "chunks" / "chapter_01_chunk_000.json").write_text(
            json.dumps({"id": "chapter_01_chunk_000", "translated_text": translated},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return proj_dir

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return build


def _save_coded_finding(proj_dir, *, char_start, char_end, match, eval_name="blacklist"):
    issue = NormalizedIssue(
        eval_name=eval_name,
        eval_version="1.0.0",
        issue_index=0,
        severity="error",
        message="flagged",
        suggestion=None,
        location=NormalizedLocation(
            raw=f"char {char_start}-{char_end}", side="target",
            char_start=char_start, char_end=char_end, match=match,
        ),
    )
    save_chunk_evaluation(
        proj_dir, "chapter_01_chunk_000",
        results=[], aggregated={}, normalized_issues=[issue],
    )


def _save_judge_finding(proj_dir, location, judge="address"):
    merge_judge_result(
        proj_dir, "chapter_01_chunk_000", judge,
        {
            "eval_name": judge,
            "eval_version": "1.0.0",
            "issues": [{
                "severity": "warning",
                "message": "forms of address",
                "location": location,
                "suggestion": "use usted",
            }],
        },
    )


def test_unplaceable_judge_excerpt_reaches_the_bin(client, review_project):
    # The address judge's habit: an excerpt with an elision, which can never
    # match the prose verbatim however current the run is.
    _save_judge_finding(review_project, "El gato ... negro.")

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["by_es_idx"] == {}
    assert body["type_counts"].get("address") == 1
    assert len(body["unanchored"]) == 1
    f = body["unanchored"][0]
    assert f["eval_name"] == "address"
    assert f["chunk_id"] == "chapter_01_chunk_000"
    assert f["issue_index"] == 0
    assert f["excerpt"] == "El gato ... negro."
    assert f["message"] == "forms of address"
    assert f["suggestion"] == "use usted"
    # Run is current, so the excerpt — not the prose — is what is wrong.
    assert f["reason"] == "unplaceable"


def test_judge_finding_on_edited_prose_reads_obsolete(client, review_project):
    _save_judge_finding(review_project, "Ven, mi senor.")
    # Edit the chunk *after* the judge ran, exactly as chapter 7 of
    # the-little-duke did: the ledger's text_sha no longer matches, but nothing
    # set the chunk-level `stale` flag, so the chunk is still walked.
    chunk_path = review_project / "chunks" / "chapter_01_chunk_000.json"
    chunk_path.write_text(
        json.dumps({"id": "chapter_01_chunk_000",
                    "translated_text": _TRANSLATED + " Y llovia."},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["stale_chunks"] == 0
    assert len(body["unanchored"]) == 1
    assert body["unanchored"][0]["reason"] == "obsolete"


def test_finding_in_a_whitespace_gap_anchors_to_the_next_sentence(client, make_project):
    # Sentence spans do not tile the chunk: char 14-15 is the paragraph break
    # between the two sentences, covered by neither row.
    proj = make_project(
        "gapproj", "El gato negro.\n\nEl perro grande.",
        [(0, "El gato negro."), (1, "El perro grande.")],
    )
    _save_coded_finding(proj, char_start=14, char_end=16, match="")

    body = client.get("/api/project/gapproj/review/chapter_01").get_json()
    assert body["unanchored"] == []
    assert list(body["by_es_idx"]) == ["1"]
    assert body["type_counts"].get("blacklist") == 1


def test_finding_on_uncovered_prose_is_not_bridged(client, make_project):
    # "Dos ladra." is in the chunk but has no alignment row. A finding inside it
    # must not jump to the next covered sentence — it goes to the bin instead.
    proj = make_project(
        "prosegapproj", "Uno amanece. Dos ladra. Tres corre.",
        [(0, "Uno amanece."), (2, "Tres corre.")],
    )
    _save_coded_finding(proj, char_start=13, char_end=16, match="Dos")

    body = client.get("/api/project/prosegapproj/review/chapter_01").get_json()
    assert body["by_es_idx"] == {}
    assert len(body["unanchored"]) == 1
    assert body["unanchored"][0]["excerpt"] == "Dos"
    assert body["type_counts"].get("blacklist") == 1


def test_dismissed_unanchored_finding_leaves_the_bin(client, review_project):
    _save_judge_finding(review_project, "El gato ... negro.")
    append_feedback(review_project, "chapter_01_chunk_000", "address", 0, "false_positive")

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["unanchored"] == []
    assert body["type_counts"] == {}


def test_stale_chunks_stay_out_of_the_bin(client, review_project):
    # A stale chunk is already surfaced by the stale_chunks toast; re-listing
    # its findings in the bin would double-report them.
    _save_judge_finding(review_project, "El gato ... negro.")
    mark_evaluation_stale(review_project, "chapter_01_chunk_000", "text edited")

    body = client.get("/api/project/revproj/review/chapter_01").get_json()
    assert body["unanchored"] == []
    assert body["stale_chunks"] == 1


def test_string_char_start_does_not_500(client, review_project):
    _save_coded_finding(review_project, char_start=8, char_end=13, match="negro")
    eval_path = review_project / "evaluations" / "chapter_01_chunk_000.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    payload["normalized_issues"][0]["location"]["char_start"] = "8"
    eval_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    rv = client.get("/api/project/revproj/review/chapter_01")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
