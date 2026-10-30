"""
Revisions: a different wording for one card, because the user asked for it.

On the board a card has an **ask** box: what the user wants instead of the
proposed diff. The agent reads the open asks (:func:`asks`), writes a mapping in
the wave's format for each, and :func:`revise` validates it and rebuilds that one
card. The card comes back to the board pending, with its new diff.

A revision's mapping is the worker's (``replace``/``echo``/``drift``/``note``/
``adapted``), with one addition: a ``replace`` item may carry ``new``, the
Spanish to use as written, when the user wants words that are not the NBLA's.
Its ``pieces`` must still be verbatim NBLA and must all appear in ``new``; that
is what the record counts. Without ``new``, code builds the wording with the
NBLA's conventions, as for the wave.

Revisions live in ``revisions.json`` and win over the wave on every
``map-commit``. An applied card cannot be revised: fix it in the book from the
board's link and tick "fixed by hand".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.quotations import map as qmap
from src.quotations import scan as qscan


def _store(project_dir: Path, key: str, revision: dict[str, Any]) -> None:
    path = qmap.revisions_path(project_dir)
    revisions = qmap.load_revisions(project_dir)
    revisions[key] = revision
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(revisions, ensure_ascii=False, indent=2), encoding="utf-8")


def asks(project_dir: Path) -> dict[str, Any]:
    """Every card with an open ask, and what the agent needs to answer it.

    Carries NBLA verse text (from ``resolved.json``); it stays under ``projects/``.
    """
    from src.quotations import board as qboard

    project_dir = Path(project_dir)
    doc = qboard.load_mapped(project_dir)
    if doc is None:
        return {"status": "error", "error": "no mapped.json; run `map-commit` first"}
    resolved = {c["key"]: c for c in (qmap.load_resolved(project_dir) or {}).get("candidates") or []}
    from src.quotations.apply import applied_cards

    saved = qboard.load(project_dir)["choices"]
    applied = applied_cards(project_dir)
    out = []
    rows_by_chapter: dict[str, dict[int, dict]] = {}
    for card in doc.get("cards") or []:
        ask = str((saved.get(card["key"]) or {}).get("ask") or "").strip()
        if not ask:
            continue
        chapter_id = card["chapter_id"]
        if chapter_id not in rows_by_chapter:
            rows_by_chapter[chapter_id] = {
                r["es_idx"]: r for r in qscan.load_rows(project_dir, chapter_id) if "es_idx" in r
            }
        rows = rows_by_chapter[chapter_id]
        out.append({
            "key": card["key"],
            "ask": ask,
            "chapter_id": chapter_id,
            "ref": card.get("ref"),
            "kind": card.get("kind"),
            "status": card.get("status"),
            "applied": card["key"] in applied,
            "en_span": card.get("en_span"),
            "es_span": card.get("es_span"),
            "rows": [{"es_idx": i, "en": (rows.get(i) or {}).get("en"), "es": (rows.get(i) or {}).get("es")}
                     for i in card.get("es_idx") or []],
            "quote_edits": [{k: e.get(k) for k in ("es_idx", "old", "new", "pieces")}
                            for e in card.get("quote_edits") or []],
            "echo": card.get("echo") or [],
            "drift": card.get("drift") or "",
            "verses": [{"key": v.get("key"), "text": v.get("text")}
                       for v in (resolved.get(card["key"]) or {}).get("verses") or []],
        })
    return {
        "status": "ok",
        "asks": out,
        "instructions": (
            "For each ask, write a mapping in the wave's format (replace: [{es_idx, old, "
            "pieces, new?}], echo, drift, note) to a file and run `revise --card <key> "
            "--file <path>`. Use `new` only when the user wants words that are not the "
            "NBLA's; every piece must still be verbatim NBLA and appear in it."
        ),
    }


def rebuild_card(project_dir: Path, key: str, mapping: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    """The card ``key`` rebuilt from ``mapping``, validated against the current text.

    Every other unapplied card in the chapter claims its spans first, so the
    revision is refused where it would overlap one of them.
    """
    from src.quotations.apply import applied_cards
    from src.quotations import board as qboard

    project_dir = Path(project_dir)
    resolved = qmap.load_resolved(project_dir) or {}
    cand = next((c for c in resolved.get("candidates") or [] if c.get("key") == key), None)
    if cand is None:
        raise KeyError(f"no candidate {key!r} in resolved.json")
    doc = qboard.load_mapped(project_dir) or {}
    index = qmap.load_index(resolved.get("version") or "nbla")
    text = qmap.ChapterText(project_dir, cand["chapter_id"])
    claims = qmap._Claims()
    applied = applied_cards(project_dir)
    for other in doc.get("cards") or []:
        if other["key"] == key or other["key"] in applied or other["chapter_id"] != cand["chapter_id"]:
            continue
        edits = list(other.get("quote_edits") or []) + list(other.get("echo") or [])
        if other.get("ref_edit"):
            edits.append(other["ref_edit"])
        for edit in edits:
            span = qmap._span(text.es(edit["es_idx"]) or "", edit["old"])
            if span:
                claims.take(edit["es_idx"], span)
    card = qmap.build_card(
        cand, mapping, text, claims, index.proper,
        fix_caps=not index.doc.get("smallcaps_resolved"), allow_new=True,
    )
    return qmap.mark_revised(card, revision)


def revise(project_dir: Path, key: str, mapping: dict[str, Any], *, ask: str | None = None) -> dict[str, Any]:
    """Validate ``mapping`` for card ``key``; on success store it and put the card back to pending.

    Writes nothing when any part is refused, so a half-good revision never
    reaches the board.
    """
    from src.quotations import board as qboard
    from src.quotations.apply import applied_cards

    project_dir = Path(project_dir)
    doc = qboard.load_mapped(project_dir)
    if doc is None:
        return {"status": "error", "error": "no mapped.json; run `map-commit` first"}
    position = next((i for i, c in enumerate(doc.get("cards") or []) if c["key"] == key), None)
    if position is None:
        return {"status": "error", "error": f"no such card: {key}"}
    if key in applied_cards(project_dir):
        return {"status": "error", "error": (
            "this card is already applied; fix it in the book from the board's link "
            "and tick 'fixed by hand'"
        )}
    if not isinstance(mapping, dict) or not (mapping.get("replace") or mapping.get("echo")):
        return {"status": "error", "error": "the mapping needs a `replace` or an `echo` list"}

    board = qboard.load(project_dir)
    old_choice = board["choices"].get(key) or {}
    ask = ask if ask is not None else str(old_choice.get("ask") or "")
    revision = {"mapping": mapping, "ask": ask, "at": datetime.now().isoformat(timespec="seconds")}
    try:
        card = rebuild_card(project_dir, key, mapping, revision)
    except KeyError as exc:
        return {"status": "error", "error": str(exc)}
    edits = {
        "quote_edits": [{k: e[k] for k in ("es_idx", "old", "new")} for e in card["quote_edits"]],
        "echo": [{k: e[k] for k in ("es_idx", "old", "new")} for e in card["echo"]],
    }
    if card["refusals"] or not (card["quote_edits"] or card["echo"]):
        return {"status": "refused", "key": key, "refusals": card["refusals"], **edits,
                "instructions": "Nothing was written. Fix the mapping and run `revise` again."}

    _store(project_dir, key, revision)
    doc["cards"][position] = card
    qmap.mapped_path(project_dir).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    qboard.save(project_dir, {key: {
        "ref": bool(old_choice.get("ref", True)) and bool(card.get("ref_edit")),
        "nbla": bool(card["quote_edits"]),
        "echo": list(range(len(card["echo"]))),
        "paste": "",
        "state": qboard.STATE_PENDING,
        "hand": False,
        "ask": "",
    }})
    return {
        "status": "ok",
        "key": key,
        "card_status": card["status"],
        "adapted": bool(card.get("adapted")),
        **edits,
        "instructions": "The card is back on the board, pending, with the new diff. Ask the user to review it.",
    }
