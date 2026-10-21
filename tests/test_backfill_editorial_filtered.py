"""Tests for the editorial ``filtered`` backfill script.

The script writes recovered findings into evaluations, so what is worth pinning
is that it writes only what a replay proves came from the run on record, and
never touches a file in report-only mode.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_editorial_filtered import (  # noqa: E402
    backfill_project,
    discover_projects,
)
from src.judges.base import JudgeTarget  # noqa: E402
from src.judges.editorial_judge import EditorialJudge  # noqa: E402

CHUNK = "chapter_01_chunk_000"


def _finding(**overrides) -> dict:
    finding = {
        "rule": "calque-syntax",
        "category": "NATURALNESS",
        "severity": "warning",
        "confidence": "high",
        "excerpt": "La habitación estaba desnuda y calurosa",
        "message": "Calqued English word order.",
        "suggestion": "El cuarto estaba desnudo y caluroso",
        "source_check": "not_needed",
    }
    finding.update(overrides)
    return finding


DROPPY = [
    _finding(rule="b", severity="error"),
    _finding(rule="c"),
    _finding(rule="m", confidence="medium"),
    _finding(rule="d", severity="info"),
]


def _project(tmp_path: Path, findings: list[dict], *, draft: bool = True) -> Path:
    """A book whose evaluation is a real pass-1 result written before ``filtered``."""
    raw = json.dumps({"findings": findings, "summary": "ok"}, ensure_ascii=False)
    target = JudgeTarget(
        id=CHUNK, target_type="chunk", source_text="", translated_text="palabra " * 400
    )  # budget 2
    result = EditorialJudge().parse_response(target, raw, {}).model_dump(mode="json")
    del result["metadata"]["filtered"]

    project = tmp_path / "book"
    (project / "evaluations").mkdir(parents=True)
    (project / "evaluations" / f"{CHUNK}.json").write_text(
        json.dumps({"chunk_id": CHUNK, "judges": {"editorial": result}}, ensure_ascii=False),
        encoding="utf-8",
    )
    if draft:
        judges = project / ".harness" / "judges"
        judges.mkdir(parents=True)
        (judges / f"{CHUNK}.editorial.draft.json").write_text(raw, encoding="utf-8")
    return project


def _metadata(project: Path) -> dict:
    payload = json.loads((project / "evaluations" / f"{CHUNK}.json").read_text(encoding="utf-8"))
    return payload["judges"]["editorial"]["metadata"]


def test_a_matching_draft_is_recovered_with_each_drop_reason(tmp_path):
    project = _project(tmp_path, DROPPY)

    stats = backfill_project(project, write=True)
    metadata = _metadata(project)

    assert stats["recovered"] == 1
    assert {f["rule"]: f["drop_reason"] for f in metadata["filtered"]} == {
        "m": "below_confidence",
        "d": "over_budget",
    }
    assert metadata["filtered_backfilled"] is True
    assert (project / ".harness" / "backups" / "editorial-filtered" / f"{CHUNK}.json").exists()


def test_report_only_changes_no_file(tmp_path):
    project = _project(tmp_path, DROPPY)
    path = project / "evaluations" / f"{CHUNK}.json"
    before = path.read_bytes()

    stats = backfill_project(project, write=False)

    assert stats["recovered"] == 1
    assert path.read_bytes() == before
    assert not (project / ".harness" / "backups").exists()


def test_a_draft_from_another_run_is_not_trusted(tmp_path):
    project = _project(tmp_path, DROPPY)
    draft = project / ".harness" / "judges" / f"{CHUNK}.editorial.draft.json"
    draft.write_text(json.dumps({"findings": DROPPY[:2], "summary": "ok"}), encoding="utf-8")

    stats = backfill_project(project, write=True)
    metadata = _metadata(project)

    assert stats["lost"] == 1
    assert metadata["filtered_lost"] is True
    assert "filtered" not in metadata


def test_a_missing_draft_records_the_gap(tmp_path):
    project = _project(tmp_path, DROPPY, draft=False)

    stats = backfill_project(project, write=True)

    assert stats["lost"] == 1
    assert stats["findings_lost"] == 2
    assert _metadata(project)["filtered_lost"] is True


def test_a_replay_that_disagrees_is_left_alone(tmp_path):
    project = _project(tmp_path, DROPPY)
    path = project / "evaluations" / f"{CHUNK}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judges"]["editorial"]["metadata"]["candidates"].reverse()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    stats = backfill_project(project, write=True)

    assert stats["mismatched"] == 1
    assert path.read_bytes() == before


def test_a_grouped_draft_is_not_read_as_lost(tmp_path):
    """A batch draft may hold this chunk; a permanent ``filtered_lost`` would bury it."""
    project = _project(tmp_path, DROPPY, draft=False)
    judges = project / ".harness" / "judges"
    judges.mkdir(parents=True)
    (judges / "batch_001.editorial.draft.json").write_text("{}", encoding="utf-8")
    path = project / "evaluations" / f"{CHUNK}.json"
    before = path.read_bytes()

    stats = backfill_project(project, write=True)

    assert stats["mismatched"] == 1
    assert stats["files_written"] == 0
    assert path.read_bytes() == before


def test_a_malformed_judges_field_is_skipped_and_the_run_continues(tmp_path):
    project = _project(tmp_path, DROPPY)
    (project / "evaluations" / "chapter_00_chunk_000.json").write_text(
        json.dumps({"judges": ["editorial"]}), encoding="utf-8"
    )

    stats = backfill_project(project, write=True)

    assert stats["recovered"] == 1


def test_discover_skips_snapshots_at_any_depth(tmp_path):
    root = tmp_path / "projects"
    for rel in (
        "book",
        ".published/other",
        "book/ab_chapter_02/A_backup",
        ".backburner/book.bak-migration",
        ".backburner/book.bak-migration/ab_chapter_02/B",
    ):
        (root / rel / "evaluations").mkdir(parents=True)

    assert discover_projects(root) == [root / ".published" / "other", root / "book"]


def test_nothing_dropped_writes_an_empty_list_without_a_draft(tmp_path):
    project = _project(tmp_path, [_finding()], draft=False)

    stats = backfill_project(project, write=True)

    assert stats["zero_drop"] == 1
    assert _metadata(project)["filtered"] == []


def test_a_second_run_changes_nothing(tmp_path):
    project = _project(tmp_path, DROPPY)
    backfill_project(project, write=True)
    path = project / "evaluations" / f"{CHUNK}.json"
    before = path.read_bytes()

    stats = backfill_project(project, write=True)

    assert stats["already"] == 1
    assert stats["files_written"] == 0
    assert path.read_bytes() == before
