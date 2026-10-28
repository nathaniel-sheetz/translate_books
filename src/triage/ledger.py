"""Which chapters the triage pass has finished, and as of when.

``evaluations/_triage.jsonl`` cannot answer "has this chapter been triaged":
it only has rows for findings that needed a verdict, so a chapter whose
findings were all dismissed or ignored -- or that had none -- never appears in
it. ``state.json`` under ``.harness/triage/`` does: ``prepare`` stamps every
chapter it found nothing left to triage in, and ``commit`` stamps every chapter
whose findings all carry a verdict.

A stamp is the time the findings were *collected*, not when the verdicts landed,
so a checker run between ``prepare`` and ``commit`` still makes its chapter
owed. A reader compares the stamp with the chapter's newest ``evaluated_at``:
anything the checkers wrote after it has not been triaged.

Stamps from a chapter-scoped run say nothing about the chapters outside it, so
the ledger is ``complete`` -- an unstamped chapter is owed -- only once a
whole-book ``prepare`` over every triaged checker has written it. Until then a
book triaged before the ledger existed is still derived.

Kept apart from :mod:`src.triage.pass_` so the book page can read it without
importing the wave machinery.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

STATE_FILENAME = "state.json"


def state_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / ".harness" / "triage" / STATE_FILENAME


def load_state(project_dir: Path | str) -> dict[str, Any]:
    """``{"chapters": {chapter_id: stamp}, "complete": bool, "updated_at": ...}``,
    or ``{}``."""
    try:
        doc = json.loads(state_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def chapter_stamps(
    project_dir: Path | str, *, complete_only: bool = False
) -> Optional[dict[str, str]]:
    """``{chapter_id: stamp}``, or ``None`` when the book has no ledger yet --
    or, with ``complete_only``, none a whole-book run has made complete."""
    doc = load_state(project_dir)
    chapters = doc.get("chapters")
    if not isinstance(chapters, dict):
        return None
    if complete_only and doc.get("complete") is not True:
        return None
    return {ch: s for ch, s in chapters.items() if isinstance(s, str)}


def record_chapters(
    project_dir: Path | str,
    chapters: Iterable[str],
    at: str,
    *,
    complete: bool = False,
) -> int:
    """Stamp ``chapters`` as triaged as of ``at``. Returns how many stamps changed.

    A chapter already stamped later keeps its stamp: committing an older
    manifest must not move a chapter back in time. ``complete`` marks the
    ledger as covering the whole book; it is never unset. Nothing is written
    when nothing changes, so the book page's cache is not invalidated for
    nothing.
    """
    doc = load_state(project_dir)
    stamps = doc.get("chapters") if isinstance(doc.get("chapters"), dict) else {}
    changed = 0
    for ch in sorted(set(chapters)):
        previous = stamps.get(ch)
        if not isinstance(previous, str) or previous < at:
            stamps[ch] = at
            changed += 1
    was_complete = doc.get("complete") is True
    if not changed and (was_complete or not complete):
        return 0
    path = state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace, never truncate: a torn file loads as {} and would cost every stamp.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "chapters": stamps,
                "complete": was_complete or complete,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return changed
