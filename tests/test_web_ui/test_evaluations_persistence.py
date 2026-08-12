"""Tests for web_ui/evaluations.py persistence helpers."""

from __future__ import annotations

import json

import pytest

from src.evaluators.location_normalizer import NormalizedIssue, NormalizedLocation
from src.models import EvalResult, Issue, IssueLevel
from web_ui.evaluations import (
    REVIEW_TYPES,
    append_feedback,
    chapter_id_from_chunk_id,
    empty_type_counts,
    load_chapter_type_counts,
    load_chunk_evaluation,
    load_project_summary,
    load_project_type_counts,
    mark_evaluation_stale,
    merge_judge_result,
    merge_llm_judge_result,
    save_chunk_evaluation,
)


def _make_result(name: str = "length", passed: bool = True) -> EvalResult:
    return EvalResult(
        eval_name=name,
        eval_version="1.0.0",
        target_id="ch01_chunk_001",
        target_type="chunk",
        passed=passed,
        score=0.9 if passed else 0.3,
        issues=[
            Issue(
                severity=IssueLevel.WARNING,
                message="Length looks short",
                location="translation",
                suggestion="Check for omissions",
            )
        ],
        metadata={"ratio": 1.15},
    )


def _make_aggregated() -> dict:
    return {
        "total_issues": 1,
        "issues_by_severity": {"error": 0, "warning": 1, "info": 0},
        "issues_by_evaluator": {"length": 1},
        "evaluators_run": ["length"],
        "overall_passed": True,
    }


def _make_normalized_issue() -> NormalizedIssue:
    return NormalizedIssue(
        eval_name="length",
        eval_version="1.0.0",
        issue_index=0,
        severity="warning",
        message="Length looks short",
        suggestion="Check for omissions",
        location=NormalizedLocation(raw="translation", side="translation"),
        metadata_excerpt={"ratio": 1.15},
    )


def test_save_and_load_chunk_evaluation(tmp_path):
    chunk_id = "ch01_chunk_001"
    results = [_make_result()]
    aggregated = _make_aggregated()
    issues = [_make_normalized_issue()]

    path = save_chunk_evaluation(
        tmp_path, chunk_id, results, aggregated, issues,
    )

    assert path.exists()
    assert path == tmp_path / "evaluations" / f"{chunk_id}.json"

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded is not None
    assert loaded["chunk_id"] == chunk_id
    assert loaded["aggregated"] == aggregated
    assert loaded["enabled_evals"] == ["length"]
    assert len(loaded["results"]) == 1
    assert loaded["results"][0]["eval_name"] == "length"
    assert len(loaded["normalized_issues"]) == 1
    assert loaded["normalized_issues"][0]["eval_name"] == "length"
    assert loaded["llm_judge"] is None


def test_save_overwrites_previous(tmp_path):
    chunk_id = "ch01_chunk_001"
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result(passed=False)],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result(passed=True)],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded["results"][0]["passed"] is True


def test_load_missing_chunk_returns_none(tmp_path):
    assert load_chunk_evaluation(tmp_path, "does_not_exist") is None


def test_load_malformed_file_returns_none(tmp_path):
    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir()
    (eval_dir / "bad.json").write_text("not valid json{{{", encoding="utf-8")
    assert load_chunk_evaluation(tmp_path, "bad") is None


def test_merge_llm_judge_preserves_coded_results(tmp_path):
    chunk_id = "ch01_chunk_001"
    save_chunk_evaluation(
        tmp_path,
        chunk_id,
        [_make_result()],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    judge = {"overall_score": 4.2, "notes": "Looks good"}
    merge_llm_judge_result(tmp_path, chunk_id, judge)

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded["llm_judge"] == judge
    assert "llm_judge_at" in loaded
    assert len(loaded["results"]) == 1
    assert loaded["aggregated"]["total_issues"] == 1


def test_merge_llm_judge_creates_shell_if_missing(tmp_path):
    chunk_id = "ch01_chunk_002"
    judge = {"overall_score": 3.0}
    merge_llm_judge_result(tmp_path, chunk_id, judge)

    loaded = load_chunk_evaluation(tmp_path, chunk_id)
    assert loaded is not None
    assert loaded["llm_judge"] == judge
    assert loaded["results"] == []
    assert loaded["enabled_evals"] == []


def test_append_feedback_creates_jsonl(tmp_path):
    chunk_id = "ch01_chunk_001"
    path = append_feedback(
        tmp_path,
        chunk_id,
        "length",
        0,
        "false_positive",
        message="Length looks short",
        note="Actually correct",
    )
    assert path.name == "_feedback.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["chunk_id"] == chunk_id
    assert record["eval_name"] == "length"
    assert record["feedback_type"] == "false_positive"
    assert record["note"] == "Actually correct"
    assert "ts" in record


def test_append_feedback_appends_multiple(tmp_path):
    append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "false_positive")
    append_feedback(tmp_path, "ch01_chunk_001", "length", 1, "bad_message")
    append_feedback(tmp_path, "ch01_chunk_002", "glossary", 0, "missing_context_gap")
    path = tmp_path / "evaluations" / "_feedback.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_append_feedback_rejects_unknown_type(tmp_path):
    with pytest.raises(ValueError, match="Unknown feedback_type"):
        append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "bogus")


def test_append_feedback_accepts_resolved(tmp_path):
    # The reader's Review Mode adds a "resolved" ("real error, handled") label
    # alongside the three quality labels.
    path = append_feedback(tmp_path, "ch01_chunk_001", "blacklist", 2, "resolved")
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["feedback_type"] == "resolved"
    assert record["eval_name"] == "blacklist"
    assert record["issue_index"] == 2


def test_load_project_summary_empty(tmp_path):
    assert load_project_summary(tmp_path) == {}


def test_load_project_summary_aggregates(tmp_path):
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_001",
        [_make_result(passed=False)],
        {
            "total_issues": 3,
            "issues_by_severity": {"error": 1, "warning": 2, "info": 0},
            "issues_by_evaluator": {"length": 3},
            "evaluators_run": ["length"],
            "overall_passed": False,
        },
        [_make_normalized_issue()],
    )
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_002",
        [_make_result()],
        {
            "total_issues": 0,
            "issues_by_severity": {"error": 0, "warning": 0, "info": 0},
            "issues_by_evaluator": {},
            "evaluators_run": ["length"],
            "overall_passed": True,
        },
        [],
    )

    summary = load_project_summary(tmp_path)
    assert set(summary.keys()) == {"ch01_chunk_001", "ch01_chunk_002"}
    assert summary["ch01_chunk_001"] == {
        "errors": 1,
        "warnings": 2,
        "info": 0,
        "total": 3,
    }
    assert summary["ch01_chunk_002"]["total"] == 0


def test_load_project_summary_ignores_feedback_file(tmp_path):
    append_feedback(tmp_path, "ch01_chunk_001", "length", 0, "false_positive")
    save_chunk_evaluation(
        tmp_path,
        "ch01_chunk_001",
        [_make_result()],
        _make_aggregated(),
        [_make_normalized_issue()],
    )
    summary = load_project_summary(tmp_path)
    assert list(summary.keys()) == ["ch01_chunk_001"]


def test_load_project_summary_stale_suppresses_judge_counts(tmp_path):
    from web_ui.evaluations import mark_evaluation_stale, merge_judge_result

    merge_judge_result(
        tmp_path,
        "ch01_chunk_001",
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [{"severity": "error", "message": "bad", "location": "x"}],
        },
    )
    mark_evaluation_stale(tmp_path, "ch01_chunk_001", "text edited")

    summary = load_project_summary(tmp_path)
    assert summary["ch01_chunk_001"] == {
        "errors": 0,
        "warnings": 0,
        "info": 0,
        "total": 0,
        "stale": 1,
    }


def test_evaluate_and_persist_preserves_stale_when_judges_kept(tmp_path):
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from web_ui.evaluations import evaluate_and_persist_chunk, mark_evaluation_stale, merge_judge_result

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="ch01",
        position=0,
        source_text="Hello",
        translated_text="Hola",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=5,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=1,
        ),
        status=ChunkStatus.TRANSLATED,
    )
    merge_judge_result(
        tmp_path,
        chunk.id,
        "dialogue",
        {"eval_name": "dialogue", "issues": [{"severity": "error", "message": "m"}]},
    )
    mark_evaluation_stale(tmp_path, chunk.id, "edited by apply")

    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert payload["stale"] is True
    assert payload["stale_reason"] == "edited by apply"
    assert "dialogue" in payload["judges"]


def test_evaluate_and_persist_return_includes_stale_fields(tmp_path):
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from web_ui.evaluations import evaluate_and_persist_chunk, mark_evaluation_stale, merge_judge_result

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="ch01",
        position=0,
        source_text="Hello",
        translated_text="Hola",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=5,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=1,
        ),
        status=ChunkStatus.TRANSLATED,
    )
    merge_judge_result(
        tmp_path,
        chunk.id,
        "dialogue",
        {"eval_name": "dialogue", "issues": [{"severity": "error", "message": "m"}]},
    )
    mark_evaluation_stale(tmp_path, chunk.id, "edited by apply")

    result = evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    assert result["stale"] is True
    assert result["stale_reason"] == "edited by apply"


# ── Per-chapter review-category counts ───────────────────────────────────────
#
# The chapter list badges flags per chapter and the home card badges them per
# book. Both read the same walk: load_chapter_type_counts does the work and
# load_project_type_counts sums it, so the two views cannot disagree.


def _target_issue(eval_name: str, issue_index: int = 0) -> NormalizedIssue:
    return NormalizedIssue(
        eval_name=eval_name,
        eval_version="1.0.0",
        issue_index=issue_index,
        severity="error",
        message="flagged",
        suggestion="reconsider",
        location=NormalizedLocation(
            raw="char 0-3", side="target", char_start=0, char_end=3, match="abc",
        ),
    )


def test_chapter_type_counts_bucket_chunks_by_chapter(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_001", results=[], aggregated={},
                          normalized_issues=[_target_issue("grammar")])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])

    by_chapter = load_chapter_type_counts(tmp_path)
    assert set(by_chapter) == {"chapter_01", "chapter_02"}
    assert by_chapter["chapter_01"]["blacklist"] == 1
    assert by_chapter["chapter_01"]["grammar"] == 1
    assert by_chapter["chapter_02"]["blacklist"] == 1
    # Every category is present so the template and the JS re-sum can index freely.
    assert set(by_chapter["chapter_02"]) == set(REVIEW_TYPES)


def test_project_type_counts_equal_the_sum_over_chapters(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    merge_judge_result(tmp_path, "chapter_02_chunk_000", "dialogue",
                       {"eval_name": "dialogue", "issues": [{"severity": "warning", "message": "raya"}]})

    totals = load_project_type_counts(tmp_path)
    summed = empty_type_counts()
    for counts in load_chapter_type_counts(tmp_path).values():
        for name, n in counts.items():
            summed[name] += n
    assert totals == summed
    assert totals["blacklist"] == 2
    assert totals["dialogue"] == 1


def test_chunk_id_without_a_chapter_still_reaches_the_project_total(tmp_path):
    # A chunk_id with no _chunk_ marker buckets under itself rather than being
    # dropped: it will never match a chapter row, but the book-wide count on the
    # home card must not silently lose it.
    save_chunk_evaluation(tmp_path, "loose_chunk", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])

    assert chapter_id_from_chunk_id("loose_chunk") == "loose_chunk"
    assert chapter_id_from_chunk_id("chapter_01_chunk_003") == "chapter_01"
    assert load_chapter_type_counts(tmp_path)["loose_chunk"]["blacklist"] == 1
    assert load_project_type_counts(tmp_path)["blacklist"] == 1


def test_chapter_type_counts_skip_stale_and_dismissed(tmp_path):
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist", issue_index=0),
                                             _target_issue("grammar", issue_index=1)])
    save_chunk_evaluation(tmp_path, "chapter_02_chunk_000", results=[], aggregated={},
                          normalized_issues=[_target_issue("blacklist")])
    append_feedback(tmp_path, "chapter_01_chunk_000", "blacklist", 0, "false_positive")
    mark_evaluation_stale(tmp_path, "chapter_02_chunk_000", "edited by apply")

    by_chapter = load_chapter_type_counts(tmp_path)
    assert by_chapter["chapter_01"]["blacklist"] == 0
    assert by_chapter["chapter_01"]["grammar"] == 1
    assert "chapter_02" not in by_chapter


def test_chapter_type_counts_ignore_source_side_findings(tmp_path):
    # Only target-side findings with a char span can be painted, so only those
    # are counted — the same gate the reader's Review Mode applies.
    issue = NormalizedIssue(
        eval_name="blacklist", eval_version="1.0.0", issue_index=0, severity="error",
        message="flagged", suggestion="", location=NormalizedLocation(raw="src", side="source"),
    )
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000", results=[], aggregated={},
                          normalized_issues=[issue])
    assert load_chapter_type_counts(tmp_path)["chapter_01"]["blacklist"] == 0


# ── Per-evaluator freshness ledger ───────────────────────────────────────────
#
# ``eval_runs`` records, per evaluator, the hash of the translated_text it
# judged. That is what lets the Review tab tell "this verdict is current" from
# "this verdict describes prose that has since been rewritten", without every
# edit path having to remember to stamp a flag on its way past.


def _write_chunk(project_dir, chunk_id: str, translated: str, chapter_id: str = "chapter_01"):
    """Write a minimal ``chunks/<id>.json`` — enough for the sha helpers."""
    chunks_dir = project_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    path = chunks_dir / f"{chunk_id}.json"
    path.write_text(
        json.dumps({"id": chunk_id, "chapter_id": chapter_id, "translated_text": translated}),
        encoding="utf-8",
    )
    return path


def _demo_chunk(chunk_id: str = "chapter_01_chunk_000", translated: str = "Hola"):
    from src.models import Chunk, ChunkMetadata, ChunkStatus

    return Chunk(
        id=chunk_id, chapter_id="chapter_01", position=0,
        source_text="Hello there", translated_text=translated,
        metadata=ChunkMetadata(char_start=0, char_end=11, overlap_start=0,
                               overlap_end=0, paragraph_count=1, word_count=2),
        status=ChunkStatus.TRANSLATED,
    )


def test_chunk_text_sha_ignores_newline_style():
    from web_ui.evaluations import chunk_text_sha

    assert chunk_text_sha("a\r\nb") == chunk_text_sha("a\nb")
    assert chunk_text_sha("a\nb") != chunk_text_sha("a\nc")


def test_save_chunk_evaluation_stamps_eval_runs(tmp_path):
    from web_ui.evaluations import chunk_text_sha

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola mundo.")
    save_chunk_evaluation(
        tmp_path, "chapter_01_chunk_000",
        results=[_make_result("grammar")], aggregated={}, normalized_issues=[],
    )
    ledger = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")["eval_runs"]
    assert ledger["grammar"]["text_sha"] == chunk_text_sha("Hola mundo.")
    assert ledger["grammar"]["at"]


def test_merge_judge_result_stamps_eval_runs(tmp_path):
    from web_ui.evaluations import chunk_text_sha

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola mundo.")
    merge_judge_result(
        tmp_path, "chapter_01_chunk_000", "dialogue",
        {"eval_name": "dialogue", "issues": []},
    )
    ledger = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")["eval_runs"]
    assert ledger["dialogue"]["text_sha"] == chunk_text_sha("Hola mundo.")


def test_coded_rerun_leaves_judge_block_and_its_ledger_alone(tmp_path):
    """The point of a per-evaluator ledger: one group cannot speak for another."""
    from web_ui.evaluations import evaluate_and_persist_chunk

    chunk = _demo_chunk()
    _write_chunk(tmp_path, chunk.id, "Hola")
    merge_judge_result(tmp_path, chunk.id, "dialogue",
                       {"eval_name": "dialogue", "issues": []})
    judge_stamp = load_chunk_evaluation(tmp_path, chunk.id)["eval_runs"]["dialogue"]

    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert "dialogue" in payload["judges"]
    assert payload["eval_runs"]["dialogue"] == judge_stamp
    assert "grammar" in payload["eval_runs"]


def test_coded_rerun_cannot_launder_a_stale_legacy_judge_verdict(tmp_path):
    """A pre-``eval_runs`` file has only ``judges_at`` to date its verdict by.
    Carrying the judge block forward without that timestamp would restamp it
    with the coded run's clock and turn a stale verdict green."""
    import datetime as _dt

    from web_ui.evaluations import current_chunk_sha, evaluate_and_persist_chunk, evaluator_freshness

    chunk = _demo_chunk()
    chunk_path = _write_chunk(tmp_path, chunk.id, "Hola")
    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir(parents=True)
    (eval_dir / f"{chunk.id}.json").write_text(json.dumps({
        "chunk_id": chunk.id,
        "evaluated_at": "2020-01-01T00:00:00",
        "enabled_evals": [],
        "judges": {"dialogue": {"eval_name": "dialogue", "issues": []}},
        "judges_at": "2020-01-01T00:00:00",
    }), encoding="utf-8")

    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert payload["judges_at"] == "2020-01-01T00:00:00"
    fresh = evaluator_freshness(
        payload, current_chunk_sha(tmp_path, chunk.id),
        chunk_mtime=chunk_path.stat().st_mtime,
    )
    assert fresh["dialogue"] == "stale"
    assert fresh["grammar"] == "fresh"
    # Sanity: the chunk really is newer than the judge run.
    assert chunk_path.stat().st_mtime > _dt.datetime(2020, 1, 1).timestamp()


def test_judge_rerun_cannot_launder_a_stale_legacy_sibling(tmp_path):
    """The judge-path twin of the coded-rerun test above.

    Re-running *one* judge used to bump the shared ``judges_at`` and drop the
    stale flag for the whole file, so the other judge — with no ledger row of
    its own — was re-dated to now and reported fresh for prose it never saw.
    """
    from web_ui.evaluations import current_chunk_sha, evaluator_freshness_detail

    chunk_path = _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola")
    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir(parents=True)
    (eval_dir / "chapter_01_chunk_000.json").write_text(json.dumps({
        "chunk_id": "chapter_01_chunk_000",
        "evaluated_at": "2020-01-01T00:00:00",
        "enabled_evals": [],
        "judges": {
            "dialogue": {"eval_name": "dialogue", "issues": []},
            "address": {"eval_name": "address", "issues": []},
        },
        "judges_at": "2020-01-01T00:00:00",
    }), encoding="utf-8")

    merge_judge_result(
        tmp_path, "chapter_01_chunk_000", "dialogue",
        {"eval_name": "dialogue", "issues": []},
    )

    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    assert payload["judges_at"] == "2020-01-01T00:00:00"

    detail = evaluator_freshness_detail(
        payload, current_chunk_sha(tmp_path, "chapter_01_chunk_000"),
        names=["dialogue", "address"],
        chunk_mtime=chunk_path.stat().st_mtime,
    )
    assert detail["dialogue"] == {"state": "fresh", "basis": "hash"}
    assert detail["address"]["state"] == "stale"


def test_the_stale_flag_stops_covering_a_judge_that_re_ran_after_it(tmp_path):
    """``stale`` is written per chunk but means "the text moved under the
    verdicts recorded before this edit". A judge that re-ran *after* the stamp
    has its own newer evidence — clearing the flag for everyone laundered the
    others, and keeping it for everyone left the re-run judge wrongly red."""
    from web_ui.evaluations import current_chunk_sha, evaluator_freshness_detail

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola")
    for judge in ("dialogue", "address"):
        merge_judge_result(
            tmp_path, "chapter_01_chunk_000", judge,
            {"eval_name": judge, "issues": []},
        )
    mark_evaluation_stale(
        tmp_path, "chapter_01_chunk_000",
        "translated_text edited by judge-review apply (address)",
    )

    # Both are covered by the flag while neither has re-run.
    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    sha = current_chunk_sha(tmp_path, "chapter_01_chunk_000")
    before = evaluator_freshness_detail(payload, sha, names=["dialogue", "address"])
    assert before["dialogue"] == {"state": "stale", "basis": "flag"}
    assert before["address"] == {"state": "stale", "basis": "flag"}

    merge_judge_result(
        tmp_path, "chapter_01_chunk_000", "dialogue",
        {"eval_name": "dialogue", "issues": []},
    )

    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    after = evaluator_freshness_detail(payload, sha, names=["dialogue", "address"])
    assert after["dialogue"] == {"state": "fresh", "basis": "hash"}
    assert after["address"] == {"state": "stale", "basis": "flag"}
    assert payload["stale"] is True


def test_evaluate_and_persist_stamps_the_text_it_evaluated(tmp_path):
    """The ledger must describe the prose the findings came from. Hashing the
    chunk on disk at write time let a chunk-editor save land mid-run and stamp
    the *new* text's hash, so the badge read fresh for findings that predate
    the edit and never went stale."""
    from web_ui.evaluations import chunk_text_sha, evaluate_and_persist_chunk

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Texto editado despues.")
    chunk = _demo_chunk(translated="Texto que se evaluo.")

    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    stamped = {e["text_sha"] for e in payload["eval_runs"].values()}
    assert stamped == {chunk_text_sha("Texto que se evaluo.")}
    assert chunk_text_sha("Texto editado despues.") not in stamped


def test_evaluator_freshness_reads_stale_from_the_hash(tmp_path):
    """The four leak paths this closes: any edit changes the hash, full stop."""
    from web_ui.evaluations import current_chunk_sha, evaluator_freshness

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola mundo.")
    save_chunk_evaluation(
        tmp_path, "chapter_01_chunk_000",
        results=[_make_result("grammar")], aggregated={}, normalized_issues=[],
    )
    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    fresh = evaluator_freshness(payload, current_chunk_sha(tmp_path, "chapter_01_chunk_000"))
    assert fresh["grammar"] == "fresh"

    # Simulate a chunk-editor / correction / sentence-replace edit: nothing
    # tells the evaluation anything, the text just changes underneath it.
    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola mundo cruel.")
    fresh = evaluator_freshness(payload, current_chunk_sha(tmp_path, "chapter_01_chunk_000"))
    assert fresh["grammar"] == "stale"


def test_evaluator_freshness_reports_missing_for_requested_names(tmp_path):
    from web_ui.evaluations import evaluator_freshness

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola.")
    save_chunk_evaluation(tmp_path, "chapter_01_chunk_000",
                          results=[_make_result("grammar")], aggregated={},
                          normalized_issues=[])
    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    fresh = evaluator_freshness(payload, None, names=["grammar", "dialogue"])
    assert fresh == {"grammar": "fresh", "dialogue": "missing"}
    assert evaluator_freshness(None, None, names=["grammar"]) == {"grammar": "missing"}


def test_evaluator_freshness_legacy_file_falls_back_to_mtime(tmp_path):
    """Books evaluated before ``eval_runs`` existed keep working."""
    import datetime as _dt

    from web_ui.evaluations import evaluator_freshness

    eval_dir = tmp_path / "evaluations"
    eval_dir.mkdir(parents=True)
    (eval_dir / "chapter_01_chunk_000.json").write_text(json.dumps({
        "chunk_id": "chapter_01_chunk_000",
        "evaluated_at": "2026-01-01T00:00:00",
        "enabled_evals": ["grammar"],
        "judges": {"dialogue": {"eval_name": "dialogue", "issues": []}},
        "judges_at": "2026-01-01T00:00:00",
    }), encoding="utf-8")
    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")

    before = _dt.datetime(2025, 12, 31).timestamp()
    after = _dt.datetime(2026, 2, 1).timestamp()

    assert evaluator_freshness(payload, "abc", chunk_mtime=before) == {
        "dialogue": "fresh", "grammar": "fresh",
    }
    assert evaluator_freshness(payload, "abc", chunk_mtime=after) == {
        "dialogue": "stale", "grammar": "stale",
    }


def test_evaluator_freshness_honours_explicit_stale_flag(tmp_path):
    from web_ui.evaluations import current_chunk_sha, evaluator_freshness

    _write_chunk(tmp_path, "chapter_01_chunk_000", "Hola.")
    merge_judge_result(tmp_path, "chapter_01_chunk_000", "dialogue",
                       {"eval_name": "dialogue", "issues": []})
    mark_evaluation_stale(tmp_path, "chapter_01_chunk_000", "edited by apply")
    payload = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    sha = current_chunk_sha(tmp_path, "chapter_01_chunk_000")
    assert evaluator_freshness(payload, sha)["dialogue"] == "stale"


def test_narrowed_coded_set_is_not_a_permanent_partial(tmp_path, monkeypatch):
    """app_config may run fewer evaluators; the ledger records what actually ran,
    so the coded group reads "fresh" rather than a forever "partial"."""
    import web_ui.evaluations as ev

    monkeypatch.setattr(ev, "get_enabled_evaluators", lambda: ["length", "paragraph"])
    chunk = _demo_chunk()
    _write_chunk(tmp_path, chunk.id, "Hola")
    ev.evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None)

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert set(payload["eval_runs"]) == {"length", "paragraph"}
    states = ev.chunk_group_states(
        ev.evaluator_freshness(payload, ev.current_chunk_sha(tmp_path, chunk.id))
    )
    assert states["coded"] == "fresh"
    assert states["dialogue"] == "missing"


def test_narrowed_rerun_keeps_other_evaluators_findings(tmp_path):
    """Rerunning one evaluator must not delete the rest of the coded findings."""
    from web_ui.evaluations import evaluate_and_persist_chunk

    chunk = _demo_chunk()
    _write_chunk(tmp_path, chunk.id, "Hola")
    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None,
                               enabled_evals=["length", "paragraph"])
    evaluate_and_persist_chunk(tmp_path, chunk, glossary=None, blacklist=None,
                               enabled_evals=["length"])

    payload = load_chunk_evaluation(tmp_path, chunk.id)
    assert {r["eval_name"] for r in payload["results"]} == {"length", "paragraph"}
    assert set(payload["eval_runs"]) == {"length", "paragraph"}


def test_rollup_group_state_prefers_stale_over_partial():
    from web_ui.evaluations import rollup_group_state

    assert rollup_group_state([])["state"] == "not_run"
    assert rollup_group_state(["missing", "missing"])["state"] == "not_run"
    assert rollup_group_state(["fresh", "fresh"])["state"] == "done"
    assert rollup_group_state(["fresh", "missing"])["state"] == "partial"
    assert rollup_group_state(["fresh", "missing", "stale"])["state"] == "stale"
    assert rollup_group_state(["fresh", "stale"]) == {
        "state": "stale", "fresh": 1, "stale": 1, "missing": 0,
    }
