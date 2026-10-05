"""
The image ledger: what was replaced with what, and when it was put back.

``projects/`` is gitignored, so there is no ``git log`` to answer "is this the
publisher's engraving or one we generated?". ``.harness/images/images.jsonl`` is
that answer. One row per ``apply`` / ``revert`` / recorded skip, append-only for
the reason ``src/footnote_pass/ledger.py`` gives: nothing reads it as a plan, so
a later row at the same image simply supersedes the earlier one and the history
stays readable.

Each row is a snapshot, not a reference — it embeds the job's mode, instruction
and label map and the hashes on both sides — because the job directory it came
from can be re-prepared or deleted without the ledger noticing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.image_pass import work_dir

ACTION_APPLY = "apply"
ACTION_REVERT = "revert"
ACTION_SKIP = "skip"
ACTION_REDO = "redo"
ACTION_BACKFILL = "backfill"

# Actions that decide whether an image is currently a replacement. A backfill
# is not one: it swaps the publisher's small file for the publisher's large
# one, so the picture is as original afterwards as it was before.
_STATE_ACTIONS = (ACTION_APPLY, ACTION_REVERT)

# Where a backfill put the larger scan: straight into ``images/``, or into the
# backup an earlier ``apply`` made (the original of a replaced image).
BASELINE_IMAGES = "images"
BASELINE_BACKUP = "images_original"

STATUS_REPLACED = "replaced"
STATUS_REVERTED = "reverted"


def ledger_path(project_dir: Path) -> Path:
    return work_dir(project_dir) / "images.jsonl"


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def append_row(project_dir: Path, row: dict[str, Any]) -> Path:
    """Append one row and return the ledger path. Never rewrites a line."""
    path = ledger_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_rows(project_dir: Path) -> list[dict[str, Any]]:
    """Every parseable row, in file order. A torn last line is skipped."""
    path = ledger_path(project_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def current_state(project_dir: Path) -> dict[str, dict[str, Any]]:
    """The latest ``apply`` / ``revert`` row per image key."""
    state: dict[str, dict[str, Any]] = {}
    for row in read_rows(project_dir):
        if row.get("action") in _STATE_ACTIONS and row.get("image"):
            state[str(row["image"])] = row
    return state


def expected_originals(project_dir: Path) -> dict[str, str]:
    """``{image key: sha256 the backup in images_original/ should have}``.

    ``apply`` records the original it backed up; a later ``backfill`` replaces
    that backup with the larger scan and records the new hash. Whichever came
    last is what ``verify`` must find there.
    """
    expected: dict[str, str] = {}
    for row in read_rows(project_dir):
        key = row.get("image")
        if not key:
            continue
        if row.get("action") == ACTION_APPLY and row.get("sha256_original"):
            expected[str(key)] = row["sha256_original"]
        elif (
            row.get("action") == ACTION_BACKFILL
            and row.get("baseline") == BASELINE_BACKUP
            and row.get("sha256_after")
        ):
            expected[str(key)] = row["sha256_after"]
    return expected


def status_of(row: Optional[dict[str, Any]]) -> Optional[str]:
    """``replaced`` | ``reverted`` | ``None`` (never touched)."""
    if not row:
        return None
    return STATUS_REPLACED if row.get("action") == ACTION_APPLY else STATUS_REVERTED
