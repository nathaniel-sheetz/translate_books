"""Tests for the dashboard Review tab rollup (/api/project/<id>/review-status).

The tab's whole claim is that its four-state status cells are honest. These
tests pin the two halves of that: the state machine (done / partial / stale /
not_run) and the rule that a chunk edited outside the judge pipeline goes stale
purely from its content hash — no cooperation from the editing path required.
"""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from web_ui.app import app
from web_ui.evaluations import (
    merge_judge_result,
    save_chunk_evaluation,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A two-chunk chapter, translated, with chapters/ and alignments/ present."""
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "revstat"
    (proj / "chunks").mkdir(parents=True)
    (proj / "chapters").mkdir(parents=True)
    (proj / "alignments").mkdir(parents=True)

    (proj / "chapters" / "chapter_01.txt").write_text("El gato.", encoding="utf-8")
    for i, text in enumerate(["El gato negro.", "El perro grande."]):
        write_chunk(proj, f"chapter_01_chunk_{i:03d}", text)

    (proj / "alignments" / "chapter_01.json").write_text(json.dumps({
        "chapter_id": "chapter_01",
        "coverage": {"gap_count": 2, "en_orphan_chars": 140},
        "alignments": [],
    }), encoding="utf-8")

    import web_ui.app as app_module
    # _NESTED_PROJECT_CACHE outlives a single test, so a stale entry from an
    # earlier fixture would resolve this project to a deleted tmp_path.
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj


def write_chunk(proj, chunk_id: str, translated: str, chapter_id: str = "chapter_01"):
    path = proj / "chunks" / f"{chunk_id}.json"
    path.write_text(json.dumps({
        "id": chunk_id, "chapter_id": chapter_id, "position": 0,
        "source_text": "The black cat.", "translated_text": translated,
    }), encoding="utf-8")
    return path


def _coded_run(proj, chunk_id: str, *, issues=()):
    from src.models import EvalResult

    results = [
        EvalResult(eval_name=name, eval_version="1.0.0", target_id=chunk_id,
                   target_type="chunk", passed=True, score=1.0, issues=[])
        for name in ("grammar", "blacklist")
    ]
    save_chunk_evaluation(proj, chunk_id, results, {}, list(issues))


def _chapter(body, chapter_id="chapter_01"):
    return next(c for c in body["chapters"] if c["id"] == chapter_id)


# ── The four states ──────────────────────────────────────────────────────────


def test_not_run_when_nothing_evaluated(client, project):
    body = client.get("/api/project/revstat/review-status").get_json()
    ch = _chapter(body)
    assert ch["judges"]["coded"]["state"] == "not_run"
    assert ch["judges"]["dialogue"]["state"] == "not_run"
    assert ch["judges"]["coded"]["missing"] == 2


def test_done_when_every_chunk_is_current(client, project):
    for i in range(2):
        _coded_run(project, f"chapter_01_chunk_{i:03d}")
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["judges"]["coded"] == {"state": "done", "fresh": 2, "stale": 0, "missing": 0}


def test_partial_when_only_some_chunks_ran(client, project):
    _coded_run(project, "chapter_01_chunk_000")
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["judges"]["coded"]["state"] == "partial"
    assert (ch["judges"]["coded"]["fresh"], ch["judges"]["coded"]["missing"]) == (1, 1)


def test_stale_from_the_hash_alone_after_an_out_of_band_edit(client, project):
    """The bug this tab exists to fix.

    The chunk editor, /api/correction, /api/apply-corrections and
    /api/sentence/replace all rewrite translated_text without touching the
    persisted evaluation. None of them is involved here — the chunk file simply
    changes, and the badge has to notice.
    """
    for i in range(2):
        _coded_run(project, f"chapter_01_chunk_{i:03d}")
        merge_judge_result(project, f"chapter_01_chunk_{i:03d}", "dialogue",
                           {"eval_name": "dialogue", "issues": []})
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["judges"]["dialogue"]["state"] == "done"

    write_chunk(project, "chapter_01_chunk_001", "El perro grande y viejo.")

    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["judges"]["coded"]["state"] == "stale"
    assert ch["judges"]["dialogue"]["state"] == "stale"
    assert ch["judges"]["dialogue"]["stale"] == 1
    assert ch["judges"]["dialogue"]["fresh"] == 1


def test_stale_beats_partial_in_the_rollup(client, project):
    _coded_run(project, "chapter_01_chunk_000")
    write_chunk(project, "chapter_01_chunk_000", "Otro texto.")
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    # One stale chunk, one never evaluated: "stale" is the honest headline.
    assert ch["judges"]["coded"]["state"] == "stale"


# ── The row's other columns ──────────────────────────────────────────────────


def test_flag_counts_agree_with_what_the_reader_renders(client, project):
    """Both views read load_chapter_type_counts, so they cannot disagree."""
    issue = NormalizedIssue(
        eval_name="blacklist", eval_version="1.0.0", issue_index=0,
        severity="error", message="flagged", suggestion="",
        location=NormalizedLocation(raw="char 8-13", side="target",
                                    char_start=8, char_end=13, match="negro"),
    )
    _coded_run(project, "chapter_01_chunk_000", issues=[issue])

    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["flag_counts"]["blacklist"] == 1

    from web_ui.evaluations import load_chapter_type_counts
    assert load_chapter_type_counts(project)["chapter_01"]["blacklist"] == 1


def test_annotations_and_gaps_and_reviewed(client, project):
    (project / "annotations.jsonl").write_text("\n".join([
        json.dumps({"chapter_id": "chapter_01", "es_idx": 0, "type": "word_choice",
                    "content": "hmm", "timestamp": "2026-01-01T00:00:00"}),
        json.dumps({"chapter_id": "chapter_01", "es_idx": 1, "type": "footnote",
                    "content": "a real gloss", "timestamp": "2026-01-01T00:00:01"}),
        json.dumps({"chapter_id": "chapter_01", "es_idx": 2, "type": "footnote",
                    "content": "", "timestamp": "2026-01-01T00:00:02"}),
    ]) + "\n", encoding="utf-8")
    (project / "reviewed.json").write_text(
        json.dumps({"chapter_01": "2026-01-01T00:00:00"}), encoding="utf-8")

    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["annotations"] == {"review": 1, "footnotes_total": 2, "footnotes_filled": 1}
    assert ch["reviewed"] is True
    assert (ch["gap_count"], ch["gap_chars"]) == (2, 140)
    assert ch["has_alignment"] is True


def test_untranslated_chunks_are_counted_but_not_judged(client, project):
    write_chunk(project, "chapter_01_chunk_002", "")
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert (ch["chunk_count"], ch["translated_count"]) == (3, 2)
    # The empty chunk contributes nothing to the judge rollup.
    assert ch["judges"]["coded"]["missing"] == 2


def test_totals_roll_up_from_chunks_not_from_chapter_verdicts(client, project):
    (project / "chapters" / "chapter_02.txt").write_text("x", encoding="utf-8")
    write_chunk(project, "chapter_02_chunk_000", "Hola.", chapter_id="chapter_02")
    for i in range(2):
        _coded_run(project, f"chapter_01_chunk_{i:03d}")

    body = client.get("/api/project/revstat/review-status").get_json()
    totals = body["totals"]
    assert totals["translated_count"] == 3
    assert totals["judges"]["coded"] == {
        "state": "partial", "fresh": 2, "stale": 0, "missing": 1,
    }
    assert totals["findings"] == 0


def test_no_alignment_still_lists_the_chapter(client, project):
    (project / "alignments" / "chapter_01.json").unlink()
    ch = _chapter(client.get("/api/project/revstat/review-status").get_json())
    assert ch["has_alignment"] is False
    assert ch["gap_count"] == 0


def test_bad_project_id_and_missing_project(client, monkeypatch, tmp_path):
    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: tmp_path / "projects")
    assert client.get("/api/project/..%2Fetc/review-status").status_code in (400, 404)
    assert client.get("/api/project/nosuch/review-status").status_code == 404


# ── Reader deep link ─────────────────────────────────────────────────────────


def test_review_query_param_forces_review_mode_for_one_load(client, project):
    html = client.get("/read/revstat/chapter_01?review=dialogue").get_data(as_text=True)
    assert "window.REVIEW_FORCED_TYPES = [\"dialogue\"]" in html
    # The picker still renders the reader's own saved selection — the deep link
    # must not look like the user changed their settings.
    assert '"blacklist"' in html


def test_review_query_param_ignores_unknown_categories(client, project):
    html = client.get("/read/revstat/chapter_01?review=nonsense").get_data(as_text=True)
    assert "window.REVIEW_FORCED_TYPES = []" in html


def test_review_query_param_sets_no_cookie(client, project):
    resp = client.get("/read/revstat/chapter_01?review=dialogue")
    assert "reader_review_types" not in resp.headers.get("Set-Cookie", "")
