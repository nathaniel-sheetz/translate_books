"""Rewrite one recommendation's changes the way the user's note asked.

The user leaves a note on a card ("simpler wording", "allow the short form")
and flags it for revision on the review page. The agent reads the note, writes
the changes it asks for, and :func:`revise` stores them in place of the
recommendation's own: validated exactly as ``commit`` validates the synthesis
model's proposals, stamped against the book as it is now, with the first
version kept beside it so the page can show what changed. The user then picks
and approves the revision on the page like any other recommendation.

Nothing in the book changes here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.book_review import board as review_board
from src.book_review.corpus import load_book
from src.book_review.evidence import load_artifacts
from src.book_review.patches import stamp
from src.book_review.schema import check_recommendation, reconcile
from src.book_review.synthesize import recommendations_path


def _error(*problems: str) -> dict[str, Any]:
    return {"status": "error", "problems": list(problems)}


def revise(project_dir: Path, rid: str, change: dict[str, Any]) -> dict[str, Any]:
    """Replace ``rid``'s patches with ``change``'s.

    ``change``: ``{"patches": [...], "follow_ups": [...], "rationale": "...",
    "why": "what the revision changes"}``. ``patches`` and ``why`` are required;
    ``follow_ups`` and ``rationale`` default to the recommendation's own. Any
    patch that fails validation refuses the whole revision.
    """
    project_dir = Path(project_dir)
    path = recommendations_path(project_dir)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _error("no recommendations.json; run the review first")
    recs = doc.get("recommendations") or []
    rec = next((r for r in recs if r["id"] == rid), None)
    if rec is None:
        return _error(f"{rid}: no such recommendation")
    if rid in review_board.applied_ids(project_dir):
        return _error(f"{rid}: already applied")
    if not isinstance(change, dict) or not isinstance(change.get("patches"), list):
        return _error("the revision needs a patches list")
    why = str(change.get("why") or "").strip()
    if not why:
        return _error("the revision needs why: one line on what it changes")

    rationale = str(change.get("rationale") or rec.get("rationale") or "")
    follow_ups = change.get("follow_ups", rec.get("follow_ups") or [])
    corpus = load_book(project_dir)
    # Evidence stays the recommendation's own; only the changes are checked.
    clean, problems = check_recommendation(
        {"title": rec["title"], "rationale": rationale, "confidence": rec.get("confidence"),
         "priority": rec.get("priority"), "patches": change["patches"], "follow_ups": follow_ups},
        rec["theme"], corpus, set(),
    )
    if clean is None or problems:
        return _error(*(problems or ["no valid patch or follow-up"]))
    try:
        targets = stamp(load_artifacts(project_dir), clean["patches"])
    except ValueError as exc:
        return _error(str(exc))

    board = review_board.load(project_dir)
    previous = rec.get("revision") or {}
    rec["revision"] = {
        "n": int(previous.get("n") or 0) + 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "why": why,
        "note": board["notes"].get(rid),
        "original": previous.get("original") or {
            "rationale": rec.get("rationale"), "patches": rec["patches"],
            "follow_ups": rec.get("follow_ups") or [], "targets": rec.get("targets"),
            "precondition": rec.get("precondition"),
        },
    }
    rec.update(rationale=clean["rationale"], patches=clean["patches"],
               follow_ups=clean["follow_ups"], targets=targets, problems=[])
    rec.pop("precondition", None)
    reconcile(recs)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # The request is answered; picks of the old version no longer mean anything.
    board["revise"] = [r for r in board["revise"] if r != rid]
    board["selection"] = [t for t in board["selection"] if t.split(".")[0] != rid]
    board["approved"] = None
    review_board._write(project_dir, board)
    return {"status": "ok", "id": rid, "revision": rec["revision"]["n"],
            "patches": len(clean["patches"]), "follow_ups": len(clean["follow_ups"])}
