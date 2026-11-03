"""
The quotation board: every card the pass made, the choices on it, and the live diff.

``mapped.json`` is what the waves and the validator proposed. :func:`build` puts
beside each card what the page needs: the choice in force (saved, or the
defaults), the edits that choice makes, whether each edit still lands on the
current chunk text, and whether the card was already applied.

The page writes one file, ``board.json``::

    {"choices": {card_key: {"ref": true, "nbla": true, "echo": [0], "paste": "",
                            "state": "approve", "hand": false, "ask": ""}},
     "saved_at": "...",
     "approved": {"fingerprint": "...", "at": "..."} | null}

The ticks say **what** the change is: **fix the reference** (``ref``: the
abbreviation only) and **use the NBLA wording** (``nbla``). Echo edits are
sub-items with their own ticks; a ``not_scripture`` or unresolved card offers
``paste``, the Spanish to put in place of the translator's.

``state`` says **whether** it happens when the board is applied: ``approve``
applies the ticked changes, ``reject`` records the card as declined, ``pending``
leaves it for a later round. It starts from :func:`recommend`, a coded verdict
with the rule that gave it. ``hand`` says the user fixed the sentence in the
book themselves: nothing is written, but the card counts as NBLA in the record.
``ask`` is the wording the user wants instead; it holds the card at pending
until the agent revises it (:mod:`src.quotations.revise`).

Defaults, when a card has no saved choice: every proposed change is ticked
except echo edits, and the state follows the recommendation. A choice saved
before states existed takes its state from the ledger.

Saving different choices drops the approval. ``apply`` runs an approved board
only while its fingerprint (``mapped.json`` plus the text of every chunk a card
touches) still matches, so what is written is what was approved.

**The live diff** comes from :func:`src.corrections_apply.apply_to_chunk`, called
without ``dry_run`` on an in-memory copy of each chunk: ``dry_run=True`` throws
the patched text away. Nothing here writes a chunk.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

from src.quotations import quotations_dir
from src.quotations import scan as qscan
from src.quotations.map import (
    CARD_MATCHES,
    CARD_NO_MAPPING,
    CARD_NOT_SCRIPTURE,
    CARD_REFERENCE,
    CARD_REFUSED,
    CARD_UNMAPPED,
    CARD_UNRESOLVED,
    mapped_path,
)

SOURCE = "quotation-pass"

STATE_APPROVE = "approve"
STATE_REJECT = "reject"
STATE_PENDING = "pending"
STATES = (STATE_APPROVE, STATE_REJECT, STATE_PENDING)

VERDICT_APPROVE = "approve"
VERDICT_DECLINE = "decline"
VERDICT_UNSURE = "unsure"
_STATE_OF_VERDICT = {VERDICT_APPROVE: STATE_APPROVE, VERDICT_DECLINE: STATE_REJECT, VERDICT_UNSURE: STATE_PENDING}

#: Longest ask the page may store.
ASK_CAP = 2000


def board_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "board.json"


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_mapped(project_dir: Path) -> Optional[dict[str, Any]]:
    doc = _read_json(mapped_path(Path(project_dir)))
    return doc if isinstance(doc, dict) else None


def load(project_dir: Path) -> dict[str, Any]:
    saved = _read_json(board_path(Path(project_dir)))
    if not isinstance(saved, dict):
        saved = {}
    choices = saved.get("choices") if isinstance(saved.get("choices"), dict) else {}
    return {
        "choices": {k: v for k, v in choices.items() if isinstance(k, str) and isinstance(v, dict)},
        "saved_at": saved.get("saved_at"),
        "approved": saved.get("approved") if isinstance(saved.get("approved"), dict) else None,
    }


def _write(project_dir: Path, board: dict[str, Any]) -> None:
    path = board_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


# ── the recommendation ───────────────────────────────────────────────────────

def _verdict(verdict: str, rule: str, detail: str = "") -> dict[str, str]:
    return {"verdict": verdict, "rule": rule, "detail": detail}


def recommend(card: dict[str, Any]) -> dict[str, str]:
    """``{verdict, rule, detail}``: approve, decline or unsure, and the rule that said so.

    The first rule that matches wins. ``rule`` is a key the page words in either
    language; ``detail`` is the card's own text the reason quotes (the drift, the
    refusal reasons), never NBLA text.
    """
    status = card.get("status")
    if status == CARD_NOT_SCRIPTURE:
        return _verdict(VERDICT_DECLINE, "not_scripture")
    if status == CARD_UNRESOLVED:
        return _verdict(VERDICT_DECLINE, "unresolved", card.get("unresolved_reason") or "")
    if status in (CARD_NO_MAPPING, CARD_UNMAPPED):
        return _verdict(VERDICT_DECLINE, "no_mapping")
    if status == CARD_REFERENCE:
        if card.get("ref_edit"):
            return _verdict(VERDICT_APPROVE, "reference_fix")
        return _verdict(VERDICT_DECLINE, "reference_ok")
    if status == CARD_MATCHES:
        return _verdict(VERDICT_APPROVE, "matches")
    reasons = "; ".join(sorted({r.get("reason", "") for r in card.get("refusals") or []}))
    if status == CARD_REFUSED:
        return _verdict(VERDICT_UNSURE, "refused", reasons)
    if card.get("revised"):
        return _verdict(VERDICT_UNSURE, "revised", (card["revised"] or {}).get("ask") or "")
    if card.get("drift"):
        return _verdict(VERDICT_UNSURE, "drift", card["drift"])
    if card.get("adapted"):
        return _verdict(VERDICT_UNSURE, "adapted", card.get("why") or "")
    if card.get("kind") == "allusion":
        return _verdict(VERDICT_UNSURE, "allusion")
    if card.get("confidence") == "low":
        return _verdict(VERDICT_UNSURE, "low_confidence")
    if reasons:
        return _verdict(VERDICT_UNSURE, "partly_refused", reasons)
    if not any(e["old"] != e["new"] for e in card.get("quote_edits") or []):
        return _verdict(VERDICT_UNSURE, "echo_only")
    return _verdict(VERDICT_APPROVE, "clean")


# ── choices and the edits they make ──────────────────────────────────────────

def default_choice(card: dict[str, Any]) -> dict[str, Any]:
    """Every proposed change ticked except echo edits; the state follows :func:`recommend`."""
    return {
        "ref": bool(card.get("ref_edit")),
        "nbla": bool(card.get("quote_edits")),
        "echo": [],
        "paste": "",
        "state": _STATE_OF_VERDICT[recommend(card)["verdict"]],
        "hand": False,
        "ask": "",
    }


def _ticked(raw: dict[str, Any]) -> bool:
    return bool(raw.get("ref") or raw.get("nbla") or raw.get("echo") or raw.get("paste"))


def clean_choice(card: dict[str, Any], raw: Any, decision: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A saved choice, reduced to what this card can actually take.

    A choice saved before states existed takes one: from the card's latest
    ledger row (``decision``) when it has one, else from its ticks, which then
    were the whole decision.
    """
    base = default_choice(card)
    if not isinstance(raw, dict):
        return base
    echo_count = len(card.get("echo") or [])
    paste_ok = card.get("status") in (CARD_NOT_SCRIPTURE, CARD_UNRESOLVED)
    state = raw.get("state")
    if state not in STATES:
        if decision:
            state = STATE_APPROVE if decision.get("action") == "applied" else STATE_REJECT
        else:
            state = STATE_APPROVE if _ticked(raw) else STATE_REJECT
    ask = str(raw.get("ask") or "").strip()[:ASK_CAP]
    if ask:
        state = STATE_PENDING
    return {
        "ref": bool(raw.get("ref")) and bool(card.get("ref_edit")),
        "nbla": bool(raw.get("nbla")) and bool(card.get("quote_edits")),
        "echo": sorted({int(i) for i in raw.get("echo") or []
                        if isinstance(i, int) and 0 <= i < echo_count}),
        "paste": str(raw.get("paste") or "").strip() if paste_ok else "",
        "state": state,
        "hand": bool(raw.get("hand")) and card.get("status") not in (CARD_NOT_SCRIPTURE, CARD_UNRESOLVED),
        "ask": ask,
    }


def ticked_edits(card: dict[str, Any], choice: dict[str, Any]) -> list[dict[str, Any]]:
    """The edits the ticks name, whatever the state: what approving would write."""
    edits: list[dict[str, Any]] = []
    if choice.get("ref") and card.get("ref_edit"):
        ref = card["ref_edit"]
        edits.append({"what": "ref", "es_idx": ref["es_idx"], "old": ref["old"], "new": ref["new"]})
    if choice.get("nbla"):
        for edit in card.get("quote_edits") or []:
            if edit["old"] != edit["new"]:
                edits.append({"what": "quote", "es_idx": edit["es_idx"], "old": edit["old"], "new": edit["new"]})
    for i in choice.get("echo") or []:
        echo = card["echo"][i]
        edits.append({"what": "echo", "es_idx": echo["es_idx"], "old": echo["old"], "new": echo["new"], "n": i})
    paste = choice.get("paste")
    if paste and card.get("es_span") and card.get("es_idx"):
        edits.append({"what": "paste", "es_idx": card["es_idx"][0], "old": card["es_span"], "new": paste})
    return edits


def card_edits(card: dict[str, Any], choice: dict[str, Any]) -> list[dict[str, Any]]:
    """The edits ``choice`` makes on ``card``, each ``{what, es_idx, chunk_id, old, new}``.

    Only an approved card writes anything, and a card fixed by hand writes nothing.
    """
    if choice.get("state", STATE_APPROVE) != STATE_APPROVE or choice.get("hand"):
        return []
    return ticked_edits(card, choice)


# ── placing an edit in its chunk ─────────────────────────────────────────────

class Book:
    """Rows and chunk texts for the chapters the cards touch; read once."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.rows: dict[str, dict[int, dict]] = {}
        self.chunks: dict[str, Any] = {}

    def chapter_rows(self, chapter_id: str) -> dict[int, dict]:
        if chapter_id not in self.rows:
            self.rows[chapter_id] = {
                r["es_idx"]: r for r in qscan.load_rows(self.project_dir, chapter_id) if "es_idx" in r
            }
        return self.rows[chapter_id]

    def chunk(self, chunk_id: str):
        from src.utils.file_io import load_chunk

        if chunk_id not in self.chunks:
            path = self.project_dir / "chunks" / f"{chunk_id}.json"
            self.chunks[chunk_id] = load_chunk(path) if path.exists() else None
        return self.chunks[chunk_id]


def locate_edit(book: Book, chapter_id: str, edit: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The edit as a corrections record with chunk offsets, else ``None``.

    Offsets come from finding ``old`` in the chunk, as
    :func:`src.judges.fixes.to_correction_record` does: inside the row's
    sentence when the row is found in the chunk, else anywhere it occurs once.
    """
    row = book.chapter_rows(chapter_id).get(edit["es_idx"]) or {}
    chunk_id = row.get("chunk_id")
    chunk = book.chunk(chunk_id) if chunk_id else None
    if chunk is None:
        return None
    text = chunk.translated_text or ""
    es = row.get("es") or ""
    start = -1
    at_row = text.find(es) if es else -1
    if at_row >= 0 and text.count(es) == 1:
        inside = es.find(edit["old"])
        if inside >= 0:
            start = at_row + inside
    if start < 0 and text.count(edit["old"]) == 1:
        start = text.find(edit["old"])
    if start < 0:
        return None
    return {
        "chunk_id": chunk_id,
        "chapter_id": chapter_id,
        "project_id": book.project_dir.name,
        "original_es": edit["old"],
        "corrected_es": edit["new"],
        "chunk_offset_start": start,
        "chunk_offset_end": start + len(edit["old"]),
        "es_idx": None,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
    }


#: Where a sentence ends: terminal punctuation, any closing marks, then space;
#: or a paragraph break.
_SENTENCE_END = re.compile(r"[.!?…][»”\"')_\]]*\s+|\n\s*\n")

#: How far the context may run on either side before it is cut with "…".
CONTEXT_CAP = 500


def context_window(text: str, start: int, end: int, *, sentences: int = 1) -> tuple[int, int, bool, bool]:
    """``(lo, hi, cut_left, cut_right)`` around ``text[start:end]``.

    Its own sentence plus ``sentences`` either side; ``cut_*`` say the cap cut
    a sentence there (the page marks that with "…").
    """
    ends = [m.end() for m in _SENTENCE_END.finditer(text)]
    before = [e for e in ends if e <= start]
    after = [e for e in ends if e > end]
    lo = before[-(sentences + 1)] if len(before) > sentences else 0
    hi = after[sentences] if len(after) > sentences else len(text)
    cut_left, cut_right = lo < start - CONTEXT_CAP, hi > end + CONTEXT_CAP
    return max(lo, start - CONTEXT_CAP), min(hi, end + CONTEXT_CAP), cut_left, cut_right


def edit_context(book: "Book", chapter_id: str, edit: dict[str, Any]) -> Optional[dict[str, str]]:
    """The edit shown in place: ``{before, after}`` over the same stretch of the chunk.

    Read-only: the window is cut from the chunk text and the replacement made on
    the copy, so the page can diff a few sentences rather than the bare span.
    """
    record = locate_edit(book, chapter_id, edit)
    if record is None:
        return None
    text = book.chunk(record["chunk_id"]).translated_text or ""
    start, end = record["chunk_offset_start"], record["chunk_offset_end"]
    lo, hi, cut_left, cut_right = context_window(text, start, end)
    head = "…" if cut_left else ""
    tail = "…" if cut_right else ""
    before = text[lo:hi].strip()
    after = (text[lo:start] + edit["new"] + text[end:hi]).strip()
    return {"before": head + before + tail, "after": head + after + tail}


def preview(project_dir: Path, cards: list[dict[str, Any]], choices: dict[str, dict]) -> dict[str, Any]:
    """Apply every chosen edit to in-memory chunk copies; report what lands.

    Returns ``{card_key: [edit + {lands, record}]}`` plus per-chunk counts.
    """
    from src.corrections_apply import apply_to_chunk

    book = Book(project_dir)
    per_chunk: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        # Pending and rejected cards are previewed too, so the page can say
        # whether their change would still land before anyone approves it.
        edits = ticked_edits(card, choices[card["key"]])
        out[card["key"]] = []
        for edit in edits:
            record = locate_edit(book, card["chapter_id"], edit)
            item = {**edit, "lands": False, "record": record}
            out[card["key"]].append(item)
            if record is not None:
                per_chunk.setdefault(record["chunk_id"], []).append(
                    (card["key"], len(out[card["key"]]) - 1, record)
                )
    for chunk_id, items in per_chunk.items():
        chunk = book.chunk(chunk_id)
        copy = chunk.model_copy(deep=True)
        _updated, _count, applied = apply_to_chunk(copy, [r for _k, _i, r in items])
        for idx in applied:
            key, pos, _record = items[idx]
            out[key][pos]["lands"] = True
    return out


# ── fingerprint, save, approve ───────────────────────────────────────────────

def fingerprint(project_dir: Path, doc: dict[str, Any], board_choices: dict[str, dict]) -> str:
    """Changes when the cards are recomputed, the choices change, or a touched chunk is edited."""
    digest = hashlib.sha256()
    digest.update(json.dumps(doc.get("cards") or [], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(json.dumps(board_choices, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    book = Book(project_dir)
    chunk_ids = set()
    for card in doc.get("cards") or []:
        rows = book.chapter_rows(card["chapter_id"])
        for idx in card.get("es_idx") or []:
            if rows.get(idx, {}).get("chunk_id"):
                chunk_ids.add(rows[idx]["chunk_id"])
    for chunk_id in sorted(chunk_ids):
        chunk = book.chunk(chunk_id)
        digest.update(chunk_id.encode())
        digest.update(((chunk.translated_text if chunk else "") or "").encode("utf-8"))
    return digest.hexdigest()[:16]


def _decisions(project_dir: Path) -> dict[str, dict[str, Any]]:
    from src.quotations.apply import latest_decisions

    return latest_decisions(project_dir)


def effective_choices(doc: dict[str, Any], saved: dict[str, dict],
                      decisions: Optional[dict[str, dict]] = None) -> dict[str, dict]:
    decisions = decisions or {}
    return {card["key"]: clean_choice(card, saved.get(card["key"]), decisions.get(card["key"]))
            if card["key"] in saved else default_choice(card) for card in doc.get("cards") or []}


def save(project_dir: Path, choices: dict[str, Any]) -> dict[str, Any]:
    """Store the page's choices for the cards it names; any change drops the approval."""
    project_dir = Path(project_dir)
    doc = load_mapped(project_dir)
    if doc is None:
        return {"status": "error", "problems": ["no mapped.json; run `map-commit` first"]}
    by_key = {c["key"]: c for c in doc.get("cards") or []}
    unknown = sorted(k for k in choices if k not in by_key)
    if unknown:
        return {"status": "error", "problems": [f"no such card: {k}" for k in unknown[:10]]}
    board = load(project_dir)
    decisions = _decisions(project_dir)
    before = dict(board["choices"])
    for key, raw in choices.items():
        board["choices"][key] = clean_choice(by_key[key], raw, decisions.get(key))
    if board["choices"] != before:
        board["approved"] = None
    board["saved_at"] = datetime.now().isoformat(timespec="seconds")
    _write(project_dir, board)
    return {"status": "ok"}


def approve(project_dir: Path) -> dict[str, Any]:
    """Approve the board as it stands: the choices in force and the text they were checked on."""
    project_dir = Path(project_dir)
    doc = load_mapped(project_dir)
    if doc is None:
        return {"status": "error", "problems": ["no mapped.json; run `map-commit` first"]}
    board = load(project_dir)
    choices = effective_choices(doc, board["choices"], _decisions(project_dir))
    board["choices"] = choices
    board["approved"] = {
        "fingerprint": fingerprint(project_dir, doc, choices),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    board["saved_at"] = board["approved"]["at"]
    _write(project_dir, board)
    return {"status": "ok", "approved": board["approved"]}


def unapprove(project_dir: Path) -> dict[str, Any]:
    board = load(project_dir)
    board["approved"] = None
    _write(Path(project_dir), board)
    return {"status": "ok"}


def approved_choices(project_dir: Path) -> tuple[Optional[dict[str, dict]], Optional[str]]:
    """The approved choices, or why there are none to apply."""
    project_dir = Path(project_dir)
    doc = load_mapped(project_dir)
    if doc is None:
        return None, "no mapped.json; run `map-commit` first"
    board = load(project_dir)
    if not board["approved"]:
        return None, "the board is not approved; approve it on the page first"
    current = fingerprint(project_dir, doc, board["choices"])
    if current != board["approved"].get("fingerprint"):
        return None, (
            "the board changed since it was approved (the cards were recomputed or a "
            "chapter was edited); open the page, check the diffs and approve again"
        )
    return effective_choices(doc, board["choices"], _decisions(project_dir)), None


# ── the page's state ──────────────────────────────────────────────────────────

def _row_holding(rows: dict[int, dict], text: str, near: Optional[int]) -> Optional[dict]:
    """The row whose Spanish holds ``text``, the one closest to ``near`` of several.

    A quotation can run past its first sentence, so its opening words are tried
    when the whole of it sits in no one row.
    """
    words = text.split()
    for probe in (" ".join(words), " ".join(words[:6]), " ".join(words[:3])):
        if not probe:
            continue
        hits = [r for r in rows.values() if probe in " ".join((r.get("es") or "").split())]
        if hits:
            return min(hits, key=lambda r: abs(r["es_idx"] - near) if near is not None else 0)
    return None


def reader_url(book: Book, card: dict[str, Any], decision: Optional[dict[str, Any]]) -> Optional[str]:
    """The reader at this card's sentence, flashed on landing.

    An applied card is found by the text that went in (its rows were realigned
    since); any other by the translator's words it would replace, else its span.
    The reader lands on a sentence by its opening words, so the link carries
    those, with the row number to choose between rows that open alike, and
    ``return_to``: the board at this card, where the chunk editor sends the user
    after a save.
    """
    first = (card.get("es_idx") or [None])[0]
    hints: list[tuple[Optional[int], str]] = []
    if decision and decision.get("action") == "applied":
        # The quotation itself, before a reference or an echo edit.
        edits = sorted(decision.get("edits") or [], key=lambda e: e.get("what") != "quote")
        hints += [(e.get("es_idx"), e["new"]) for e in edits if e.get("new")]
    hints += [(e.get("es_idx"), e["old"]) for e in card.get("quote_edits") or [] if e.get("old")]
    if card.get("es_span"):
        hints.append((first, card["es_span"]))
    rows = book.chapter_rows(card["chapter_id"])
    row = None
    for idx, text in hints:
        row = _row_holding(rows, text, first if idx is None else idx)
        if row:
            break
    row = row or rows.get(first)
    if not row or not (row.get("es") or "").strip():
        return None
    name = book.project_dir.name
    back = f"/quotations/{name}#card-{quote(card['key'], safe='')}"
    query = urlencode({"anchor": row["es"][:60], "esi": row["es_idx"], "hl": 1, "near": 1,
                       "return_to": back})
    return f"/read/{name}/{card['chapter_id']}?{query}"


def phase(card_view: dict[str, Any]) -> str:
    """Where a card stands: ``applied`` once it is in the book with nothing more to
    record, or replaced by another verse's card that is, else its state."""
    choice = card_view["choice"]
    history = card_view.get("history") or {}
    if history.get("action") == "superseded":
        return "applied"
    if history.get("action") == "applied" and not (choice.get("hand") and not history.get("hand")):
        return "applied"
    return choice["state"]


def build(project_dir: Path) -> dict[str, Any]:
    """Everything the page draws, grouped by chapter."""
    project_dir = Path(project_dir)
    doc = load_mapped(project_dir)
    if doc is None:
        return {"status": "empty", "chapters": [], "counts": {}}
    board = load(project_dir)
    decisions = _decisions(project_dir)
    choices = effective_choices(doc, board["choices"], decisions)
    cards = doc.get("cards") or []
    done = {k: r for k, r in decisions.items() if r.get("action") in ("applied", "superseded")}
    live = preview(project_dir, [c for c in cards if c["key"] not in done], choices)
    refs = {c["key"]: c.get("ref") for c in cards}

    chapters: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, Any] = {
        "cards": len(cards), "picked_edits": 0, "not_landing": 0, "applied": 0, "superseded": 0,
        "to_record": 0, "by_status": {}, "by_phase": {}, "by_recommendation": {}, "by_kind": {}, "asked": 0,
    }
    book = Book(project_dir)
    for card in cards:
        view = dict(card)
        view.pop("verses", None)
        decision = decisions.get(card["key"])
        view["recommendation"] = recommend(card)
        view["reader_url"] = reader_url(book, card, decision)
        if card.get("replaces"):
            view["replaces_ref"] = refs.get(card["replaces"]) or card["replaces"]
        if decision:
            view["history"] = {k: decision.get(k) for k in ("action", "ts", "hand", "edits", "superseded_ref")}
        if card["key"] not in done:
            # Each proposed change carries a few sentences of the chapter around
            # it, so the page can show it in place (`context`: {before, after}).
            view["quote_edits"] = [
                {**e, "context": edit_context(book, card["chapter_id"], e)} if e["old"] != e["new"] else e
                for e in card.get("quote_edits") or []
            ]
            view["echo"] = [{**e, "context": edit_context(book, card["chapter_id"], e)}
                            for e in card.get("echo") or []]
            if card.get("ref_edit"):
                view["ref_edit"] = {**card["ref_edit"],
                                    "context": edit_context(book, card["chapter_id"], card["ref_edit"])}
        choice = choices[card["key"]]
        view["choice"] = choice
        view["saved"] = card["key"] in board["choices"]
        applied = done.get(card["key"])
        if applied:
            view["applied"] = applied
            view["edits"] = []
            counts["superseded" if applied["action"] == "superseded" else "applied"] += 1
        else:
            view["edits"] = [{k: v for k, v in e.items() if k != "record"} for e in live.get(card["key"], [])]
            if choice["state"] == STATE_APPROVE and not choice["hand"]:
                counts["picked_edits"] += len(view["edits"])
                counts["not_landing"] += sum(1 for e in view["edits"] if not e["lands"])
        view["phase"] = phase(view)
        last = (decision or {}).get("action")
        if view["phase"] == STATE_APPROVE:
            does = choice["hand"] or bool(view["edits"]) or (choice["nbla"] and bool(card.get("quote_edits")))
            if does or last != "declined":
                counts["to_record"] += 1
        elif view["phase"] == STATE_REJECT and last not in ("declined", "applied"):
            counts["to_record"] += 1
        for axis, value in (("by_status", card["status"]), ("by_phase", view["phase"]),
                            ("by_recommendation", view["recommendation"]["verdict"]),
                            ("by_kind", card.get("kind") or "?")):
            counts[axis][value] = counts[axis].get(value, 0) + 1
        if choice["ask"] or card.get("revised"):
            counts["asked"] += 1
        chapters.setdefault(card["chapter_id"], []).append(view)

    approved = board["approved"]
    if approved and approved.get("fingerprint") != fingerprint(project_dir, doc, board["choices"]):
        approved = {**approved, "stale": True}
    return {
        "status": "ok",
        "project": project_dir.name,
        "version": doc.get("version"),
        "worker_model": doc.get("worker_model"),
        "committed_at": doc.get("committed_at"),
        "chapters": [{"chapter_id": cid, "cards": chapters[cid]} for cid in sorted(chapters)],
        "counts": counts,
        "saved_at": board["saved_at"],
        "approved": approved,
    }
