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
