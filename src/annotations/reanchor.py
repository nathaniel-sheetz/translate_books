"""
Keep reader annotations on their sentence when a chapter is realigned.

An annotation is stored under ``(chapter_id, es_idx, sub_id)``, and ``es_idx`` is
a position: add or remove one sentence and every row after it is renumbered. The
note's ``es_text`` snapshot is the only thing that says which sentence it was
written on, so this pass finds each note's row from that text rather than from a
diff of the alignment before and after.

That matters because a diff only works at the realign where the numbers move. A
path that realigned without re-anchoring used to leave the notes behind for good:
the next pass read the already-shifted alignment as "before", found the stale
number still pointing at *a* sentence, and concluded nothing had moved. Working
from the snapshot, any later realign puts the note back.

Notes saved before snapshots existed have no ``es_text``; for those the caller's
``old_es_map`` (the alignment as it stood before this realign) supplies the text,
and the note is given a snapshot once its row is confirmed.

Deliberately free of any ``web_ui`` import, like :mod:`src.annotations.store`, so
the judges, the harness and the CLI scripts can all call it.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.annotations import store
from src.annotations.anchors import parse_anchors

logger = logging.getLogger(__name__)

# A row that is only an image placeholder renders no sentence, so a note can
# neither sit on it nor be moved to it. Same pattern the reader filters on
# (web_ui/app.py:_IMAGE_PLACEHOLDER_RE).
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]")

# The opening run that must survive for an edited sentence to count as the same
# one, and the length below which a text is too short to match on anything but
# equality ("—Sí." is inside half the rows of a dialogue).
PREFIX_CHARS = 30
LOOSE_MIN_CHARS = 12

# Fuzzy matching is the last resort for a sentence that was reworded. It needs
# enough text to be distinctive, a high ratio, and a clear winner.
FUZZY_MIN_CHARS = 25
FUZZY_MIN_RATIO = 0.75
FUZZY_MIN_LEAD = 0.05

# Hearts on notes (web_ui/favorites.py) are stored beside annotations.jsonl.
FAVORITES_FILENAME = "favorites.jsonl"


@dataclass
class Move:
    """One note that belongs on a different row than the one it is stored on."""

    record: dict
    old_idx: int
    new_idx: int
    tier: str
    new_text: str


@dataclass
class ReanchorResult:
    """What a re-anchor pass found, and (unless it was a dry run) wrote."""

    moved: list[Move] = field(default_factory=list)
    # Notes whose sentence is nowhere in the new alignment.
    orphaned: list[dict] = field(default_factory=list)
    # Notes confirmed in place that had no snapshot and were given one.
    backfilled: list[dict] = field(default_factory=list)
    # Notes confirmed in place whose sentence was reworded; snapshot updated.
    refreshed: list[dict] = field(default_factory=list)
    # Notes with no snapshot and no prior alignment to read one from. Their
    # number still resolves, so nothing can be disproved and they are left alone.
    unverified: list[dict] = field(default_factory=list)
    # Notes with no snapshot whose own ``[bracketed]`` word is not in the row
    # they sit on. Left alone, and not given a snapshot that would hide the doubt.
    suspect: list[dict] = field(default_factory=list)
    kept: int = 0
    # Moved notes whose heart was taken along; set once the plan is written.
    hearts_moved: int = 0


def as_es_idx(value: Any) -> Optional[int]:
    """Coerce a stored/alignment ``es_idx`` to int; ``None`` if it isn't one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_alignment_rows(project_dir: Path, chapter_id: str) -> Optional[list[dict]]:
    """The chapter's alignment rows, or ``None`` when there is no readable file."""
    align_path = Path(project_dir) / "alignments" / f"{chapter_id}.json"
    if not align_path.exists():
        return None
    try:
        with open(align_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    rows = data.get("alignments") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [a for a in rows if isinstance(a, dict)]


def load_es_map(project_dir: Path, chapter_id: str) -> dict[int, str]:
    """Load ``{es_idx: es_text}`` for a chapter's current alignment, or ``{}``."""
    result: dict[int, str] = {}
    for a in load_alignment_rows(project_dir, chapter_id) or []:
        idx = as_es_idx(a.get("es_idx"))
        es_text = a.get("es")
        if idx is not None and isinstance(es_text, str):
            result[idx] = es_text
    return result


def _es_heads(rows: list[dict]) -> dict[int, int]:
    """``{es_idx: its row's es_idx}`` for every later member of a merged row."""
    result: dict[int, int] = {}
    for a in rows:
        head = as_es_idx(a.get("es_idx"))
        members = a.get("es_indices")
        if head is None or not isinstance(members, list):
            continue
        for member in members:
            member = as_es_idx(member)
            if member is not None and member != head:
                result[member] = head
    return result


def _nearest(candidates: list[int], expected: int) -> int:
    return min(candidates, key=lambda idx: (abs(idx - expected), idx))


def _locate_certain(
    text: str, old_idx: int, es_map: dict[int, str], heads: dict[int, int],
) -> Optional[tuple[int, str]]:
    """The row a note is on when there is only one answer, else ``None``."""
    row = es_map.get(old_idx)
    if row is not None and (row == text or text in row):
        return old_idx, "in_place"
    # The sentence kept its number but its row did not: the aligner shows it as
    # a later part of the row before it (a speech tag glued to its line).
    # Checked before the text tiers, which would send "—dijo—." to the first
    # row that reads the same, and required to hold on both number and text.
    head = heads.get(old_idx)
    if head is not None and text in es_map.get(head, ""):
        return head, "joined_row"
    exact = [idx for idx, row_text in es_map.items() if row_text == text]
    if len(exact) == 1:
        return exact[0], "exact"
    return None


def _locate_loose(
    text: str, expected: int, es_map: dict[int, str],
) -> Optional[tuple[int, str]]:
    """The best row for a note whose sentence is repeated, merged, split or
    edited. ``expected`` is where the note should be if it moved the same
    distance as the notes around it."""
    exact = [idx for idx, row_text in es_map.items() if row_text == text]
    if exact:
        return _nearest(exact, expected), "exact_nearest"

    inside = [idx for idx, row_text in es_map.items() if text in row_text]
    if inside and (len(inside) == 1 or len(text) >= LOOSE_MIN_CHARS):
        return _nearest(inside, expected), "merged_row"

    if len(text) >= PREFIX_CHARS:
        prefix = text[:PREFIX_CHARS]
        starts = [idx for idx, row_text in es_map.items() if row_text.startswith(prefix)]
        if starts:
            return _nearest(starts, expected), "prefix"

    # The sentence was cut in two: the note stays with its opening half.
    halves = [
        idx for idx, row_text in es_map.items()
        if len(row_text) >= LOOSE_MIN_CHARS and text.startswith(row_text)
    ]
    if halves:
        return _nearest(halves, expected), "split"

    if len(text) < FUZZY_MIN_CHARS:
        return None
    best_idx, best, second = None, 0.0, 0.0
    for idx, row_text in es_map.items():
        matcher = difflib.SequenceMatcher(None, text, row_text, autojunk=False)
        if matcher.real_quick_ratio() < second or matcher.quick_ratio() < second:
            continue
        ratio = matcher.ratio()
        if ratio > best:
            best_idx, best, second = idx, ratio, best
        elif ratio > second:
            second = ratio
    if best_idx is not None and best >= FUZZY_MIN_RATIO and best - second >= FUZZY_MIN_LEAD:
        return best_idx, "fuzzy"
    return None


def _shift_near(old_idx: int, anchors: list[tuple[int, int]]) -> int:
    """How far the closest already-placed note moved; 0 when none has been."""
    if not anchors:
        return 0
    near_old, near_new = min(anchors, key=lambda a: (abs(a[0] - old_idx), a[0]))
    return near_new - near_old


def plan_chapter(
    project_dir: Path,
    chapter_id: str,
    old_es_map: Optional[dict[int, str]] = None,
) -> ReanchorResult:
    """Work out where every live note in a chapter belongs. Writes nothing.

    Args:
        project_dir: ``projects/<slug>/``.
        chapter_id: The chapter whose alignment was just rewritten.
        old_es_map: ``{es_idx: es_text}`` of the alignment before the realign.
            Only read for notes that carry no ``es_text`` snapshot of their own.
    """
    result = ReanchorResult()
    records = store.load_active(project_dir, chapter_id=chapter_id)
    if not records:
        return result
    rows = load_alignment_rows(project_dir, chapter_id)
    if rows is None:
        # No alignment to check against: nothing can be disproved.
        return result

    es_map: dict[int, str] = {}
    for a in rows:
        idx = as_es_idx(a.get("es_idx"))
        es_text = a.get("es")
        if idx is None or not isinstance(es_text, str):
            continue
        if _IMAGE_PLACEHOLDER_RE.fullmatch(es_text.strip()):
            continue
        es_map[idx] = es_text
    heads = _es_heads(rows)
    old_es_map = old_es_map or {}

    placed: list[tuple[dict, int, str, tuple[int, str]]] = []
    pending: list[tuple[dict, int, str]] = []
    # (old number, new number) of every note placed without doubt, used to
    # choose between candidates for the notes that are not.
    anchors: list[tuple[int, int]] = []

    for record in records:
        old_idx = as_es_idx(record.get("es_idx"))
        if old_idx is None:
            result.unverified.append(record)
            continue
        snapshot = record.get("es_text")
        text = snapshot if isinstance(snapshot, str) and snapshot else old_es_map.get(old_idx)
        if not text:
            (result.unverified if old_idx in es_map else result.orphaned).append(record)
            continue
        hit = _locate_certain(text, old_idx, es_map, heads)
        if hit is None:
            pending.append((record, old_idx, text))
            continue
        anchors.append((old_idx, hit[0]))
        placed.append((record, old_idx, text, hit))

    for record, old_idx, text in pending:
        hit = _locate_loose(text, old_idx + _shift_near(old_idx, anchors), es_map)
        if hit is None:
            result.orphaned.append(record)
            continue
        placed.append((record, old_idx, text, hit))

    for record, old_idx, text, (new_idx, tier) in placed:
        new_row = es_map[new_idx]
        # Keep the note's own sentence as its snapshot while the row still
        # holds it (a merged row is wider than the sentence, and may be cut
        # apart again). Take the row's text only when the sentence itself changed.
        new_text = text if text in new_row else new_row
        has_snapshot = bool(record.get("es_text"))
        if not has_snapshot:
            words = parse_anchors(record.get("content") or "")
            row_folded = new_row.casefold()
            if words and not any(word.casefold() in row_folded for word in words):
                # The note names a word this row does not have. It may have
                # drifted before this pass could see it; do not vouch for it.
                result.suspect.append(record)
                new_text = ""
        if new_idx != old_idx:
            result.moved.append(Move(record, old_idx, new_idx, tier, new_text))
            continue
        result.kept += 1
        if not has_snapshot and new_text:
            result.backfilled.append({**record, "es_text": new_text})
        elif has_snapshot and new_text != record["es_text"]:
            # The sentence was edited where it stands. Follow it, so the next
            # edit is matched against this wording and not the one before it.
            result.refreshed.append({**record, "es_text": new_text})
    return result


def _rows_to_append(
    project_dir: Path, chapter_id: str, result: ReanchorResult, ts: str,
) -> tuple[list[dict], list[dict]]:
    """The jsonl rows that carry out a plan's moves: ``(tombstones, notes)``,
    one of each per move and in the same order, to be written in that order.

    Tombstones go first because two notes can trade places in one pass (73 → 79
    while 79 → 85). Written pair by pair, the second note's tombstone would land
    after the first note's new row and delete it.
    """
    # The slots still held once every mover has left its old one.
    taken = {
        (as_es_idx(rec.get("es_idx")), store.storage_sub_id(rec.get("sub_id")))
        for rec in store.load_active(project_dir, chapter_id=chapter_id)
    }
    for move in result.moved:
        taken.discard((move.old_idx, store.storage_sub_id(move.record.get("sub_id"))))

    tombstones: list[dict] = []
    recreated: list[dict] = []
    for move in result.moved:
        record = move.record
        sub = store.storage_sub_id(record.get("sub_id"))
        tombstone = {
            "project_id": record.get("project_id"),
            "chapter_id": chapter_id,
            "es_idx": record.get("es_idx"),
            "removed": True,
            "timestamp": ts,
        }
        if record.get("sub_id") is not None:
            tombstone["sub_id"] = record["sub_id"]
        tombstones.append(tombstone)

        # Two notes that end up on one row under the same sub_id (two legacy
        # notes whose rows merged) would overwrite each other. Give the
        # arriving one an id of its own rather than lose it.
        new_sub = sub
        while (move.new_idx, new_sub) in taken:
            new_sub = "u" + secrets.token_hex(4)
        taken.add((move.new_idx, new_sub))

        # Carry the whole record, so provenance, verified_by and the
        # annotation-review sidecar all survive the move.
        row = dict(record)
        row["es_idx"] = move.new_idx
        row["timestamp"] = ts
        if new_sub != sub:
            row["sub_id"] = new_sub
        if move.new_text:
            row["es_text"] = move.new_text
        recreated.append(row)
    return tombstones, recreated


def _heart_rows(
    project_dir: Path, moves: list[tuple[dict, dict]], ts: str,
) -> list[dict]:
    """The ``favorites.jsonl`` rows that take a heart along with its note.

    A favorite is stored under an id that includes the note's ``es_idx``
    (:func:`store.favorite_id`), so a moved note would leave its heart behind
    on the old number. ``moves`` pairs each note as it was with the row that
    replaces it. Every unfavorite comes before every favorite, for the reason
    tombstones come first: two hearted notes can trade rows in one pass.

    The file belongs to ``web_ui/favorites.py``; its format is repeated here
    (append-only, last record for an id wins) because ``src`` does not import
    ``web_ui``.
    """
    path = Path(project_dir) / FAVORITES_FILENAME
    if not moves or not path.exists():
        return []
    standing: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            standing[record["id"]] = record

    off: list[dict] = []
    on: list[dict] = []
    for old, new in moves:
        old_id = store.favorite_id(old)
        heart = standing.get(old_id)
        if not heart or not heart.get("favorite"):
            continue
        off.append({"ts": ts, "id": old_id, "favorite": False})
        row = {"ts": ts, "id": store.favorite_id(new), "favorite": True}
        if isinstance(heart.get("snapshot"), dict):
            row["snapshot"] = heart["snapshot"]
        on.append(row)
    return off + on


def reanchor_chapter(
    project_dir: Path,
    chapter_id: str,
    old_es_map: Optional[dict[int, str]] = None,
    *,
    dry_run: bool = False,
) -> ReanchorResult:
    """Move a chapter's notes onto the rows their sentences now occupy.

    Call it after every write of ``alignments/<chapter_id>.json``. Appends
    tombstone + recreate rows to ``annotations.jsonl`` for notes that moved, and
    a replacement row for notes whose snapshot was added or updated. A moved
    note's heart in ``favorites.jsonl`` moves with it. Never rewrites either
    file, so the prior state stays on disk.

    Returns:
        The plan that was carried out. ``orphaned`` lists the notes whose
        sentence is no longer in the chapter; they are left untouched and the
        reader's overflow bin shows them.
    """
    result = plan_chapter(project_dir, chapter_id, old_es_map)
    if not dry_run:
        apply_plan(project_dir, chapter_id, result)
    return result


def apply_plan(project_dir: Path, chapter_id: str, result: ReanchorResult) -> int:
    """Write a plan from :func:`plan_chapter`; returns the number of rows appended.

    The plan must be fresh: it is carried out against the file as it stands now.
    """
    if not (result.moved or result.backfilled or result.refreshed):
        return 0
    ts = datetime.now().isoformat()
    tombstones, recreated = _rows_to_append(project_dir, chapter_id, result, ts)
    # Worked out before the notes move, written after: a heart must not leave
    # for a row its note never reached.
    records = [move.record for move in result.moved]
    hearts = _heart_rows(project_dir, list(zip(records, recreated)), ts)
    rows = tombstones + recreated + result.backfilled + result.refreshed
    store.append_records(project_dir, rows)
    if hearts:
        with open(Path(project_dir) / FAVORITES_FILENAME, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in hearts))
    result.hearts_moved = len(hearts) // 2
    logger.info(
        "re-anchored %s/%s: %d moved (%d hearted), %d orphaned, %d given a snapshot",
        Path(project_dir).name, chapter_id, len(result.moved), result.hearts_moved,
        len(result.orphaned), len(result.backfilled),
    )
    return len(rows)


def reanchor_chapter_quietly(
    project_dir: Path,
    chapter_id: str,
    old_es_map: Optional[dict[int, str]] = None,
) -> Optional[ReanchorResult]:
    """:func:`reanchor_chapter` for callers whose realign must not fail on it.

    The alignment is already written by the time this runs; a failure here
    leaves the notes where they were, which the next realign repairs.
    """
    try:
        return reanchor_chapter(project_dir, chapter_id, old_es_map)
    except Exception as exc:  # noqa: BLE001 - never fail a realign over its notes
        logger.warning(
            "annotation re-anchor failed for %s/%s: %s",
            Path(project_dir).name, chapter_id, exc,
        )
        return None
