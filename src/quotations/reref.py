"""
Re-reference: the user says a card names the wrong verse.

The scan keys a card by its reference, so another verse is another card.
:func:`reref` makes it: a copy of the card on the same sentence, with the new
reference, its verses looked up, ``origin: "reref"`` and ``replaces`` (the old
card's key). It starts unmapped. Its wording comes through
:func:`src.quotations.revise.revise` like any ask, so every piece is still checked
as verbatim NBLA, and it lands only when the user approves it on the board.

- **An applied old card stays as it is until then.** When the new card is
  applied, :mod:`src.quotations.apply` gives the old one a ``superseded`` row in
  the same write: the record stops counting its verse, ``verify`` stops looking
  for its text, and the board shows it as replaced.
- **An unapplied old card is set to Reject**, so it is recorded as declined; it
  claims none of the words its replacement edits.

The request lives in ``rerefs.json`` (``{old_key: {ref_en, key, at, why}}``),
which ``resolve`` reads on every run, so a re-scan or a re-resolve keeps the new
card. Asking again for the same old card, before its replacement is applied,
swaps the replacement for one on the newer reference.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.quotations import quotations_dir
from src.quotations import scan as qscan
from src.quotations.refs import parse_ref

ORIGIN = "reref"

#: What the old candidate knew about its own reference, which the new one does not inherit.
_NOT_INHERITED = (
    "written_ref", "ref_edit", "ref_ok", "ref_edit_shared", "unresolved_reason",
    "verses", "status", "flags", "adapted", "why",
)


def rerefs_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "rerefs.json"


def load_rerefs(project_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(rerefs_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict) and v.get("ref_en")} \
        if isinstance(doc, dict) else {}


def derived_candidate(old: dict[str, Any], ref_en: str, index) -> dict[str, Any]:
    """The candidate for ``old``'s sentence on ``ref_en``, with its verses looked up."""
    from src.quotations.resolve import STATUS_RESOLVED, STATUS_UNRESOLVED, verses_for

    ref = parse_ref(ref_en)
    new = {k: v for k, v in old.items() if k not in _NOT_INHERITED}
    new.update({
        "key": qscan.candidate_key(old["chapter_id"], ref.key, old.get("en_span") or ""),
        "ref_en": ref_en,
        "ref": ref.english,
        "ref_key": ref.key,
        "verse_keys": ref.verse_keys(),
        "origin": ORIGIN,
        "replaces": old["key"],
        "flags": [],
        "adapted": False,
        "why": "",
        "ref_edit": None,
    })
    verses, missing = verses_for(index, new)
    new["verses"] = verses
    if missing or not verses:
        new["status"] = STATUS_UNRESOLVED
        new["unresolved_reason"] = f"not in the {index.version.upper()} index: {missing or new['ref_key']}"
    else:
        new["status"] = STATUS_RESOLVED
    return new


def add_derived(project_dir: Path, candidates: list[dict[str, Any]], index) -> list[dict[str, Any]]:
    """``candidates`` with each re-referenced card's replacement right after it."""
    rerefs = load_rerefs(project_dir)
    if not rerefs:
        return candidates
    out: list[dict[str, Any]] = []
    for cand in candidates:
        out.append(cand)
        request = rerefs.get(cand.get("key"))
        if request and cand.get("origin") != ORIGIN:
            out.append(derived_candidate(cand, request["ref_en"], index))
    return out


def _write(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def reref(project_dir: Path, key: str, ref_en: str, *, why: str = "") -> dict[str, Any]:
    """Make the card for another verse on card ``key``'s sentence. Writes nothing on an error."""
    from src.quotations import board as qboard
    from src.quotations import map as qmap
    from src.quotations.apply import ACTION_APPLIED, ACTION_SUPERSEDED, latest_decisions
    from src.quotations.index import IndexError_

    project_dir = Path(project_dir)
    resolved = qmap.load_resolved(project_dir)
    doc = qboard.load_mapped(project_dir)
    if resolved is None or doc is None:
        return {"status": "error", "error": "no resolved.json or mapped.json; run `resolve` and `map-commit` first"}
    cands = resolved.get("candidates") or []
    old = next((c for c in cands if c.get("key") == key), None)
    if old is None:
        return {"status": "error", "error": f"no such card: {key}"}
    if old.get("origin") == ORIGIN:
        return {"status": "error", "error": f"this card already replaces another; run `reref` on {old.get('replaces')}"}
    if old.get("kind") not in qmap.MAPPED_KINDS:
        return {"status": "error", "error": f"a {old.get('kind')} card has no verse wording to replace"}
    latest = latest_decisions(project_dir)
    if (latest.get(key) or {}).get("action") == ACTION_SUPERSEDED:
        return {"status": "error", "error": f"this card was already replaced by {latest[key].get('superseded_by')}"}
    ref = parse_ref(ref_en)
    if ref is None or not ref.verses:
        return {"status": "error", "error": f"cannot read {ref_en!r} as a chapter and verse (e.g. 'Jeremiah 12:3')"}
    try:
        index = qmap.load_index(resolved.get("version") or "nbla")
    except IndexError_ as exc:
        return {"status": "error", "error": str(exc)}

    new = derived_candidate(old, ref_en, index)
    if new["key"] == key:
        return {"status": "error", "error": "that is the verse this card already names"}
    if new["status"] != "resolved":
        return {"status": "error", "error": new.get("unresolved_reason")}
    rerefs = load_rerefs(project_dir)
    prev_key = (rerefs.get(key) or {}).get("key")
    if prev_key and (latest.get(prev_key) or {}).get("action") == ACTION_APPLIED:
        return {"status": "error", "error": f"its replacement {prev_key} is already applied"}
    if any(c.get("key") == new["key"] for c in cands if c.get("key") != prev_key):
        return {"status": "error", "error": f"a card {new['key']} already exists"}

    # A replacement asked for earlier, on another verse, gives way to this one.
    cands = [c for c in cands if c.get("key") != prev_key]
    cards = [c for c in doc.get("cards") or [] if c.get("key") != prev_key]
    at = next(i for i, c in enumerate(cands) if c.get("key") == key)
    cands.insert(at + 1, new)
    text = qmap.ChapterText(project_dir, old["chapter_id"])
    card = qmap.build_card(new, None, text, qmap._Claims(), index.proper,
                           fix_caps=not index.doc.get("smallcaps_resolved"))
    at = next((i for i, c in enumerate(cards) if c.get("key") == key), len(cards) - 1)
    cards.insert(at + 1, card)

    rerefs[key] = {"ref_en": ref_en, "key": new["key"], "why": why,
                   "at": datetime.now().isoformat(timespec="seconds")}
    _write(rerefs_path(project_dir), rerefs)
    _write(qmap.resolved_path(project_dir), {**resolved, "candidates": cands})
    _write(qmap.mapped_path(project_dir), {**doc, "cards": cards})

    old_applied = (latest.get(key) or {}).get("action") == ACTION_APPLIED
    old_card = next((c for c in cards if c.get("key") == key), None) or {}
    if not old_applied and old_card:
        saved = qboard.load(project_dir)["choices"].get(key) or qboard.default_choice(old_card)
        qboard.save(project_dir, {key: {**saved, "state": qboard.STATE_REJECT, "ask": ""}})

    # The words the new mapping replaces: what the old card put in, else what it would have edited.
    if old_applied:
        in_book = [e["new"] for e in latest[key].get("edits") or [] if e.get("what") in ("quote", "paste")]
    else:
        in_book = [e["old"] for e in old_card.get("quote_edits") or []]
    in_book = in_book or [old.get("es_span") or ""]
    rows = {r["es_idx"]: r for r in qscan.load_rows(project_dir, old["chapter_id"]) if "es_idx" in r}
    return {
        "status": "ok",
        "key": new["key"],
        "replaces": key,
        "ref": new["ref"],
        "ref_es": ref.spanish(resolved.get("range_separator")),
        "old_applied": old_applied,
        "replaced_earlier": prev_key,
        "in_book": in_book,
        "rows": [{"es_idx": i, "en": (rows.get(i) or {}).get("en"), "es": (rows.get(i) or {}).get("es")}
                 for i in old.get("es_idx") or []],
        "verses": [{"key": v["key"], "text": v["text"]} for v in new["verses"]],
        "instructions": (
            "Write a mapping that replaces the text in `in_book` with the new verse's NBLA "
            f"pieces and run `revise --card \"{new['key']}\" --file <path>`. The card then waits "
            "on the board at Not yet for the user to approve."
        ),
    }
