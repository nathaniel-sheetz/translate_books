"""Replay every archived judge fix through the boundary-restatement measure.

A standing net over real book data: for each ``judge:`` record in every
``projects/*/corrections_applied.jsonl``, reconstruct the text the edit was
applied to and assert the edit did not duplicate the prose around it.

This is the audit the 2026-07-27 friction log asked for, as a test rather than
new CLI surface. ``projects/`` is gitignored, so it skips outside a machine that
has the books — it is a local regression net, not a CI gate.

``corrections_applied.jsonl`` is an append-only audit log, so the five known
corruptions stay in it forever even though their damage was reverted from
backups (four books) and hand-repaired (one). They are pinned in
:data:`KNOWN_HISTORICAL` and the test asserts the offender set is *exactly*
that — a sixth, or one of these five going missing, both fail.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.judges.fixes import ProposedFix, classify_fix, restated_context

PROJECTS = Path(__file__).resolve().parent.parent / "projects"

pytestmark = pytest.mark.skipif(
    not PROJECTS.is_dir(), reason="projects/ is gitignored — local-machine audit only"
)

# The corrupting applies that motivated the guard, keyed
# (project, chunk_id, rule, applied_at). All were reverted or hand-repaired in
# the books; only the archive records remain. Nothing may be added here without
# a matching repair.
KNOWN_HISTORICAL = {
    ("the-house-on-the-cliff", "chapter_03_chunk_000", "guillemets-for-thoughts", "2026-07-27T19:38:36"),
    ("the-house-on-the-cliff", "chapter_05_chunk_000", "inciso-punctuation", "2026-07-27T19:38:36"),
    ("the-house-on-the-cliff", "chapter_18_chunk_000", "same-speaker-continuation", "2026-07-27T19:38:36"),
    ("the-house-on-the-cliff", "chapter_23_chunk_000", "narration-separation", "2026-07-27T19:38:36"),
    ("wonder-book-of-horses", "chapter_02_chunk_000", "inciso-punctuation", "2026-07-01T17:36:10"),
}

# Applied fixes that left an unpaired closing raya in the book — found by the
# raya-balance guard when it was added (2026-07-30) by replaying this archive.
# Both are the shape the 2026-07-29 friction log (item 6) caught in pollyanna
# *before* applying: a second action inciso written with a closing raya only,
# where prompts/dialogue.txt requires an action inciso to be framed on both sides.
#
#   —¡De vuelta en casa! —exclamó Frank con una sonrisa—. Luego se volvió,
#   inquieto, hacia su tía—. ¿Hay ya noticias de papá?
#
# Unlike KNOWN_HISTORICAL these are **still in the book**: the prose needs the
# missing opening raya (or the sentence split), after which these entries go away.
# Nothing may be added here without a friction-log or TODO entry to repair it.
KNOWN_UNBALANCED_RAYA = {
    ("the-missing-chums", "chapter_09_chunk_000", "inciso-punctuation", "2026-07-28T08:29:30"),
    ("the-missing-chums", "chapter_23_chunk_000", "inciso-punctuation", "2026-07-28T08:29:30"),
}


def _snapshot_time(path: Path) -> datetime | None:
    """Parse the ``.chunk_edits`` backup convention: ``<YYYYmmdd>T<HHMMSS>.json``."""
    try:
        return datetime.strptime(path.stem, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _pre_edit_text(project_dir: Path, record: dict) -> str | None:
    """Best reconstruction of the ``translated_text`` a record was applied to.

    Newest ``.chunk_edits`` snapshot taken at or before ``applied_at`` in which
    ``original_es`` occurs exactly once (the snapshot the applier itself wrote
    just before splicing); falls back to the live chunk. ``None`` when no
    candidate locates the excerpt uniquely — there is nothing to measure then.
    """
    chunk_id = record.get("chunk_id") or ""
    chapter_id = record.get("chapter_id") or chunk_id.rsplit("_chunk_", 1)[0]
    original = record.get("original_es") or ""
    stamp = record.get("applied_at") or record.get("timestamp") or ""
    try:
        applied_at = datetime.fromisoformat(stamp)
    except ValueError:
        applied_at = None

    candidates: list[tuple[datetime, Path]] = []
    snapshot_dir = project_dir / ".chunk_edits" / chapter_id / chunk_id
    if snapshot_dir.is_dir():
        for snapshot in snapshot_dir.glob("*.json"):
            taken = _snapshot_time(snapshot)
            if taken is not None and (applied_at is None or taken <= applied_at):
                candidates.append((taken, snapshot))

    paths = [p for _t, p in sorted(candidates, reverse=True)]
    paths.append(project_dir / "chunks" / f"{chunk_id}.json")
    for path in paths:
        if not path.exists():
            continue
        try:
            text = json.loads(path.read_text(encoding="utf-8")).get("translated_text") or ""
        except (OSError, json.JSONDecodeError):
            continue
        if text.count(original) == 1:
            return text
    return None


def _judge_records() -> list[tuple[Path, int, dict]]:
    out: list[tuple[Path, int, dict]] = []
    for archive in sorted(PROJECTS.glob("*/corrections_applied.jsonl")):
        for i, line in enumerate(archive.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(record.get("source") or "").startswith("judge:"):
                continue
            if record.get("original_es") and record.get("corrected_es"):
                out.append((archive.parent, i, record))
    return out


def test_no_archived_judge_fix_duplicated_adjacent_prose():
    records = _judge_records()
    if not records:
        pytest.skip("no judge-sourced corrections archived on this machine")

    replayed = 0
    offenders: dict[tuple, str] = {}
    for project_dir, index, record in records:
        text = _pre_edit_text(project_dir, record)
        if text is None:
            continue
        replayed += 1
        original = record["original_es"]
        start = text.find(original)
        repeated = restated_context(
            text, start, start + len(original), record["corrected_es"], baseline=original,
        )
        if repeated:
            key = (
                project_dir.name, record.get("chunk_id"),
                record.get("rule"), record.get("applied_at"),
            )
            offenders[key] = f"{project_dir.name} #{index} {key[1]} (rule={key[2]}): repeats {repeated!r}"

    assert replayed, "no archived judge fix could be replayed — reconstruction is broken"

    new_offenders = [msg for key, msg in offenders.items() if key not in KNOWN_HISTORICAL]
    assert not new_offenders, (
        "judge fixes duplicated prose in the book:\n" + "\n".join(new_offenders)
    )
    # The known five must still be detectable, or the measure has gone blind.
    missing = KNOWN_HISTORICAL - set(offenders)
    assert not missing, f"known corrupting applies no longer detected: {sorted(missing)}"


def test_every_archived_judge_fix_would_still_be_offered():
    """The false-positive net for the whole classifier, not just one measure.

    Each archived fix landed in a book, so any guard that would now withhold it is
    costing throughput on correct work — unless it is one of the five corruptions,
    which every guard is welcome to catch. This is what calibrated the length
    thresholds (legitimate fixes grow by at most 13 characters here; the
    corruptions by 25 to 89) and what will catch the next guard that overreaches.
    """
    records = _judge_records()
    if not records:
        pytest.skip("no judge-sourced corrections archived on this machine")

    replayed = 0
    withheld: dict[tuple, str] = {}
    for project_dir, index, record in records:
        text = _pre_edit_text(project_dir, record)
        if text is None:
            continue
        replayed += 1
        rule = record.get("rule") or "other"
        result = classify_fix(
            {
                "severity": record.get("severity") or "warning",
                "message": f"[{rule}] {record.get('message') or ''}",
                "location": record["original_es"],
                "suggestion": record["corrected_es"],
            },
            text,
        )
        if isinstance(result, ProposedFix):
            continue
        key = (
            project_dir.name, record.get("chunk_id"),
            record.get("rule"), record.get("applied_at"),
        )
        withheld[key] = (
            f"{project_dir.name} #{index} {key[1]} (rule={key[2]}): {result.reason}, "
            f"{len(record['original_es'])} -> {len(record['corrected_es'])} chars"
        )

    assert replayed, "no archived judge fix could be replayed — reconstruction is broken"

    expected = KNOWN_HISTORICAL | KNOWN_UNBALANCED_RAYA
    regressions = [msg for key, msg in withheld.items() if key not in expected]
    assert not regressions, (
        "a guard now withholds archived fixes that landed correctly:\n" + "\n".join(regressions)
    )
    # The two live raya defects must stay visible: if the prose is repaired, the
    # replay stops flagging them and this pin comes out with the same commit.
    missing = KNOWN_UNBALANCED_RAYA - set(withheld)
    assert not missing, (
        "these applied fixes no longer read as unbalanced — if the prose was "
        f"repaired, drop them from KNOWN_UNBALANCED_RAYA: {sorted(missing)}"
    )
