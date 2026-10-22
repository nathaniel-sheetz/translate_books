"""The review board: one book's recommendations as a page the operator picks from.

``recommendations.json`` is what the synthesis wave proposed. :func:`build` puts
everything the page shows beside it, from disk: the findings and coded signals
each recommendation cites, the glossary entry a patch edits, which ones were
already applied or went stale, and the dry run of what is picked right now.

The page writes one file, ``board.json``::

    {"selection": ["R-ab12cd", "R-ef34gh.2"],      # apply's --select tokens
     "notes": {"R-ab12cd": "but keep the old key too"},
     "revise": ["R-9f8e7d"],                       # rewrite these from their notes
     "saved_at": "...",
     "approved": {"selection": [...], "fingerprint": "...", "at": "..."} | null}

Saving a different selection drops the approval, so an approval always covers
the diffs the user last saw. ``apply --from-board --yes`` applies an approved
selection only while its fingerprint (``recommendations.json`` plus the three
artifacts' hashes) still matches; otherwise the diffs it would write are not
the ones that were approved.

A recommendation flagged ``revise`` waits for the agent: it reads the note,
writes the changes the user asked for, and stores them with
:func:`src.book_review.revise.revise`. Until then it cannot be picked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.book_review import review_dir
from src.book_review.apply import apply as apply_selection
from src.book_review.apply import parse_selection, stale_reasons
from src.book_review.evidence import _finding_full, artifact_hashes, load_artifacts
from src.book_review.patches import find_entry
from src.book_review.report import _metrics_line, describe_patch
from src.book_review.synthesize import recommendations_path

PRIORITIES = (1, 2, 3)


def board_path(project_dir: Path) -> Path:
    return review_dir(project_dir) / "board.json"


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load(project_dir: Path) -> dict[str, Any]:
    saved = _read_json(board_path(Path(project_dir)))
    if not isinstance(saved, dict):
        saved = {}
    return {
        "selection": [s for s in saved.get("selection") or [] if isinstance(s, str)],
        "notes": {k: v for k, v in (saved.get("notes") or {}).items()
                  if isinstance(k, str) and isinstance(v, str) and v.strip()},
        "revise": [r for r in saved.get("revise") or [] if isinstance(r, str)],
        "saved_at": saved.get("saved_at"),
        "approved": saved.get("approved") if isinstance(saved.get("approved"), dict) else None,
    }


def _write(project_dir: Path, board: dict[str, Any]) -> None:
    path = board_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


def _recommendations(project_dir: Path) -> Optional[dict[str, Any]]:
    doc = _read_json(recommendations_path(project_dir))
    return doc if isinstance(doc, dict) else None


def fingerprint(project_dir: Path, doc: dict[str, Any]) -> str:
    """Changes when the review is re-run, a recommendation is revised or an artifact edited."""
    digest = hashlib.sha256()
    digest.update(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(json.dumps(artifact_hashes(project_dir), sort_keys=True).encode())
    return digest.hexdigest()[:16]


def applied_record(project_dir: Path) -> dict[str, tuple[str, Optional[set[int]]]]:
    """Recommendation id -> ``(when, patch numbers)`` from ``applied.jsonl``.

    Patch numbers is ``None`` when a selection applied the whole recommendation
    (``R-id``, not ``R-id.2``). A later whole-recommendation apply stays ``None``.
    """
    out: dict[str, tuple[str, Optional[set[int]]]] = {}
    try:
        lines = (review_dir(project_dir) / "applied.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = row.get("ts") or ""
        for rid, numbers in parse_selection(row.get("selection") or []).items():
            previous = out.get(rid)
            if numbers is None or (previous is not None and previous[1] is None):
                out[rid] = (when, None)
            elif previous is None:
                out[rid] = (when, set(numbers))
            else:
                out[rid] = (when, previous[1] | set(numbers))
    return out


def applied_closed(patch_count: int, numbers: Optional[set[int]]) -> bool:
    """True when every patch of a recommendation has been applied."""
    if numbers is None:
        return True
    return patch_count > 0 and set(range(1, patch_count + 1)) <= numbers


def applied_ids(project_dir: Path) -> dict[str, str]:
    """Recommendation id -> when any of it was applied.

    A partial apply (``R-id.2``) is recorded here too. Callers that must block
    only the patches that ran use :func:`applied_record`.
    """
    return {rid: when for rid, (when, _numbers) in applied_record(project_dir).items()}


def check_selection(project_dir: Path, doc: dict[str, Any], selection: list[str],
                    revise: Optional[list[str]] = None) -> list[str]:
    """Why this selection cannot be saved; empty when it can."""
    by_id = {r["id"]: r for r in doc.get("recommendations") or []}
    try:
        chosen = parse_selection(selection)
    except ValueError:
        return [f"not a selection: {selection}"]
    problems = []
    done = applied_record(project_dir)
    docs, current = load_artifacts(project_dir), artifact_hashes(project_dir)
    waiting = set(load(project_dir)["revise"] if revise is None else revise)
    for rid, numbers in chosen.items():
        rec = by_id.get(rid)
        if rec is None:
            problems.append(f"{rid}: no such recommendation")
            continue
        stale = stale_reasons(rec, docs, current)
        record = done.get(rid)
        blocked = False
        if record is not None:
            _when, applied = record
            if applied is None:
                problems.append(f"{rid}: already applied")
                blocked = True
            else:
                asked = set(range(1, len(rec["patches"]) + 1)) if numbers is None else numbers
                overlap = sorted(asked & applied)
                if overlap:
                    problems.append(f"{rid}: changes {overlap} already applied")
                    blocked = True
        if not blocked and rid in waiting:
            problems.append(f"{rid}: waiting for a revision from your note")
        elif not blocked and stale:
            problems.append(f"{rid}: {'; '.join(stale)}")
        bad = sorted(n for n in numbers or () if not 1 <= n <= len(rec["patches"]))
        if bad:
            problems.append(f"{rid}: no change numbered {bad}")
    for rid in chosen:
        for other in (by_id.get(rid) or {}).get("conflicts_with") or []:
            if other in chosen and rid < other:
                problems.append(f"{rid} and {other} change the same thing: pick one")
    return problems


def save(project_dir: Path, selection: list[str], notes: Optional[dict[str, str]] = None,
         revise: Optional[list[str]] = None) -> dict[str, Any]:
    """Store what the page has picked; an approval survives only an identical selection.

    ``revise`` names recommendations to rewrite from their notes; one without a
    note, or already applied, is dropped from it.
    """
    project_dir = Path(project_dir)
    doc = _recommendations(project_dir)
    if doc is None:
        return {"status": "error", "problems": ["no recommendations.json; run the review first"]}
    board = load(project_dir)
    known = {r["id"] for r in doc.get("recommendations") or []}
    if notes is not None:
        board["notes"] = {k: v.strip() for k, v in notes.items()
                          if k in known and isinstance(v, str) and v.strip()}
    if revise is not None:
        done = applied_ids(project_dir)
        board["revise"] = sorted({r for r in revise if isinstance(r, str) and r in known
                                  and r in board["notes"] and r not in done})
    selection = [s for s in selection if isinstance(s, str) and s.strip()]
    problems = check_selection(project_dir, doc, selection, board["revise"])
    if problems:
        return {"status": "error", "problems": problems}
    if board["approved"] and board["approved"].get("selection") != selection:
        board["approved"] = None
    board["selection"] = selection
    board["saved_at"] = datetime.now().isoformat(timespec="seconds")
    _write(project_dir, board)
    return {"status": "ok"}


def approve(project_dir: Path, selection: list[str]) -> dict[str, Any]:
    """Approve the saved selection, as long as it is the one the page shows."""
    project_dir = Path(project_dir)
    doc = _recommendations(project_dir)
    board = load(project_dir)
    if doc is None:
        return {"status": "error", "problems": ["no recommendations.json"]}
    if not board["selection"]:
        return {"status": "error", "problems": ["nothing is picked"]}
    if selection != board["selection"]:
        return {"status": "error", "problems": ["the page and the saved picks differ: reload"]}
    preview = apply_selection(project_dir, board["selection"], yes=False)
    if preview.get("status") != "dry_run":
        return {"status": "error", "problems": [preview.get("error") or "the dry run failed"]}
    board["approved"] = {"selection": list(board["selection"]),
                         "fingerprint": fingerprint(project_dir, doc),
                         "at": datetime.now().isoformat(timespec="seconds")}
    _write(project_dir, board)
    return {"status": "ok"}


def unapprove(project_dir: Path) -> dict[str, Any]:
    board = load(project_dir)
    board["approved"] = None
    _write(Path(project_dir), board)
    return {"status": "ok"}


def approved_selection(project_dir: Path) -> tuple[Optional[list[str]], Optional[str]]:
    """The selection ``apply --from-board --yes`` may write, or why there is none."""
    project_dir = Path(project_dir)
    doc = _recommendations(project_dir)
    board = load(project_dir)
    approved = board["approved"]
    if doc is None:
        return None, "no recommendations.json"
    if not approved:
        if board["selection"]:
            return None, "the picks on the board are not approved yet"
        return None, "nothing is picked on the board"
    if approved.get("selection") != board["selection"]:
        return None, "the picks changed after approval: approve them again"
    if approved.get("fingerprint") != fingerprint(project_dir, doc):
        return None, ("the glossary, style guide or address map changed after approval, or the "
                      "review was re-run: the diffs approved are not the ones that would be "
                      "written; approve again")
    return list(approved["selection"]), None


def clear_after_apply(project_dir: Path) -> None:
    """The picks are applied: start the board empty, keeping the notes."""
    board = load(project_dir)
    board.update(selection=[], approved=None, saved_at=datetime.now().isoformat(timespec="seconds"))
    _write(Path(project_dir), board)


# --- the page's state ---------------------------------------------------------

def _finding_view(fid: str, findings: dict[str, Any], corpus) -> dict[str, Any]:
    f = findings.get(fid)
    if f is None:
        return {"kind": "finding", "id": fid, "missing": True}
    return {"kind": "finding", **_finding_full(f, corpus)}


# The page shows these patches' text in full beside the change, so their line
# says only what is edited instead of quoting it clipped.
_SHORT = {
    "style.replace_text": "style guide: rewrite a passage",
    "style.append": "style guide: add a passage",
    "address.replace_global_rules_text": "address map global rules: rewrite a passage",
}


def _patch_view(n: int, patch: dict[str, Any], corpus, findings: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": n, "type": patch["type"],
                           "summary": _SHORT.get(patch["type"]) or describe_patch(patch),
                           "patch": patch}
    if patch["type"].startswith("glossary.") and patch.get("english"):
        current = find_entry(corpus.glossary, patch["english"])
        if current:
            out["current"] = current
    if patch["type"] == "judge.dismiss":
        out["findings"] = [_finding_view(fid, findings, corpus) for fid in patch["finding_ids"]]
    return out


def _evidence_view(eid: str, signals: dict[str, Any], findings: dict[str, Any], corpus) -> dict[str, Any]:
    signal = signals.get(eid)
    if signal is not None:
        return {"kind": "signal", "id": eid, "detector": signal["detector"],
                "title": signal["title"], "summary": _metrics_line(signal),
                "chapters": signal.get("chapters") or [],
                "findings": len(signal.get("finding_ids") or [])}
    if eid in findings:
        return _finding_view(eid, findings, corpus)
    return {"kind": "unknown", "id": eid}


def _applied_view(rec: dict[str, Any], done: dict[str, tuple[str, Optional[set[int]]]]) -> tuple[Optional[str], list[int]]:
    """``(applied_at or None, patch numbers already applied)``.

    ``applied_at`` is set only once every patch has run, so a partial apply
    leaves the remaining patches pickable.
    """
    record = done.get(rec["id"])
    if record is None:
        return None, []
    when, numbers = record
    count = len(rec.get("patches") or [])
    patches = list(range(1, count + 1)) if numbers is None else sorted(numbers)
    if applied_closed(count, numbers):
        return when, patches
    return None, patches


def build(project_dir: Path) -> dict[str, Any]:
    """Everything the page draws, rebuilt from disk on every request."""
    from src.book_review.corpus import load_book

    project_dir = Path(project_dir)
    doc = _recommendations(project_dir)
    board = load(project_dir)
    if doc is None:
        return {"status": "empty", "project": project_dir.name, "recommendations": [],
                "selection": [], "notes": {}, "approved": None, "preview": None}

    corpus = load_book(project_dir)
    findings = {f.id: f for f in corpus.findings}
    signals_doc = _read_json(review_dir(project_dir) / "signals.json") or {}
    signals = {s["id"]: s for s in signals_doc.get("signals") or []}
    docs, current = load_artifacts(project_dir), artifact_hashes(project_dir)
    done = applied_record(project_dir)

    recs = []
    for rec in doc.get("recommendations") or []:
        revision = rec.get("revision")
        if revision:
            original = revision.get("original") or {}
            revision = {**{k: revision.get(k) for k in ("n", "at", "why", "note")},
                        "original": [_patch_view(n, p, corpus, findings)
                                     for n, p in enumerate(original.get("patches") or [], 1)],
                        "original_rationale": original.get("rationale")}
        applied_at, applied_patches = _applied_view(rec, done)
        recs.append({
            **{k: rec.get(k) for k in ("id", "theme", "title", "rationale", "confidence",
                                       "priority", "problems")},
            "conflicts_with": rec.get("conflicts_with") or [],
            "applied_at": applied_at,
            "applied_patches": applied_patches,
            "stale": [] if applied_at else stale_reasons(rec, docs, current),
            "revision": revision,
            "revise_requested": rec["id"] in board["revise"],
            "patches": [_patch_view(n, p, corpus, findings)
                        for n, p in enumerate(rec["patches"], 1)],
            "follow_ups": [{"type": p["type"], "summary": describe_patch(p), "patch": p}
                           for p in rec.get("follow_ups") or []],
            "evidence": [_evidence_view(e, signals, findings, corpus)
                         for e in rec.get("evidence") or []],
        })

    preview = None
    if board["selection"]:
        problems = check_selection(project_dir, doc, board["selection"])
        if problems:
            preview = {"status": "error", "error": "; ".join(problems)}
        else:
            preview = apply_selection(project_dir, board["selection"], yes=False)
            preview.pop("instructions", None)

    approved = board["approved"]
    approval_current = bool(
        approved and approved.get("selection") == board["selection"]
        and approved.get("fingerprint") == fingerprint(project_dir, doc)
    )
    cov = doc.get("coverage") or {}
    return {
        "status": "ok",
        "project": project_dir.name,
        "committed_at": doc.get("committed_at"),
        "worker_model": doc.get("worker_model"),
        "cli": doc.get("cli"),
        "prompt_version": doc.get("prompt_version"),
        "owed": {judge: len(v.get("owed") or []) for judge, v in (cov.get("judges") or {}).items()
                 if v.get("owed")},
        "recommendations": recs,
        "no_action": doc.get("no_action") or [],
        "rejected": [{"title": r.get("title"), "problems": r.get("problems") or []}
                     for r in doc.get("rejected") or []],
        "omitted": doc.get("omitted") or [],
        "selection": board["selection"],
        "notes": board["notes"],
        "revise": board["revise"],
        "approved": approved if approval_current else None,
        "preview": preview,
    }


def summary(project_dir: Path) -> dict[str, Any]:
    """What the agent needs from the board: the picks, the notes, the approval."""
    project_dir = Path(project_dir)
    doc = _recommendations(project_dir)
    board = load(project_dir)
    selection, problem = approved_selection(project_dir)
    recs = (doc or {}).get("recommendations") or []
    done = applied_record(project_dir)
    applied = sorted(
        r["id"] for r in recs
        if done.get(r["id"]) and applied_closed(len(r.get("patches") or []), done[r["id"]][1])
    )
    return {
        "recommendations": len(recs),
        "applied": applied,
        "open": [r["id"] for r in recs if r["id"] not in applied],
        "selection": board["selection"],
        "notes": board["notes"],
        "revise": {rid: board["notes"].get(rid, "") for rid in board["revise"]},
        "approved": selection is not None,
        "approved_selection": selection,
        "not_approved_because": problem,
    }
