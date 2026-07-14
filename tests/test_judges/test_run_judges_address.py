"""Tests for the address-map wiring in the run_judges CLI context builder."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

run_judges = importlib.import_module("scripts.run_judges")


def _write_map(project_dir: Path) -> None:
    (project_dir / "address_map.json").write_text(
        json.dumps({
            "content": "Betsy→Frances usted; Frances→Betsy tú.",
            "pairs": [], "global_rules": "tú in family.",
        }),
        encoding="utf-8",
    )


def test_missing_map_errors_for_address_judge(tmp_path: Path):
    ctx, err = run_judges._build_judge_context(tmp_path, ["address"], None, None)
    assert err is not None
    assert "address map" in err.lower()
    assert "address_map" not in ctx


def test_missing_map_ok_for_other_judges(tmp_path: Path):
    ctx, err = run_judges._build_judge_context(tmp_path, ["dialogue"], None, None)
    assert err is None
    assert "address_map" not in ctx


def test_present_map_loaded_into_context(tmp_path: Path):
    _write_map(tmp_path)
    ctx, err = run_judges._build_judge_context(tmp_path, ["address"], None, None)
    assert err is None
    assert "Betsy" in ctx["address_map"]


def test_present_map_empty_content_falls_back_to_global_rules(tmp_path: Path):
    (tmp_path / "address_map.json").write_text(
        json.dumps({"content": "", "pairs": [], "global_rules": "tú in family."}),
        encoding="utf-8",
    )
    ctx, err = run_judges._build_judge_context(tmp_path, ["address"], None, None)
    assert err is None
    assert ctx["address_map"] == "tú in family."


def test_empty_map_errors_for_address_judge(tmp_path: Path):
    (tmp_path / "address_map.json").write_text(
        json.dumps({"content": "  ", "pairs": [], "global_rules": ""}),
        encoding="utf-8",
    )
    ctx, err = run_judges._build_judge_context(tmp_path, ["address"], None, None)
    assert err is not None
    assert "empty" in err.lower()
    assert "address_map" not in ctx


def test_empty_map_ok_for_other_judges(tmp_path: Path):
    (tmp_path / "address_map.json").write_text(
        json.dumps({"content": "", "pairs": [], "global_rules": ""}),
        encoding="utf-8",
    )
    ctx, err = run_judges._build_judge_context(tmp_path, ["dialogue"], None, None)
    assert err is None
    assert "address_map" not in ctx
