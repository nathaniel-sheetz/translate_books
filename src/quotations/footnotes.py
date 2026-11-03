"""
Reference footnotes: one endnote per marked Bible quotation, holding its reference.

Every quotation card whose words sit inside « » gets a note such as ``Sal 116:11``
(the Spanish abbreviation, :meth:`Ref.spanish`, with an en dash in a range). The marker
goes right after the quotation's closing », past any closing marks stacked on it
and past the punctuation that follows (``«…».¹``, ``«…»,¹ dice``), and stops at a
space or a raya. For a quotation split by a speaker tag it is the last part's ».

Nothing here edits the book. A note is a ``type: "footnote"`` record in
``annotations.jsonl`` with ``origin: "quotation_refs"``, written through
:func:`src.footnote_pass.write.add` (which validates every one), and the marker
only exists in the EPUB build and the reader.

- **Which cards.** ``quotation`` cards that are not superseded and have a
  resolved reference; allusions, epigraphs, bare references and not-Scripture
  cards get none. A quotation woven into the prose without « » gets none.
- **Defaults.** Every note is ticked except where the author already wrote the
  reference beside the quotation (``ref_written``). A verse quoted again in the
  same chapter is ticked and flagged ``repeat``.
- **Notes already there.** Only notes with this module's origin are ever changed
  or retired, plus, when a quotation's note is ticked, the notes it ``replaces``:
  another note whose marker lands on exactly the same spot (two markers would
  read as one quotation with two notes), or a note in the same chapter holding
  nothing but the same verse's reference. Any other note is never touched.
- **The page.** ``footnotes_board.json`` holds the ticks and an approval of the
  exact plan (notes to add and retire). :func:`write` refuses a plan that changed
  since it was approved.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.annotations import store
from src.endnotes import _injection_point, parse_endnote_content
from src.footnote_import import _unique_anchor
from src.footnote_pass import FOOTNOTE_TYPE
from src.footnote_pass.corpus import load_alignment_es_map, read_chapter_body
from src.quotations import quotations_dir
from src.quotations.refs import find_es_refs, parse_ref

ORIGIN = "quotation_refs"

#: Verse ranges in a note take an en dash (``2 R 4:8–37``), whatever the book's text uses.
RANGE_DASH = "–"

#: Card statuses whose reference was looked up and found.
_PLACEABLE_STATUSES = ("proposed", "matches", "no_mapping", "refused", "unmapped")

_OPEN, _CLOSE = "«", "»"
#: Closing marks that can stack on the quotation's own ».
_STACKED = "»”’"
#: Punctuation the marker steps past after the quotation.
_TRAILING = ",.;:?!…)"
#: How far around the quotation an author's own written reference counts.
_REF_BEFORE, _REF_AFTER = 100, 30


def board_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "footnotes_board.json"


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_board(project_dir: Path) -> dict[str, Any]:
    saved = _read_json(board_path(Path(project_dir)))
    if not isinstance(saved, dict):
        saved = {}
    choices = saved.get("choices") if isinstance(saved.get("choices"), dict) else {}
    return {
        "choices": {k: bool(v) for k, v in choices.items() if isinstance(k, str) and isinstance(v, bool)},
        "saved_at": saved.get("saved_at"),
        "approved": saved.get("approved") if isinstance(saved.get("approved"), dict) else None,
    }


def _write_board(project_dir: Path, board: dict[str, Any]) -> None:
    path = board_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


# ── finding the spot ──────────────────────────────────────────────────────────

def _open_depth(body: str, start: int, end: int) -> int:
    """How many « are open at ``end``, counted from ``start``."""
    depth = 0
    for ch in body[start:end]:
        if ch == _OPEN:
            depth += 1
        elif ch == _CLOSE and depth:
            depth -= 1
    return depth


def marker_end(body: str, start: int, end: int) -> Optional[int]:
    """Where the marker goes for the quotation at ``body[start:end]``, or None when
    the quotation is not inside « ».

    Marked means the quotation's end sits inside an open « (counted from its
    paragraph), or a « opens within it. The marker follows the » that closes it,
    then any closing marks stacked on that one, then any run of
    ``, . ; : ? ! … )``.
    """
    para = body.rfind("\n", 0, start) + 1
    if _open_depth(body, para, end):
        close = body.find(_CLOSE, end)
    elif _OPEN in body[start:end]:
        close = body.find(_CLOSE, body.rfind(_OPEN, start, end))
    else:
        return None
    if close < 0 or "\n" in body[end:close]:
        return None
    pos = close + 1
    while pos < len(body) and body[pos] in _STACKED:
        pos += 1
    while pos < len(body) and body[pos] in _TRAILING:
        pos += 1
    return pos


def _spans(card: dict[str, Any], decision: Optional[dict[str, Any]]) -> list[str]:
    """The quotation's words as the book has them now, most likely first."""
    out: list[str] = []
    if decision and decision.get("action") == "applied":
        out += [e.get("new") or "" for e in decision.get("edits") or [] if e.get("what") in ("quote", "paste")]
    out += [e.get("new") or "" for e in card.get("quote_edits") or []]
    out += [e.get("old") or "" for e in card.get("quote_edits") or []]
    out.append(card.get("es_span") or "")
    seen: list[str] = []
    for span in out:
        span = span.strip().strip("«»‹›“”\"' ").strip()
        if span and span not in seen:
            seen.append(span)
    return seen


def _sentence_spans(body: str, es_map: dict[int, str]) -> dict[int, tuple[int, int]]:
    """``es_idx -> (start, end)`` in the body, found the way ``endnotes`` finds them."""
    out = {}
    for idx, text in es_map.items():
        at = body.find(text) if text else -1
        if at >= 0:
            out[idx] = (at, at + len(text))
    return out


#: How far apart the parts of one split quotation can sit.
_SPLIT_REACH = 400
#: A quotation edited by hand is found by its last words, this many, this near its sentences.
_TAIL_WORDS = 4
_TAIL_REACH = 300


def _locate(body: str, sentences: dict[int, tuple[int, int]], card: dict[str, Any],
            decision: Optional[dict[str, Any]], claimed: set[int]) -> tuple[Optional[int], Optional[int], bool]:
    """``(start, end, by_tail)`` of the quotation in the chapter, its last part's end
    for a split one.

    The card's ``es_idx`` is only a hint: applying a chapter realigns it, so the
    numbers on a card mapped before then can point a few sentences off. The
    quotation is the occurrence of its words nearest the hinted sentences whose end
    no other card in the chapter has taken (``claimed``), so a verse quoted twice
    gets both. A quotation edited by hand since no longer matches whole; then its
    last few words, near its own sentences, find it, and ``by_tail`` says so.
    """
    rows = [sentences[i] for i in card.get("es_idx") or [] if i in sentences]
    lo, hi = (min(r[0] for r in rows), max(r[1] for r in rows)) if rows else (0, 0)

    def distance(at: int, length: int) -> int:
        return max(lo - at, at + length - hi, 0)

    spans = _spans(card, decision)
    for span in spans:
        found = []
        at = body.find(span)
        while at >= 0:
            if at + len(span) not in claimed:
                found.append(at)
            at = body.find(span, at + 1)
        if not found:
            continue
        start = min(found, key=lambda a: distance(a, len(span)))
        end = start + len(span)
        # The parts of a split quotation are separate edits; whichever ends last ends it.
        for other in spans:
            at = body.find(other, start, end + _SPLIT_REACH)
            if at >= 0 and at + len(other) > end and body.count("\n", end, at) == 0:
                end = at + len(other)
        claimed.add(end)
        claimed.add(start + len(span))
        return start, end, False
    # Edited by hand since: the quotation's last words, near its own sentences only.
    if rows:
        for span in spans:
            # Never the whole span: that was searched for already.
            words = span.split()
            take = min(_TAIL_WORDS, len(words) - 1)
            if take < 2:
                continue
            tail = " ".join(words[-take:])
            at = body.find(tail, max(0, lo - _TAIL_REACH), hi + _TAIL_REACH)
            if at >= 0 and at + len(tail) not in claimed:
                claimed.add(at + len(tail))
                return at, at + len(tail), True
    return None, None, False


def _sentence_at(sentences: dict[int, tuple[int, int]], pos: int,
                 prefer: list[int]) -> Optional[int]:
    """The sentence holding the character before ``pos``; the card's own first."""
    hits = [i for i, (s, e) in sentences.items() if s < pos <= e]
    for i in prefer:
        if i in hits:
            return i
    return min(hits) if hits else None


# ── the items ─────────────────────────────────────────────────────────────────

def _cards(project_dir: Path) -> list[dict[str, Any]]:
    from src.quotations import board as qboard

    doc = qboard.load_mapped(project_dir) or {}
    return [c for c in doc.get("cards") or [] if c.get("kind") == "quotation"]


def _nbla_in_book(body: str, start: int, end: int, card: dict[str, Any],
                  decision: Optional[dict[str, Any]]) -> bool:
    """Whether the words at ``body[start:end]`` are the NBLA's: applied by the pass,
    or already the verse's wording (a card declined over capitals or a mark)."""
    if decision and decision.get("action") == "applied":
        return True
    pieces = [str(p).strip().strip(",.;:").strip() for e in card.get("quote_edits") or []
              for p in e.get("pieces") or []]
    found = body[start:end].casefold()
    return bool(pieces) and all(p.casefold() in found for p in pieces if p)


def items(project_dir: Path) -> dict[str, Any]:
    """Every quotation card with its note, its spot, and why it has none if it has none."""
    from src.quotations.apply import ACTION_SUPERSEDED, latest_decisions

    project_dir = Path(project_dir)
    latest = latest_decisions(project_dir)
    notes = store.load_active(project_dir, types=(FOOTNOTE_TYPE,))
    out: list[dict[str, Any]] = []
    skipped = {"superseded": 0, "unresolved": 0, "unmarked": 0}
    texts: dict[str, tuple[Optional[str], dict[int, str], dict[int, tuple[int, int]]]] = {}
    claimed: dict[str, set[int]] = {}
    spots: dict[tuple[str, int], str] = {}
    seen_refs: set[tuple[str, str]] = set()

    for card in _cards(project_dir):
        decision = latest.get(card["key"])
        if decision and decision.get("action") == ACTION_SUPERSEDED:
            skipped["superseded"] += 1
            continue
        ref = parse_ref(card.get("ref") or "")
        if card.get("status") not in _PLACEABLE_STATUSES or ref is None or not ref.verses:
            skipped["unresolved"] += 1
            continue
        chapter_id = card["chapter_id"]
        if chapter_id not in texts:
            body = read_chapter_body(project_dir, chapter_id)
            es_map = load_alignment_es_map(project_dir, chapter_id)
            texts[chapter_id] = (body, es_map, _sentence_spans(body or "", es_map))
        body, es_map, sentences = texts[chapter_id]
        item: dict[str, Any] = {
            "key": card["key"],
            "chapter_id": chapter_id,
            "ref": card.get("ref"),
            "ref_key": ref.key,
            "note": ref.spanish(RANGE_DASH),
            "nbla": False,
            "by_tail": False,
            "es_idx": None,
            "anchor": None,
            "unplaced": None,
            "ref_written": False,
            "repeat": False,
            "replaces": [],
            "written": None,
        }
        start, end, by_tail = _locate(body or "", sentences, card, decision, claimed.setdefault(chapter_id, set()))
        if start is None:
            item["unplaced"] = "the quotation's words are not in the chapter text"
            out.append(item)
            continue
        spot = marker_end(body, start, end)
        if spot is None:
            skipped["unmarked"] += 1
            continue
        item["nbla"] = _nbla_in_book(body, start, end, card, decision)
        item["by_tail"] = by_tail
        es_idx = _sentence_at(sentences, spot, list(card.get("es_idx") or []))
        if es_idx is None:
            # The trailing punctuation can sit past the aligned sentence's end.
            es_idx = _sentence_at(sentences, end, list(card.get("es_idx") or []))
            if es_idx is not None:
                spot = min(spot, sentences[es_idx][1])
        if es_idx is None:
            item["unplaced"] = "the quotation's end is in no aligned sentence"
            out.append(item)
            continue
        if (chapter_id, spot) in spots:
            item["unplaced"] = f"another quotation's note is on the same spot ({spots[(chapter_id, spot)]})"
            out.append(item)
            continue
        spots[(chapter_id, spot)] = card["key"]
        s, e = sentences[es_idx]
        anchor = _unique_anchor(body, s, e, spot)
        if not anchor or _injection_point(body, s, e, anchor) != spot:
            item["unplaced"] = "no words before the quotation's end are unique in its sentence"
            out.append(item)
            continue
        item.update({"es_idx": es_idx, "anchor": anchor})
        item["head"] = body[max(s, spot - 140):spot]
        item["tail"] = body[spot:min(e, spot + 60)]
        para_start = body.rfind("\n", 0, start) + 1
        para_end = body.find("\n", spot)
        context = body[max(para_start, start - _REF_BEFORE):min(para_end if para_end >= 0 else len(body),
                                                                spot + _REF_AFTER)]
        item["ref_written"] = any(set(f.ref.verses) & set(ref.verses) for f in find_es_refs(context) if f.ref)
        seen_key = (chapter_id, item["note"])
        item["repeat"] = seen_key in seen_refs
        seen_refs.add(seen_key)
        content = compose(anchor, item["note"])
        for note in notes:
            if note.get("chapter_id") != chapter_id or note.get("es_idx") != es_idx:
                continue
            if note.get("origin") == ORIGIN:
                if note.get("content") == content:
                    item["written"] = note.get("sub_id")
                continue
            note_anchor, _text = parse_endnote_content(note.get("content") or "")
            if _injection_point(body, s, e, note_anchor) == spot:
                item["replaces"].append(_replaced(note, "same_spot"))
        out.append(item)
    _replace_reference_notes(out, notes)
    return {"items": out, "skipped": skipped}


def _replaced(note: dict[str, Any], why: str) -> dict[str, Any]:
    return {"es_idx": note.get("es_idx"), "sub_id": note.get("sub_id"), "content": note.get("content") or "",
            "origin": note.get("origin"), "why": why}


def _reference_only(text: str) -> Optional[str]:
    """The ref key when a note's text is nothing but one written reference."""
    text = re.sub(r"\bSalmo\b", "Salmos", text.strip().rstrip("."))
    found = find_es_refs(text)
    if len(found) == 1 and found[0].ref and found[0].start == 0 and found[0].end == len(text):
        return found[0].ref.key
    return None


def _replace_reference_notes(found: list[dict[str, Any]], notes: list[dict[str, Any]]) -> None:
    """A note of someone else's that holds only the same verse's reference, in the
    same chapter, is replaced by the nearest note for that verse: the reader would
    otherwise get the reference twice. Anything with more words in it stays."""
    taken = {r["sub_id"] for it in found for r in it["replaces"]}
    for note in notes:
        if note.get("origin") == ORIGIN or note.get("sub_id") in taken:
            continue
        key = _reference_only(parse_endnote_content(note.get("content") or "")[1])
        if key is None:
            continue
        mates = [it for it in found if it["chapter_id"] == note.get("chapter_id")
                 and it["ref_key"] == key and not it["unplaced"]]
        if mates:
            nearest = min(mates, key=lambda it: abs(it["es_idx"] - (note.get("es_idx") or 0)))
            nearest["replaces"].append(_replaced(note, "same_reference"))


def compose(anchor: str, note: str) -> str:
    from src.footnote_pass.write import compose_content

    return compose_content(anchor, note)


def default_tick(item: dict[str, Any]) -> bool:
    return not item.get("unplaced") and not item.get("ref_written")


def ticks(found: list[dict[str, Any]], saved: dict[str, bool]) -> dict[str, bool]:
    """The tick in force for every item: saved, else the default; never on an unplaced one."""
    return {it["key"]: (saved.get(it["key"], default_tick(it)) if not it.get("unplaced") else False)
            for it in found}


# ── the plan, and the write ──────────────────────────────────────────────────

def plan(project_dir: Path, found: Optional[list[dict[str, Any]]] = None,
         saved: Optional[dict[str, bool]] = None) -> dict[str, Any]:
    """What a write would do: notes to add and notes to retire, with a fingerprint."""
    project_dir = Path(project_dir)
    if found is None:
        found = items(project_dir)["items"]
    if saved is None:
        saved = load_board(project_dir)["choices"]
    on = ticks(found, saved)
    wanted = {(it["chapter_id"], it["es_idx"], compose(it["anchor"], it["note"])): it
              for it in found if on[it["key"]]}
    retire: list[dict[str, Any]] = []
    have = set()
    for note in store.load_active(project_dir, types=(FOOTNOTE_TYPE,)):
        if note.get("origin") != ORIGIN:
            continue
        key = (note.get("chapter_id"), note.get("es_idx"), note.get("content"))
        if key in wanted and key not in have:
            have.add(key)
        else:
            retire.append({"chapter_id": note.get("chapter_id"), "es_idx": note.get("es_idx"),
                           "sub_id": note.get("sub_id"), "content": note.get("content"), "origin": ORIGIN})
    for it in wanted.values():
        for old in it["replaces"]:
            retire.append({"chapter_id": it["chapter_id"], **old})
    add = [{"chapter_id": c, "es_idx": i, "anchor": it["anchor"], "note": it["note"], "key": it["key"]}
           for (c, i, _content), it in wanted.items() if (c, i, _content) not in have]
    body = json.dumps({"add": [[a["chapter_id"], a["es_idx"], a["anchor"], a["note"]] for a in add],
                       "retire": [[r["chapter_id"], r["es_idx"], r["sub_id"]] for r in retire]},
                      ensure_ascii=False, sort_keys=True)
    return {"add": add, "retire": retire, "fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]}


def save_choices(project_dir: Path, choices: dict[str, Any]) -> dict[str, Any]:
    project_dir = Path(project_dir)
    found = {it["key"]: it for it in items(project_dir)["items"]}
    problems = [f"no such item: {k}" for k in choices if k not in found]
    problems += [f"not a tick: {k}" for k, v in choices.items() if not isinstance(v, bool)]
    problems += [f"has no place for a note: {k}" for k, v in choices.items()
                 if v is True and k in found and found[k].get("unplaced")]
    if problems:
        return {"status": "error", "problems": problems}
    board = load_board(project_dir)
    board["choices"].update(choices)
    board["saved_at"] = datetime.now().isoformat(timespec="seconds")
    board["approved"] = None
    _write_board(project_dir, board)
    return {"status": "ok"}


def approve(project_dir: Path) -> dict[str, Any]:
    project_dir = Path(project_dir)
    board = load_board(project_dir)
    current = plan(project_dir, saved=board["choices"])
    if not current["add"] and not current["retire"]:
        return {"status": "error", "problems": ["nothing to write"]}
    board["approved"] = {"fingerprint": current["fingerprint"], "at": datetime.now().isoformat(timespec="seconds")}
    _write_board(project_dir, board)
    return {"status": "ok"}


def unapprove(project_dir: Path) -> None:
    board = load_board(Path(project_dir))
    board["approved"] = None
    _write_board(Path(project_dir), board)


def build(project_dir: Path) -> dict[str, Any]:
    """The page's state: every item by chapter, its tick, the plan and the approval."""
    project_dir = Path(project_dir)
    found = items(project_dir)
    board = load_board(project_dir)
    on = ticks(found["items"], board["choices"])
    current = plan(project_dir, found["items"], board["choices"])
    chapters: dict[str, list[dict[str, Any]]] = {}
    for it in found["items"]:
        chapters.setdefault(it["chapter_id"], []).append(
            {**it, "ticked": on[it["key"]], "saved": it["key"] in board["choices"]})
    approved = board["approved"]
    if approved:
        approved = {**approved, "stale": approved.get("fingerprint") != current["fingerprint"]}
    counts = {
        "items": len(found["items"]),
        "ticked": sum(on.values()),
        "unplaced": sum(1 for it in found["items"] if it.get("unplaced")),
        "ref_written": sum(1 for it in found["items"] if it.get("ref_written")),
        "repeat": sum(1 for it in found["items"] if it.get("repeat")),
        "written": sum(1 for it in found["items"] if it.get("written") and on[it["key"]]),
        "to_add": len(current["add"]),
        "to_retire": len(current["retire"]),
        **{f"skipped_{k}": v for k, v in found["skipped"].items()},
    }
    return {
        "status": "ok",
        "chapters": [{"chapter_id": c, "items": v} for c, v in chapters.items()],
        "counts": counts,
        "approved": approved,
        "saved_at": board["saved_at"],
    }


def write(project_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Write the approved plan: tombstone what it retires, then add through ``add``."""
    from src.footnote_pass import write as fp_write

    project_dir = Path(project_dir)
    board = load_board(project_dir)
    current = plan(project_dir, saved=board["choices"])
    summary = {
        "to_add": len(current["add"]),
        "to_retire": len(current["retire"]),
        "retire": [{k: r.get(k) for k in ("chapter_id", "es_idx", "sub_id", "content", "origin")}
                   for r in current["retire"]],
    }
    if dry_run:
        return {"status": "ok", "dry_run": True, **summary,
                "add": [{k: a[k] for k in ("chapter_id", "es_idx", "anchor", "note")} for a in current["add"]]}
    approved = board["approved"] or {}
    if approved.get("fingerprint") != current["fingerprint"]:
        return {"status": "error", **summary, "error": (
            "the footnotes page is not approved, or what it would write changed since: approve it there"
        )}
    if not current["add"] and not current["retire"]:
        return {"status": "ok", **summary, "added": 0, "retired": 0, "refused": []}

    now = datetime.now().isoformat()
    store.append_records(project_dir, [{
        "project_id": project_dir.name, "chapter_id": r["chapter_id"], "es_idx": r["es_idx"],
        "sub_id": r["sub_id"], "removed": True, "origin": r.get("origin"), "timestamp": now,
    } for r in current["retire"]])
    added = fp_write.add(
        project_dir,
        [{"chapter_id": a["chapter_id"], "es_idx": a["es_idx"], "anchor": a["anchor"], "note": a["note"],
          "reason": f"reference of {a['key']}"} for a in current["add"]],
        report=False, origin=ORIGIN,
    ) if current["add"] else {"counts": {"added": 0}, "refused": [], "warnings": []}
    board["approved"] = None
    _write_board(project_dir, board)
    return {
        "status": "ok" if not added.get("refused") else "partial",
        **summary,
        "added": added["counts"]["added"],
        "retired": len(current["retire"]),
        "refused": added.get("refused") or [],
        "warnings": added.get("warnings") or [],
        "instructions": "Run `footnote_pass.py verify`, then build the EPUB; a note reaches the book only then.",
    }
