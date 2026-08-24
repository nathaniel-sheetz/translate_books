"""Purge coded findings whose char_start sits inside an ``[IMAGE:...]`` token."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.purge_image_placeholder_findings import clean_project, image_spans


_TOKEN = "[IMAGE:images/i001.jpg]"
_TRANSLATED = f"Hello {_TOKEN} world."


def _issue(issue_index: int, char_start: int, eval_name: str = "grammar") -> dict:
    return {
        "eval_name": eval_name,
        "issue_index": issue_index,
        "severity": "error",
        "message": "flagged",
        "location": {"side": "target", "char_start": char_start, "match": "x"},
    }


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "projects" / "book"
    (proj / "chunks").mkdir(parents=True)
    (proj / "evaluations").mkdir()
    (proj / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({"id": "chapter_01_chunk_000", "translated_text": _TRANSLATED}),
        encoding="utf-8",
    )
    start, end = image_spans(_TRANSLATED)[0]
    payload = {
        "chunk_id": "chapter_01_chunk_000",
        "normalized_issues": [
            _issue(0, start),
            _issue(7, end),
        ],
    }
    (proj / "evaluations" / "chapter_01_chunk_000.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return proj


def test_dry_run_writes_nothing(tmp_path):
    proj = _project(tmp_path)
    eval_path = proj / "evaluations" / "chapter_01_chunk_000.json"
    before = eval_path.read_text(encoding="utf-8")

    removed, by_eval = clean_project(proj, apply=False)

    assert removed == 1
    assert by_eval == {"grammar": 1}
    assert eval_path.read_text(encoding="utf-8") == before


def test_apply_drops_inside_token_and_keeps_survivor_index(tmp_path):
    proj = _project(tmp_path)

    removed, by_eval = clean_project(proj, apply=True)

    assert removed == 1
    assert by_eval == {"grammar": 1}
    kept = json.loads(
        (proj / "evaluations" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
    )["normalized_issues"]
    assert len(kept) == 1
    assert kept[0]["issue_index"] == 7
    assert kept[0]["location"]["char_start"] == image_spans(_TRANSLATED)[0][1]
