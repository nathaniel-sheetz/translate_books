"""The read-only scanner's status line prints the floors that will apply."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
pending_work = importlib.import_module("pending_work")


def _payload(**automation):
    settings = {
        "default_cli": "claude",
        "exclude_groups": [],
        "auto_apply_types": ["word_choice", "flag"],
        "confidence_floor": "high",
        "confidence_floor_by_type": {},
    }
    settings.update(automation)
    return {
        "totals": {"books": 0, "books_with_work": 0, "pending": 0, "blocked": 0},
        "settings": settings,
        "books": [],
        "skipped_books": [],
    }


def test_status_line_prints_resolved_floors(capsys):
    pending_work._print_human(_payload(
        confidence_floor=" High ",
        confidence_floor_by_type={"word_choice": None, "footnote": "low"},
    ))
    out = capsys.readouterr().out
    assert "auto_apply=word_choice,flag@high word_choice@high" in out
    assert "footnote" not in out


def test_status_line_has_no_suffix_without_overrides(capsys):
    pending_work._print_human(_payload())
    line = next(l for l in capsys.readouterr().out.splitlines() if "auto_apply=" in l)
    assert line.rstrip().endswith("auto_apply=word_choice,flag@high")
