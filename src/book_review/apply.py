"""Apply the recommendations the operator picked, through the normal save paths.

``recommendations.json`` stamps each recommendation with the hash of every
artifact its patches edit, taken when it was validated. :func:`apply` refuses a
selection whose artifacts changed since, because a patch validated against one
glossary may be wrong against another. It checks every selected recommendation
before writing anything, then applies them in order.

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
from src.book_review.evidence import artifact_hashes
from src.book_review.synthesize import recommendations_path

WRITER = "book_review"
_FILES = {"glossary": "glossary.json", "style": "style.json", "address_map": "address_map.json"}


def _load(project_dir: Path, name: str) -> Optional[dict[str, Any]]:
    path = project_dir / _FILES[name]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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


def _entry(terms: list[dict], english: str) -> Optional[dict]:
    wanted = english.lower()
    return next((t for t in terms if str(t.get("english") or "").lower() == wanted), None)


def _apply_glossary(doc: dict, patch: dict) -> None:
    terms = doc.setdefault("terms", [])
    kind = patch["type"]
    if kind == "glossary.add":
        terms.append({"english": patch["english"], "spanish": patch["spanish"], "type": "other",
                      "alternatives": patch.get("alternatives") or [],
                      **({"context": patch["context"]} if patch.get("context") else {})})
        return
    entry = _entry(terms, patch["english"])
    if entry is None:
        raise ValueError(f"glossary entry {patch['english']!r} is gone")
    if kind == "glossary.rekey":
        entry["english"] = patch["new_english"]
    elif kind == "glossary.add_variant":
        twin = copy.deepcopy(entry)
        twin["english"] = patch["new_english"]
        terms.insert(terms.index(entry) + 1, twin)
    elif kind == "glossary.edit":
        for field in ("spanish", "alternatives", "context"):
            if field in patch:
                entry[field] = patch[field]
    elif kind == "glossary.split_sense":
        at = terms.index(entry)
        terms.pop(at)
        for offset, e in enumerate(patch["entries"]):
            terms.insert(at + offset, {"english": e["english"], "spanish": e["spanish"],
                                       "type": entry.get("type") or "other", "alternatives": [],
                                       **({"context": e["context"]} if e.get("context") else {})})
    elif kind == "glossary.remove":
        terms.remove(entry)


def _apply_style(doc: dict, patch: dict) -> None:
    content = doc.get("content") or ""
    if patch["type"] == "style.replace_text":
        if content.count(patch["before"]) != 1:
            raise ValueError("the style guide text to replace is no longer there exactly once")
        doc["content"] = content.replace(patch["before"], patch["after"], 1)
    else:
        doc["content"] = content.rstrip() + "\n\n" + patch["text"]


def _apply_address(doc: dict, patch: dict) -> None:
    if patch["type"] == "address.replace_global_rules_text":
        rules = doc.get("global_rules") or ""
        if rules.count(patch["before"]) != 1:
            raise ValueError("the global-rules text to replace is no longer there exactly once")
        doc["global_rules"] = rules.replace(patch["before"], patch["after"], 1)
        return
    for pair in doc.get("pairs") or []:
        if pair.get("a") == patch["a"] and pair.get("b") == patch["b"]:
            segment = {"form": patch["form"], "when": "default"}
            if patch.get("notes"):
                segment["notes"] = patch["notes"]
            pair.setdefault("directions", {})[patch["direction"]] = [segment]
            return
    raise ValueError(f"address pair {patch['a']}/{patch['b']} is gone")


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


def _save(project_dir: Path, name: str, doc: dict, reason: str) -> None:
    from src.models import AddressMap, Glossary, StyleGuide
    from src.utils.file_io import save_address_map, save_glossary, save_style_guide

    path = project_dir / _FILES[name]
    if name == "glossary":
        save_glossary(Glossary.model_validate(doc), path, writer=WRITER, reason=reason)
    elif name == "style":
        save_style_guide(StyleGuide.model_validate(doc), path, writer=WRITER, reason=reason)
    else:
        save_address_map(AddressMap.model_validate(doc), path, writer=WRITER, reason=reason)


def apply(project_dir: Path, selection: list[str], *, yes: bool = False) -> dict[str, Any]:
    project_dir = Path(project_dir)
    try:
        doc = json.loads(recommendations_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "no recommendations.json; run `commit` first"}
    by_id = {r["id"]: r for r in doc.get("recommendations") or []}
    chosen = parse_selection(selection)
    unknown = sorted(set(chosen) - set(by_id))
    if not chosen or unknown:
        return {"status": "error", "error": f"unknown recommendation ids: {unknown or selection}"}

    current = artifact_hashes(project_dir)
    stale = []
    for rid in chosen:
        for name, stamped in (by_id[rid].get("precondition") or {}).items():
            if stamped != current.get(name):
                stale.append(f"{rid}: {_FILES[name]} changed since the review")
    if stale:
        return {"status": "error", "error": "artifacts changed since `commit`; re-run the review",
                "stale": stale}

    loaded = {name: _load(project_dir, name) for name in _FILES}
    working = {name: copy.deepcopy(d) for name, d in loaded.items() if d is not None}
    touched: dict[str, list[str]] = {}
    actions: list[dict[str, Any]] = []
    for rid, numbers in chosen.items():
        rec = by_id[rid]
        for n, patch in enumerate(rec["patches"], 1):
            if numbers is not None and n not in numbers:
                continue
            kind = patch["type"]
            family = kind.split(".", 1)[0]
            try:
                if family in ("glossary", "style", "address"):
                    name = {"glossary": "glossary", "style": "style", "address": "address_map"}[family]
                    if name not in working:
                        raise ValueError(f"{_FILES[name]} does not exist")
                    {"glossary": _apply_glossary, "style": _apply_style,
                     "address": _apply_address}[family](working[name], patch)
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

    for name, ids in touched.items():
        _save(project_dir, name, working[name], reason=", ".join(sorted({i.split('.')[0] for i in ids})))
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
        written = 0
        for fid in patch["finding_ids"]:
            f = findings.get(fid)
            if not f:
                continue
            append_feedback(project_dir, f.chunk_id, f.judge, int(fid.rsplit(":", 1)[-1]),
                            patch["feedback_type"], message=f.message, note=f"book review {rid}",
                            key=f.issue_key)
            written += 1
        return {**action, "status": "done", "marked": written}
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
