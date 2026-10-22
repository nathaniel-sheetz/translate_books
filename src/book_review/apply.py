"""Apply the recommendations the operator picked, through the normal save paths.

Before anything is written, the selection goes through the board's
:func:`~src.book_review.board.check_selection`: no recommendation already
applied, no change number out of range, no two conflicting picks, and nothing
stale (:func:`stale_reasons`) -- each patch must still apply, and a glossary
entry or address pair it edits by name must be as it was when the review was
committed. A selection with any problem is refused whole. The patches are then
applied in order, and every edited artifact is validated before any is saved.

What each patch does:

- glossary / style / address-map patches edit the artifact in memory and save it
  through ``save_glossary`` / ``save_style_guide`` / ``save_address_map``, so each
  change lands in ``decisions_history.jsonl`` with ``writer="book_review"`` and the
  recommendation id as the reason;
- ``judge.dismiss`` appends one ``_feedback.jsonl`` mark per finding;
- ``rejudge`` flags the chapters' evaluations stale, so the next judge sweep
  re-runs them;
- ``house.rule_proposal`` records a house proposal (adopting it stays a separate
  decision);
- ``retranslate`` and ``manual`` are never executed: they are returned for the
  operator, ``retranslate`` with the preview command to run.

Without ``yes`` nothing is written: the result shows each artifact's diff.
"""

from __future__ import annotations

import copy
import difflib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.book_review import review_dir
from src.book_review.evidence import ARTIFACT_FILES as _FILES
from src.book_review.evidence import load_artifacts
from src.book_review.patches import apply_patch, artifact_of, stale_changes
from src.book_review.synthesize import recommendations_path

WRITER = "book_review"


def stale_reasons(rec: dict[str, Any], docs: dict[str, Optional[dict]],
                  hashes: dict[str, Optional[str]]) -> list[str]:
    """Why ``rec`` no longer fits the book as it is now; empty when it still does.

    A recommendation committed before per-patch stamps (no ``targets``) carries
    whole-file hashes instead, and goes stale on any edit to a file it touches.
    """
    if "targets" in rec:
        return stale_changes(docs, rec["patches"], rec["targets"])
    return [f"{_FILES[name]} changed since the review"
            for name, stamped in (rec.get("precondition") or {}).items()
            if stamped != hashes.get(name)]


def parse_selection(raw: list[str]) -> dict[str, Optional[set[int]]]:
    """``["R-ab12", "R-cd34.2"]`` -> ``{"R-ab12": None, "R-cd34": {2}}`` (None = every patch)."""
    out: dict[str, Optional[set[int]]] = {}
    for item in raw:
        for token in str(item).split(","):
            token = token.strip()
            if not token:
                continue
            rid, _, num = token.partition(".")
            if num:
                current = out.get(rid, set())
                if current is not None:
                    current.add(int(num))
                    out[rid] = current
            else:
                out[rid] = None
    return out


def _diff(name: str, before: Optional[dict], after: dict) -> str:
    if name == "style":
        a, b = (before or {}).get("content") or "", after.get("content") or ""
    else:
        a = json.dumps(before or {}, ensure_ascii=False, indent=2)
        b = json.dumps(after, ensure_ascii=False, indent=2)
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile=f"{_FILES[name]} (now)", tofile=f"{_FILES[name]} (after)", n=1,
    ))


def _validate(name: str, doc: dict) -> Any:
    """The artifact as its model, stamped as edited now; raises on an invalid edit."""
    from src.models import AddressMap, Glossary, StyleGuide

    model = {"glossary": Glossary, "style": StyleGuide, "address_map": AddressMap}[name]
    validated = model.model_validate(doc)
    validated.updated_at = datetime.now()
    return validated


def _save(project_dir: Path, name: str, validated: Any, reason: str) -> None:
    from src.utils.file_io import save_address_map, save_glossary, save_style_guide

    save = {"glossary": save_glossary, "style": save_style_guide,
            "address_map": save_address_map}[name]
    save(validated, project_dir / _FILES[name], writer=WRITER, reason=reason)


def apply(project_dir: Path, selection: list[str], *, yes: bool = False) -> dict[str, Any]:
    project_dir = Path(project_dir)
    try:
        doc = json.loads(recommendations_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "no recommendations.json; run `commit` first"}
    from src.book_review.board import check_selection

    by_id = {r["id"]: r for r in doc.get("recommendations") or []}
    problems = check_selection(project_dir, doc, selection)
    if problems:
        return {"status": "error", "error": "; ".join(problems), "problems": problems}
    chosen = parse_selection(selection)
    if not chosen:
        return {"status": "error", "error": "nothing is selected"}

    loaded = load_artifacts(project_dir)

    working = copy.deepcopy(loaded)
    touched: dict[str, list[str]] = {}
    actions: list[dict[str, Any]] = []
    for rid, numbers in chosen.items():
        rec = by_id[rid]
        for n, patch in enumerate(rec["patches"], 1):
            if numbers is not None and n not in numbers:
                continue
            kind = patch["type"]
            name = artifact_of(kind)
            try:
                if name:
                    apply_patch(working, patch)
                    touched.setdefault(name, []).append(f"{rid}.{n}")
                    actions.append({"id": f"{rid}.{n}", "type": kind, "status": "staged"})
                else:
                    actions.append({"id": f"{rid}.{n}", "type": kind, "status": "staged",
                                    "patch": patch})
            except ValueError as exc:
                return {"status": "error", "error": f"{rid}.{n}: {exc}"}
        for item in rec.get("follow_ups") or []:
            actions.append({"id": f"{rid}.follow_up", "type": item["type"], "status": "offer",
                            "patch": item})

    diffs = {name: _diff(name, loaded.get(name), working[name]) for name in touched}
    if not yes:
        return {"status": "dry_run", "diffs": diffs, "actions": actions,
                "instructions": "Show the diffs; on approval re-run with --yes."}

    # Validate every artifact before saving any, so a bad edit leaves the book untouched.
    try:
        validated = {name: _validate(name, working[name]) for name in touched}
    except ValueError as exc:
        return {"status": "error", "error": f"the edited artifacts do not validate: {exc}"}
    for name, ids in touched.items():
        _save(project_dir, name, validated[name], reason=", ".join(sorted({i.split('.')[0] for i in ids})))
    results = [_side_effect(project_dir, a) for a in actions]
    log = review_dir(project_dir) / "applied.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "selection": selection, "saved": sorted(touched),
                            "actions": results}, ensure_ascii=False) + "\n")
    return {"status": "ok", "saved": sorted(touched), "diffs": diffs, "actions": results,
            "instructions": _after_note(touched, results)}


def _side_effect(project_dir: Path, action: dict[str, Any]) -> dict[str, Any]:
    """Run the non-artifact actions; return what happened."""
    kind = action["type"]
    patch = action.get("patch") or {}
    rid = action["id"].split(".")[0]
    if kind.split(".", 1)[0] in ("glossary", "style", "address"):
        return {**action, "status": "saved"}
    if action["status"] == "offer":
        if kind == "retranslate":
            return {**action, "command": _retranslate_command(project_dir, patch["chapters"])}
        return action
    if kind == "judge.dismiss":
        from src.book_review.corpus import load_book
        from web_ui.evaluations import append_feedback

        findings = {f.id: f for f in load_book(project_dir).findings}
        # Finding ids are positions in a chunk's list: a re-judged chunk can put a
        # different issue at the same id, so the key stamped at commit must still match.
        stamped = patch.get("finding_keys") or {}
        written, moved = 0, []
        for fid in patch["finding_ids"]:
            f = findings.get(fid)
            if not f or (fid in stamped and stamped[fid] != f.issue_key):
                moved.append(fid)
                continue
            append_feedback(project_dir, f.chunk_id, f.judge, int(fid.rsplit(":", 1)[-1]),
                            patch["feedback_type"], message=f.message, note=f"book review {rid}",
                            key=f.issue_key)
            written += 1
        return {**action, "status": "done", "marked": written, "skipped_changed": moved}
    if kind == "rejudge":
        from web_ui.evaluations import mark_evaluation_stale

        chunks = [p.stem for c in patch["chapters"]
                  for p in sorted((project_dir / "chunks").glob(f"{c}_chunk_*.json"))]
        for chunk_id in chunks:
            mark_evaluation_stale(project_dir, chunk_id, reason=f"book review {rid}")
        return {**action, "status": "done", "chunks": len(chunks)}
    if kind == "house.rule_proposal":
        from src.house.proposals import record_proposals

        ids = record_proposals(project_dir.name, [{"category": "book-review", "proposals": [
            {"rule": patch["rule"], "why": patch.get("why") or "", "subjects": []}]}])
        return {**action, "status": "done", "proposal_ids": ids}
    if kind == "retranslate":
        return {**action, "status": "offer", "command": _retranslate_command(project_dir, patch["chapters"])}
    return {**action, "status": "offer"}


def _retranslate_command(project_dir: Path, chapters: list[str]) -> str:
    chunks = [p.stem for c in chapters for p in sorted((project_dir / "chunks").glob(f"{c}_chunk_*.json"))]
    return (f"python scripts/harness.py retranslate --project {project_dir.name} "
            f"--chunk-ids {','.join(chunks)}   # preview; add --yes after approval")


def _after_note(touched: dict, results: list[dict]) -> str:
    notes = []
    if touched:
        notes.append("Artifact changes reach chunks translated from now on; chapters already "
                     "translated keep the old wording unless redone.")
    if any(r["type"] == "retranslate" for r in results):
        notes.append("Retranslate commands are previews: run them through references/retranslate.md.")
    if any(r["type"] == "manual" for r in results):
        notes.append("Manual items need a human or a code change; nothing was done for them.")
    return " ".join(notes) or "Done."
