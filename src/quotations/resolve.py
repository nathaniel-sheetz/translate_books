"""
Resolve: look every candidate's verses up, and work out its reference fix. Code only.

Reads ``candidates.json`` (the scan) and the per-user Bible index, and writes
``resolved.json``: each candidate with a ``status`` —

- ``resolved``: every verse it names is in the index; the marked verse texts ride
  along for the mapping wave;
- ``unresolved``: no reference, one the parser cannot read, or a verse the index
  does not have. Shown on the board as a card to settle by hand, never an error;
- ``not_scripture``: nothing to look up; the board offers a paste box.

The reference fix is deterministic. Where the author wrote a reference, the
Spanish row that carries it is searched for the translator's form ("Juan 10:5",
"Heb. 11:6") and ``ref_edit`` proposes the standard abbreviation with the book's
own range separator (``Jn 10:5``, ``Jn 21:15–17``). Only the book name changes:
the numbers stay as the Spanish has them (see :func:`standard_form`).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.quotations import quotations_dir
from src.quotations import scan as qscan
from src.quotations.index import BibleIndex, IndexError_, load_index
from src.quotations.refs import Ref, find_es_refs, parse_ref, range_separator

STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVED = "unresolved"
STATUS_NOT_SCRIPTURE = "not_scripture"


def resolved_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "resolved.json"


def book_range_separator(project_dir: Path) -> Optional[str]:
    """The dash this book's Spanish uses in verse ranges, else its English's."""
    counts: Counter[str] = Counter()
    for chapter_id in qscan.chapter_ids(project_dir):
        for row in qscan.load_rows(project_dir, chapter_id):
            for found in find_es_refs(row.get("es") or ""):
                sep = range_separator(found.ref.text)
                if sep:
                    counts[sep] += 1
    if counts:
        return counts.most_common(1)[0][0]
    for chapter_id in qscan.chapter_ids(project_dir):
        sep = range_separator(qscan.chapter_english(project_dir, chapter_id))
        if sep:
            return sep
    return None


def verses_for(index: BibleIndex, candidate: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    """``([{key, label, text}], missing_keys)`` for a candidate's reference."""
    ref = parse_ref(candidate.get("ref_en") or "") if candidate.get("ref_en") else None
    keys = list(candidate.get("verse_keys") or [])
    if ref is not None:
        for chapter in ref.whole_chapters:
            keys += index.chapter_keys(ref.book, chapter)
    out, missing = [], []
    for key in dict.fromkeys(keys):
        text = index.get(key)
        if text is None:
            missing.append(key)
            continue
        book, chapter, verse = (int(p) for p in key.split(":"))
        out.append({"key": key, "label": f"{chapter}:{verse}", "text": text})
    return out, missing


def standard_form(found: Ref, sep: Optional[str]) -> str:
    """The Spanish reference with the standard abbreviation; its numbers kept.

    Abbreviation only: a translator who wrote ``1 Jn 3:19–20`` where the author
    has ``1 John 3:20`` may have done so on purpose (the quotation runs from
    v. 19), so the numbers are the Spanish's own. Only the range dash follows
    the book's style.
    """
    return found.spanish(sep)


def find_spanish_ref(
    candidate: dict[str, Any], rows_by_idx: dict[int, dict], written: str
) -> Optional[tuple[int, Ref]]:
    """``(es_idx, ref)`` of the Spanish reference that renders ``written``.

    Looked for in the candidate's rows first, then one row either side (an
    epigraph's reference often sits in the row after the verse).
    """
    ref = parse_ref(written)
    if ref is None:
        return None
    wanted = set(ref.verse_keys())
    idxs = list(candidate.get("es_idx") or [])
    order = idxs + [i + d for i in idxs for d in (1, -1, 2, -2)]
    for idx in dict.fromkeys(order):
        row = rows_by_idx.get(idx)
        if not row:
            continue
        for found in find_es_refs(row.get("es") or ""):
            if found.ref.book == ref.book and (wanted & set(found.ref.verse_keys()) or not wanted):
                return idx, found.ref
    return None


def resolve(project_dir: Path, *, index: Optional[BibleIndex] = None) -> dict[str, Any]:
    """Look every candidate up; write ``resolved.json``. Errors only on a missing index."""
    project_dir = Path(project_dir)
    cands_file = qscan.candidates_path(project_dir)
    if not cands_file.exists():
        return {"status": "error", "error": "no candidates.json; run `scan-commit` first"}
    if index is None:
        try:
            index = load_index("nbla")
        except IndexError_ as exc:
            return {"status": "error", "error": str(exc)}
    doc = json.loads(cands_file.read_text(encoding="utf-8"))
    sep = book_range_separator(project_dir)

    rows_cache: dict[str, dict[int, dict]] = {}
    claimed_refs: set[tuple[str, int, str]] = set()
    out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for cand in doc.get("candidates") or []:
        cand = dict(cand)
        chapter_id = cand["chapter_id"]
        if chapter_id not in rows_cache:
            rows_cache[chapter_id] = {
                r["es_idx"]: r for r in qscan.load_rows(project_dir, chapter_id) if "es_idx" in r
            }
        rows_by_idx = rows_cache[chapter_id]

        if cand.get("kind") == "not_scripture":
            cand["status"] = STATUS_NOT_SCRIPTURE
            cand["verses"] = []
        elif not cand.get("ref_key"):
            cand["status"] = STATUS_UNRESOLVED
            cand["unresolved_reason"] = f"cannot read the reference {cand.get('ref_en')!r}"
            cand["verses"] = []
        else:
            verses, missing = verses_for(index, cand)
            cand["verses"] = verses
            if missing or not verses:
                cand["status"] = STATUS_UNRESOLVED
                cand["unresolved_reason"] = f"not in the {index.version.upper()} index: {missing or cand['ref_key']}"
            else:
                cand["status"] = STATUS_RESOLVED

        cand["ref_edit"] = None
        written = cand.get("written_ref")
        if written:
            parsed = parse_ref(written)
            hit = find_spanish_ref(cand, rows_by_idx, written)
            if parsed is None:
                cand.setdefault("flags", []).append("written_ref_unreadable")
            elif hit is None:
                cand.setdefault("flags", []).append("es_ref_not_found")
            else:
                es_idx, found = hit
                old = found.text
                new = standard_form(found, sep)
                claim = (chapter_id, es_idx, old)
                if claim in claimed_refs:
                    cand["ref_edit_shared"] = True
                elif old != new:
                    cand["ref_edit"] = {"es_idx": es_idx, "old": old, "new": new}
                    claimed_refs.add(claim)
                else:
                    cand["ref_ok"] = {"es_idx": es_idx, "text": old}
                    claimed_refs.add(claim)
        counts[cand["status"]] += 1
        out.append(cand)

    resolved = {
        "project": project_dir.name,
        "resolved_at": datetime.now().isoformat(),
        "version": index.version,
        "range_separator": sep,
        "candidates": out,
    }
    path = resolved_path(project_dir)
    path.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "counts": dict(counts),
        "ref_edits": sum(1 for c in out if c.get("ref_edit")),
        "refs_already_standard": sum(1 for c in out if c.get("ref_ok")),
        "es_ref_not_found": sum(1 for c in out if "es_ref_not_found" in (c.get("flags") or [])),
        "range_separator": sep,
        "unresolved": [
            {"key": c["key"], "ref_en": c.get("ref_en"), "reason": c.get("unresolved_reason")}
            for c in out if c["status"] == STATUS_UNRESOLVED
        ],
        "resolved_path": str(path),
        "instructions": "Run `map-prepare` next.",
    }
