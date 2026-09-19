"""Tests for the triage floor calibration in ``scripts/replay_triage.py``.

This script is the only thing that decides whether the triage pass may be
trusted, and the only place ``TRIAGE_CONFIDENCE_FLOOR`` comes from. Two ways it
could quietly report a floor is safe when it is not, both pinned here:

* **The join.** ``issue_key`` is unique only *within* a chunk — it hashes
  ``(eval_name, severity, message, location)`` and for these two checkers
  ``location`` is a character offset — so flattening a book into one map lets
  one chapter's verdict stand in for another's.
* **The recommendation.** With nothing scored, every floor trivially loses zero
  real defects, and the lowest of them is the largest suppression the pass can
  be asked to do.

The full corpus replay is not covered: it reads the real ``projects/`` tree and
is run by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay_triage import (  # noqa: E402
    _totals,
    marked_findings,
    recommended_floor,
    score_project,
    triage_verdicts,
)
from web_ui.evaluations import append_feedback, append_triage  # noqa: E402

CHUNK_A = "chapter_01_chunk_000"
CHUNK_B = "chapter_02_chunk_000"

# One key, two chunks. This is not contrived: the same unknown word at the same
# offset in two chapters hashes identically, which is exactly what the flattened
# join could not tell apart.
SHARED_KEY = "8f3b32300d4cf994"


@pytest.fixture
def project(tmp_path) -> Path:
    proj = tmp_path / "a-book"
    (proj / "evaluations").mkdir(parents=True)
    return proj


def _mark(proj: Path, chunk_id: str, label: str, *, key: str = SHARED_KEY) -> None:
    append_feedback(
        proj, chunk_id,
        eval_name="dictionary",
        issue_index=0,
        feedback_type=label,
        key=key,
    )


def _verdict(
    proj: Path, chunk_id: str, verdict: str, confidence: float,
    *, key: str = SHARED_KEY,
) -> None:
    append_triage(
        proj, chunk_id,
        eval_name="dictionary",
        issue_index=0,
        verdict=verdict,
        key=key,
        confidence=confidence,
        reason="a proper noun",
        model="grok-4.6",
    )


# --- the join ----------------------------------------------------------------

def test_one_key_in_two_chunks_is_two_findings(project: Path):
    """The veto is computed on this join, so a collision here loses a defect.

    Chunk A holds a real defect the filter would have suppressed at 0.99; chunk
    B holds a false positive it left alone. Flattened, B's row stood in for A's
    and the sweep reported zero real defects lost — recommending a floor on the
    strength of the one finding that proves it unsafe.
    """
    _mark(project, CHUNK_A, "resolved")
    _verdict(project, CHUNK_A, "suppress", 0.99)
    _mark(project, CHUNK_B, "false_positive")
    _verdict(project, CHUNK_B, "keep", 0.20)

    assert len(marked_findings(project)) == 2
    assert len(triage_verdicts(project)) == 2

    report = score_project(project)
    assert report["scored"] == 2
    assert report["per_floor"]["0.90"]["lost"] == 1, "the real defect is still seen"


def test_the_same_chunk_still_takes_the_last_mark(project: Path):
    """Chunk-keying must not cost the append-only file its re-marking."""
    _mark(project, CHUNK_A, "resolved")
    _mark(project, CHUNK_A, "false_positive")

    labels = marked_findings(project)
    assert len(labels) == 1
    assert next(iter(labels.values())) == "false_positive"


# --- the recommendation ------------------------------------------------------

def test_a_wave_nobody_has_marked_recommends_no_floor(project: Path):
    """The normal state right after a commit, and the one that read as safe.

    ``prepare`` skips every already-marked finding, so a fresh wave and the
    labelled corpus begin with zero overlap by design. Every floor then loses
    zero real defects for want of any real defect, and the old code answered
    "0.50 is safe" — the bottom of the table.
    """
    _verdict(project, CHUNK_A, "suppress", 0.99)

    total = _totals([score_project(project)])
    assert total["scored"] == 0
    assert recommended_floor(total) is None


def test_a_join_of_only_false_positives_cannot_test_the_veto(project: Path):
    """Benefit is measurable here; the veto is not, and it is the veto that
    decides. A floor recommended off this evidence would rest on nothing."""
    _mark(project, CHUNK_A, "false_positive")
    _verdict(project, CHUNK_A, "suppress", 0.99)

    total = _totals([score_project(project)])
    assert total["scored"] == 1
    assert total["scored_real"] == 0
    assert recommended_floor(total) is None


def test_a_real_defect_in_the_join_is_what_lets_a_floor_be_chosen(project: Path):
    """The guard must not refuse a sweep that does have the evidence.

    The verdict suppresses at 0.95, so every floor at or below that loses the
    defect and only 0.99 is clean.
    """
    _mark(project, CHUNK_A, "resolved")
    _verdict(project, CHUNK_A, "suppress", 0.95)

    total = _totals([score_project(project)])
    assert total["scored_real"] == 1
    assert recommended_floor(total) == "0.99"


def test_a_clean_sweep_recommends_the_lowest_clean_floor(project: Path):
    """A suppressed false positive and an untouched real defect: nothing is
    lost at any floor, so the lowest is the answer and the benefit is real."""
    _mark(project, CHUNK_A, "false_positive")
    _verdict(project, CHUNK_A, "suppress", 0.99)
    _mark(project, CHUNK_B, "resolved", key="other-key-0000000")
    _verdict(project, CHUNK_B, "keep", 0.90, key="other-key-0000000")

    total = _totals([score_project(project)])
    assert total["scored_real"] == 1
    assert recommended_floor(total) == "0.50"
