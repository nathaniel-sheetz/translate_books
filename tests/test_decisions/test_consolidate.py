"""The consolidate step: merge, prepare, coverage audit and commit."""

from __future__ import annotations

import json

import pytest

from src.decisions import consolidate as dcons
from src.decisions import decisions_dir
from src.decisions import taxonomy as dtax

from tests.test_decisions.test_scan import TAXONOMY


def _cand(chapter, category, subject, **extra):
    return {
        "chapter_id": chapter,
        "category": category,
        "subject": subject,
        "subject_key": extra.pop("key", subject.casefold()),
        "question": extra.pop("question", f"What about {subject}?"),
        "options": extra.pop("options", [{"label": "a", "effect": "x"}]),
        "quotes": extra.pop("quotes", [f"{subject} said hello."]),
        "why_hard": "",
        "house_default": extra.pop("house_default", False),
    }


CANDIDATES = [
    _cand("chapter_01", "names", "Thunder"),
    _cand("chapter_02", "names", "Thunder"),
    _cand("chapter_02", "names", "thunder", key="thunder"),
    _cand("chapter_01", "names", "Old Bess"),
    _cand("chapter_01", "address-pairs", "Hobbs -> Polly"),
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    tax = tmp_path / "decisions_taxonomy.json"
    tax.write_text(json.dumps(TAXONOMY), encoding="utf-8")
    monkeypatch.setattr(dtax, "TAXONOMY_FILE", tax)
    project_dir = tmp_path / "projects" / "book-a"
    decisions_dir(project_dir).mkdir(parents=True)
    (decisions_dir(project_dir) / "candidates.json").write_text(
        json.dumps({"candidates": CANDIDATES}), encoding="utf-8"
    )
    return project_dir


def test_merge_subjects_folds_chapters_and_keeps_the_common_spelling():
    merged = dcons.merge_subjects(CANDIDATES)
    names = merged["names"]
    assert [s["subject"] for s in names] == ["Thunder", "Old Bess"]
    assert names[0]["chapters"] == ["chapter_01", "chapter_02"]


def test_prepare_writes_one_job_per_category(project):
    out = dcons.consolidate_prepare(project)
    assert out["status"] == "ok", out
    assert out["by_category"] == {"names": 2, "address-pairs": 1}
    body = (decisions_dir(project) / "consolidate.names.body.txt").read_text(encoding="utf-8")
    assert body.startswith("CATEGORY: names")
    assert 'subject: "Thunder" — 2 chapter(s): 01, 02' in body
    assert "Translated Thunder" not in body  # the book's own example is held out


def test_prepare_needs_scan_candidates(tmp_path):
    out = dcons.consolidate_prepare(tmp_path / "nothing")
    assert out["status"] == "error"


def test_check_draft_audits_coverage():
    subjects = {"Thunder": ["chapter_01"], "Old Bess": ["chapter_01"], "Rex": ["chapter_02"]}
    checked = dcons.check_draft(
        {
            "rules": [{
                "question": "Translate descriptive names?",
                "options": [{"label": "translate", "effect": "e"}, {"label": "keep", "effect": "e"}],
                "recommendation": "translate",
                "examples": ["thunder", "Nobody"],
                "covers": ["thunder", "Old Bess"],
                "destination": "decisions_gate",
            }],
            "items": [{"subjects": ["Old Bess"], "question": "q", "destination": "somewhere"}],
            "dropped": [{"subject": "Ghost", "reason": "invented"}],
        },
        subjects,
    )
    rule = checked["rules"][0]
    assert rule["covers"] == ["Thunder", "Old Bess"]
    assert rule["examples"] == ["Thunder"]
    assert rule["recommendation"] == "translate"
    assert checked["items"][0]["destination"] == "glossary"
    assert checked["unaccounted"] == ["Rex"]
    assert checked["duplicated"] == ["Old Bess"]
    assert checked["unknown"] == ["Ghost"]


def test_commit_routes_and_reports(project):
    dcons.consolidate_prepare(project)
    ddir = decisions_dir(project)
    (ddir / "consolidate.names.draft.json").write_text(json.dumps({
        "category": "names",
        "rules": [{
            "question": "Translate names that are ordinary words?",
            "options": [{"label": "translate", "effect": "e"}],
            "recommendation": "translate",
            "why": "w",
            "examples": ["Thunder"],
            "covers": ["Thunder", "Old Bess"],
            "destination": "decisions_gate",
        }],
    }), encoding="utf-8")
    (ddir / "consolidate.address-pairs.draft.json").write_text(json.dumps({
        "category": "address-pairs",
        "items": [{"subjects": ["Hobbs -> Polly"], "question": "usted?", "destination": "address_map"}],
    }), encoding="utf-8")

    out = dcons.consolidate_commit(project)
    assert out["status"] == "ok", out
    assert out["counts"]["gate_questions"] == 1
    assert out["counts"]["address_map"] == 1
    assert out["coverage"] == {"unaccounted": 0, "unknown": 0, "duplicated": 0}
    report = (ddir / "consolidated.md").read_text(encoding="utf-8")
    assert "1. **Translate names that are ordinary words?**" in report
    assert "Thunder (2 ch.)" in report


def test_fanout_needs_a_manifest(project):
    assert "consolidate-prepare" in dcons.consolidate_fanout(project)["error"]
