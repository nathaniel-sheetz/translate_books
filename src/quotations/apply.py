"""
Apply an approved quotation board through the judge-apply path, and keep the ledger.

Mirrors ``scripts/run_judges.py`` ``_cmd_apply``, per chunk: snapshot the chunk
(``.chunk_edits/``) → :func:`~src.corrections_apply.apply_to_chunk` → save →
:func:`~src.corrections_apply.archive_applied_records` → stale-mark the chunk's
evaluation; then per chapter :func:`~src.corrections_apply.recombine_chapter` →
:func:`~src.corrections_apply.realign_chapter` (which re-anchors annotations).

- **The reader's ``corrections.jsonl`` queue is never touched.** Draining it would
  also apply the reader's own pending edits.
- Records carry chunk offsets found from ``old`` in the chunk
  (:func:`src.quotations.board.locate_edit`) and ``source: "quotation-pass"``.
- **A card lands whole or not at all.** Its edits are first applied to in-memory
  copies; a card with any edit that would not land is reported and skipped.
- **Resumable.** A card whose edits are already in the text (old gone, new
  present) is recorded as applied, not re-edited; a chapter whose alignment is
  older than its chunks gets its recombine/realign tail on the next run.

- **Only approved cards are applied.** A rejected card gets one ``declined`` row
  (and never undoes an applied one); a pending card gets nothing. A card the
  user fixed by hand gets an ``applied`` row with ``hand`` and no edits, applied
  card or not, so ``verify`` stops looking for the text an earlier apply wrote.

Decisions go to ``.harness/quotations/decisions.jsonl``, append-only, one row per
card per apply that decides it, keyed as the scan keyed it. A later row for a key supersedes an
earlier one. ``quotations.json`` (:mod:`src.quotations.record`) is computed from it.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.quotations import quotations_dir

ACTION_APPLIED = "applied"
ACTION_DECLINED = "declined"


# ── the ledger ────────────────────────────────────────────────────────────────

def decisions_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "decisions.jsonl"


def append_decisions(project_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = decisions_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_decisions(project_dir: Path) -> list[dict[str, Any]]:
    path = decisions_path(project_dir)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("key"):
            rows.append(row)
    return rows


def latest_decisions(project_dir: Path) -> dict[str, dict[str, Any]]:
    """The last row for every card key."""
    out: dict[str, dict[str, Any]] = {}
    for row in read_decisions(project_dir):
        out[row["key"]] = row
    return out


def applied_cards(project_dir: Path) -> dict[str, dict[str, Any]]:
    """Card key -> its latest ledger row, for cards whose latest row is ``applied``."""
    return {k: r for k, r in latest_decisions(project_dir).items() if r.get("action") == ACTION_APPLIED}


# ── helpers shared with run_judges' apply ────────────────────────────────────

def _write_chunk_snapshot(project_dir: Path, chapter_id: str, chunk_id: str, chunk_path: Path, ts: str) -> Path:
    """Copy the pre-edit chunk into ``.chunk_edits/`` (the web-UI editor convention)."""
    backup_root = project_dir / ".chunk_edits" / chapter_id / chunk_id
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{ts}.json"
    backup_path.write_text(chunk_path.read_text(encoding="utf-8"), encoding="utf-8")
    for old_backup in sorted(backup_root.glob("*.json"))[:-10]:
        try:
            old_backup.unlink()
        except OSError:
            pass
    return backup_path


def alignment_is_stale(project_dir: Path, chapter_id: str) -> bool:
    """True if the chapter's alignment is missing or older than any of its chunks."""
    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    if not align_path.exists():
        return True
    aligned_at = align_path.stat().st_mtime
    for chunk_path in (project_dir / "chunks").glob(f"{chapter_id}_chunk_*.json"):
        if chunk_path.stat().st_mtime > aligned_at:
            return True
    return False


def _already_in(book, record: dict[str, Any]) -> bool:
    """The edit is already made: the new text is in the chunk and the old is not."""
    chunk = book.chunk(record["chunk_id"]) if record else None
    text = (chunk.translated_text if chunk else "") or ""
    return bool(record) and record["corrected_es"] in text and record["original_es"] not in text


def _ledger_row(card: dict[str, Any], choice: dict[str, Any], edits: list[dict[str, Any]],
                action: str, applied_at: str, *, recovered: bool = False,
                hand_words: Optional[int] = None) -> dict[str, Any]:
    """One ledger row. ``hand_words`` marks a card the user fixed in the book."""
    hand = hand_words is not None
    quote_edits = card.get("quote_edits") or []
    verse_keys = sorted({k for e in quote_edits for k in e.get("verse_keys") or []})
    nbla_words = sum(e.get("nbla_words") or 0 for e in quote_edits)
    if hand:
        verse_keys = verse_keys or sorted(card.get("verse_keys") or [])
        nbla_words = nbla_words or hand_words
    row = {
        "ts": applied_at,
        "key": card["key"],
        "chapter_id": card["chapter_id"],
        "action": action,
        "kind": card.get("kind"),
        "ref": card.get("ref"),
        "status": card.get("status"),
        "choice": choice,
        "nbla": hand or (bool(choice.get("nbla")) and bool(quote_edits)),
        "adapted": bool(card.get("adapted")),
        "drift": card.get("drift") or "",
        "en_span": card.get("en_span"),
        "verse_keys": verse_keys,
        "nbla_words": nbla_words,
        "edits": [{k: e[k] for k in ("what", "old", "new", "chunk_id") if k in e} for e in edits],
    }
    if hand:
        row["hand"] = True
    if recovered:
        row["recovered"] = True
    return row


def _verse_words(project_dir: Path) -> dict[str, int]:
    """Card key -> words in its verses' NBLA text, for a card fixed by hand with no
    mapping to count from. The whole verse is more than the author quoted, which
    errs the safe way for the NBLA's limits."""
    from src.quotations.map import load_resolved

    resolved = load_resolved(project_dir) or {}
    return {
        c["key"]: sum(len(str(v.get("text") or "").split()) for v in c.get("verses") or [])
        for c in resolved.get("candidates") or [] if c.get("key")
    }


# ── apply ─────────────────────────────────────────────────────────────────────

def apply(project_dir: Path, *, no_realign: bool = False) -> dict[str, Any]:
    """Apply the approved board. Errors, changing nothing, when it is not approved."""
    from src.corrections_apply import (
        apply_to_chunk,
        archive_applied_records,
        realign_chapter,
        recombine_chapter,
    )
    from src.quotations import board as qboard
    from src.quotations.record import write_record
    from src.utils.file_io import save_chunk

    project_dir = Path(project_dir)
    choices, error = qboard.approved_choices(project_dir)
    if error:
        # Still finish an interrupted run's realign tail: it changes no text.
        pending = _pending_tails(project_dir)
        if pending and not no_realign:
            with contextlib.redirect_stdout(sys.stderr):
                for chapter_id in pending:
                    recombine_chapter(project_dir, chapter_id)
                    realign_chapter(project_dir, chapter_id)
            return {"status": "error", "error": error, "chapters_realigned": pending}
        return {"status": "error", "error": error}

    doc = qboard.load_mapped(project_dir)
    cards = {c["key"]: c for c in doc.get("cards") or []}
    latest = latest_decisions(project_dir)
    done = {k for k, r in latest.items() if r.get("action") == ACTION_APPLIED}
    book = qboard.Book(project_dir)
    applied_at = datetime.now().isoformat(timespec="seconds")
    ts = time.strftime("%Y%m%dT%H%M%S")
    verse_words: Optional[dict[str, int]] = None

    ledger: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    plan: dict[str, list[tuple[str, dict[str, Any]]]] = {}   # chunk -> [(card key, record)]
    card_records: dict[str, list[dict[str, Any]]] = {}
    placed: dict[str, list[dict[str, Any]]] = {}
    for key, card in cards.items():
        choice = choices[key]
        last = (latest.get(key) or {}).get("action")
        state = choice.get("state", qboard.STATE_APPROVE)
        if state == qboard.STATE_PENDING:
            continue
        if state == qboard.STATE_REJECT:
            # A rejection is recorded once, and cannot take back what is in the book.
            if last not in (ACTION_DECLINED, ACTION_APPLIED):
                ledger.append(_ledger_row(card, choice, [], ACTION_DECLINED, applied_at))
            continue
        if choice.get("hand"):
            # Fixed in the book by the user: nothing to write, but it counts as NBLA.
            # An applied card can be redone this way too.
            if not (latest.get(key) or {}).get("hand"):
                if verse_words is None:
                    verse_words = _verse_words(project_dir)
                ledger.append(_ledger_row(card, choice, [], ACTION_APPLIED, applied_at,
                                          hand_words=verse_words.get(key, 0)))
            continue
        if key in done:
            continue
        edits = qboard.card_edits(card, choice)
        if not edits:
            action = ACTION_APPLIED if choice.get("nbla") and card.get("quote_edits") else ACTION_DECLINED
            if action != last:
                ledger.append(_ledger_row(card, choice, [], action, applied_at))
            continue
        records = [qboard.locate_edit(book, card["chapter_id"], e) for e in edits]
        # The ledger keeps each edit's chunk, which is what `verify` reads.
        placed[key] = [{**e, "chunk_id": r["chunk_id"]} if r else e for e, r in zip(edits, records)]
        if all(r is None or _already_in(book, r) for r in records) and any(records):
            ledger.append(_ledger_row(card, choice, placed[key], ACTION_APPLIED, applied_at, recovered=True))
            continue
        if any(r is None for r in records):
            skipped.append({"key": key, "reason": "an edit's text is no longer in the chapter"})
            continue
        card_records[key] = [{**r, "card": key} for r in records]
        for record in card_records[key]:
            plan.setdefault(record["chunk_id"], []).append((key, record))

    # Dry pass: a card lands whole or not at all.
    bad: set[str] = set()
    for chunk_id, items in plan.items():
        chunk = book.chunk(chunk_id)
        _u, _n, applied = apply_to_chunk(chunk.model_copy(deep=True), [r for _k, r in items])
        for i, (key, _r) in enumerate(items):
            if i not in applied:
                bad.add(key)
    for key in sorted(bad):
        skipped.append({"key": key, "reason": "an edit would not land on the current text"})

    chunk_order = sorted(plan)
    chunks_of: dict[str, set[str]] = {}
    for chunk_id in chunk_order:
        for key, _r in plan[chunk_id]:
            if key not in bad:
                chunks_of.setdefault(key, set()).add(chunk_id)
    finished: set[str] = set()
    logged: set[str] = set()
    affected: list[str] = []
    backups: list[str] = []
    archived = 0

    from web_ui.evaluations import mark_evaluation_stale

    for chunk_id in chunk_order:
        records = [r for key, r in plan[chunk_id] if key not in bad]
        if not records:
            continue
        chunk = book.chunk(chunk_id)
        chapter_id = records[0]["chapter_id"]
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        updated, _count, applied = apply_to_chunk(chunk, [{k: v for k, v in r.items() if k != "card"} for r in records])
        landed = [records[i] for i in applied]
        backups.append(str(_write_chunk_snapshot(project_dir, chapter_id, chunk_id, chunk_path, ts)))
        save_chunk(updated, chunk_path)
        book.chunks[chunk_id] = updated
        archive_applied_records(
            project_dir, [{k: v for k, v in r.items()} for r in landed], applied_at=applied_at
        )
        archived += len(landed)
        mark_evaluation_stale(project_dir, chunk_id, "translated_text edited by quotation-pass apply")
        if chapter_id not in affected:
            affected.append(chapter_id)
        finished.add(chunk_id)
        # A card is logged once every chunk it touches is saved.
        for key, needed in chunks_of.items():
            if key not in logged and needed <= finished:
                ledger.append(_ledger_row(cards[key], choices[key], placed[key], ACTION_APPLIED, applied_at))
                logged.add(key)
        print(f"[apply] {chunk_id}: {len(landed)} edit(s) written + archived", file=sys.stderr)
        # Write the ledger as we go, so a kill leaves the record of what landed.
        if ledger:
            append_decisions(project_dir, ledger)
            ledger = []
    if ledger:
        append_decisions(project_dir, ledger)

    tails = list(dict.fromkeys(affected + _pending_tails(project_dir)))
    realigned: list[str] = []
    if not no_realign:
        with contextlib.redirect_stdout(sys.stderr):
            for i, chapter_id in enumerate(tails, start=1):
                print(f"[apply] realigning {chapter_id} ({i}/{len(tails)})", file=sys.stderr)
                recombine_chapter(project_dir, chapter_id)
                realign_chapter(project_dir, chapter_id)
                realigned.append(chapter_id)

    record = write_record(project_dir)
    qboard.unapprove(project_dir)
    return {
        "status": "ok" if not skipped else "partial",
        "cards_applied": len(logged),
        "cards_recorded": sum(1 for r in read_decisions(project_dir) if r.get("ts") == applied_at),
        "edits_archived": archived,
        "skipped": skipped,
        "chapters_edited": affected,
        "chapters_realigned": realigned,
        "chapters_pending_realign": [] if not no_realign else tails,
        "backups": len(backups),
        "record": {k: record.get(k) for k in ("verses_unique", "verse_occurrences", "share", "within_limits")},
        "record_path": str(project_dir / "quotations.json"),
    }


def _pending_tails(project_dir: Path) -> list[str]:
    """Chapters with applied cards whose alignment is older than their chunks."""
    chapters = {r["chapter_id"] for r in applied_cards(project_dir).values() if r.get("edits")}
    return [c for c in sorted(chapters) if alignment_is_stale(project_dir, c)]


# ── verify ────────────────────────────────────────────────────────────────────

def verify(project_dir: Path) -> dict[str, Any]:
    """Every applied quotation's text must still be in its live chunk.

    Catches a later retranslation or reader edit that overwrote one.
    """
    from src.utils.file_io import load_chunk

    project_dir = Path(project_dir)
    texts: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    checked = 0
    for key, row in applied_cards(project_dir).items():
        for edit in row.get("edits") or []:
            if edit.get("what") not in ("quote", "paste"):
                continue
            chunk_id = edit.get("chunk_id")
            if chunk_id is None:
                continue
            if chunk_id not in texts:
                path = project_dir / "chunks" / f"{chunk_id}.json"
                texts[chunk_id] = (load_chunk(path).translated_text or "") if path.exists() else ""
            checked += 1
            if edit["new"] not in texts[chunk_id]:
                missing.append({"key": key, "chunk_id": chunk_id, "expected": edit["new"]})
    return {
        "status": "ok" if not missing else "drift",
        "checked": checked,
        "missing": missing,
    }
