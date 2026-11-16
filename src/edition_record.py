"""What the record shows about an edition: its notes, its hand edits, its source.

The ``book-metadata`` skill has to say what an edition changed, and the site
promises that every kind of change is declared. These three readers do the
mechanical part of finding out, so the agent reads a short table instead of
unpacking an EPUB, scanning hundreds of corrections or diffing two texts by hand:

- ``notes``: every endnote of the built EPUB under the chapter heading the
  reader sees, with the ones that read like memos named;
- ``changes``: the rows of ``corrections_applied.jsonl`` where a number or a
  proper noun changed, which is where a silent correction of the author hides.
  Rows that only restyle a sentence say nothing about the edition;
- ``source_diff``: what differs, word for word, between the text a content
  review read and the project's ``source.txt``, blind to case, punctuation and
  line breaks. A source edited before translation carries changes no later
  record shows.

Nothing here judges a change. Each returns rows for the agent to read.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from src import epub_readback

NOTE_EXCERPT = 120
ROW_EXCERPT = 220
#: Corrections these passes made are declared elsewhere (``quotations.json``), not silent.
DECLARED_SOURCES = ("quotation-pass",)

_NUMBER_RE = re.compile(r"\d+(?:[.,:]\d+)*")
_WORD_RE = re.compile(r"[^\W\d_]+(?:[’'-][^\W\d_]+)*", re.UNICODE)
#: After one of these (or at the start) a capital letter only opens a sentence or a quotation.
_OPENERS = set(".!?…:;«»\"“”‹›()[]—–-_*¿¡")
_DIFF_WORD_RE = re.compile(r"[^\W_]+(?:[’'][^\W_]+)*", re.UNICODE)
#: What ends a unit for the coarse pass of ``source_diff``.
_UNIT_BREAK_RE = re.compile(r"[.!?;:]|\n[ \t]*\n")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── notes ──────────────────────────────────────────────────────────────────

def notes(project_dir: Path, epub: Optional[Path] = None) -> dict[str, Any]:
    """Every endnote of the built EPUB, grouped under the heading the reader sees."""
    epub = Path(epub) if epub else epub_readback.newest_epub(Path(project_dir))
    if not epub or not epub.exists():
        return {"ok": False, "error": "no built EPUB: build one (harness.py epub) first"}
    by_chapter: list[dict[str, Any]] = []
    memos, short = [], []
    all_notes = epub_readback.endnotes(epub)
    for note in all_notes:
        kind = epub_readback.memo_like(note["text"])
        row = {"number": note["number"], "text": _clip(note["text"], NOTE_EXCERPT)}
        if kind:
            row["looks_like"] = kind
            (memos if kind == "memo" else short).append(note["number"])
        if not by_chapter or by_chapter[-1]["chapter"] != note["chapter"]:
            by_chapter.append({"chapter": note["chapter"], "notes": []})
        by_chapter[-1]["notes"].append(row)
    return {
        "ok": True,
        "epub": str(epub),
        "count": len(all_notes),
        "chapters_with_notes": len(by_chapter),
        "memo_like": memos,
        "short_unpunctuated": short,
        "by_chapter": by_chapter,
    }


# ── hand edits ─────────────────────────────────────────────────────────────

def _proper_nouns(text: str) -> Counter:
    """Capitalized words that do not open a sentence or a quotation: names, places, titles."""
    found: Counter = Counter()
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        if not word[0].isupper() or len(word) < 2:
            continue
        before = text[:match.start()].rstrip()
        if not before or before[-1] in _OPENERS:
            continue
        found[word] += 1
    return found


def _delta(before: Counter, after: Counter) -> tuple[list[str], list[str]]:
    removed = sorted((before - after).elements())
    added = sorted((after - before).elements())
    return removed, added


def _name_delta(before: str, after: str) -> tuple[list[str], list[str]]:
    """The proper nouns one sentence has and the other lacks.

    A word the other sentence has in any case is not a changed name: «él» made
    «Él» is a capital of reverence, and a word moved to open the sentence only
    gained its capital there.
    """
    removed, added = _delta(_proper_nouns(before), _proper_nouns(after))
    before_words = {w.casefold() for w in _WORD_RE.findall(before)}
    after_words = {w.casefold() for w in _WORD_RE.findall(after)}
    return ([w for w in removed if w.casefold() not in after_words],
            [w for w in added if w.casefold() not in before_words])


def changes(project_dir: Path) -> dict[str, Any]:
    """The hand edits that changed a number or a proper noun.

    ``rows`` are in book order. ``left_out`` counts what was not listed and why:
    skipped rows, rows a declared pass made, and rows that changed neither.
    """
    log = Path(project_dir) / "corrections_applied.jsonl"
    if not log.exists():
        return {"ok": True, "total": 0, "listed": 0, "rows": [], "left_out": {},
                "note": "no corrections_applied.jsonl: no hand edit was recorded"}
    rows: list[dict[str, Any]] = []
    left_out: Counter = Counter()
    total = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            left_out["unreadable"] += 1
            continue
        if not isinstance(row, dict):
            continue
        total += 1
        if row.get("status") == "skipped":
            left_out["skipped"] += 1
            continue
        source = str(row.get("source") or "").split(":")[0]
        if source in DECLARED_SOURCES:
            left_out[source] += 1
            continue
        before, after = str(row.get("original_es") or ""), str(row.get("corrected_es") or "")
        numbers = _delta(Counter(_NUMBER_RE.findall(before)), Counter(_NUMBER_RE.findall(after)))
        names = _name_delta(before, after)
        kinds = [k for k, d in (("numbers", numbers), ("names", names)) if d[0] or d[1]]
        if not kinds:
            left_out["style only"] += 1
            continue
        rows.append({
            "chapter": row.get("chapter_id"),
            "es_idx": row.get("es_idx"),
            "by": source or "reader",
            "changed": kinds,
            "removed": numbers[0] + names[0],
            "added": numbers[1] + names[1],
            "before": _clip(before, ROW_EXCERPT),
            "after": _clip(after, ROW_EXCERPT),
            "english": _clip(row.get("en_reference") or "", ROW_EXCERPT),
        })
    rows.sort(key=lambda r: (str(r["chapter"]), r["es_idx"] if isinstance(r["es_idx"], int) else 0))
    return {"ok": True, "total": total, "listed": len(rows), "left_out": dict(left_out),
            "rows": rows}


# ── source diff ────────────────────────────────────────────────────────────

def _tokens(text: str) -> tuple[list[str], list[str], list[int], list[int]]:
    """``(shown, folded, line of each, unit starts)`` for the words of ``text``.

    A word is a run of letters or digits (an apostrophe inside it kept), so
    «is.—Heb.» is two words and punctuation is never one. ``unit starts`` are
    the indexes where a sentence-like unit begins: after ``.!?;:`` or a blank
    line, so line wrapping never moves a boundary.
    """
    shown, folded, lines, starts = [], [], [], []
    matches = list(_DIFF_WORD_RE.finditer(text))
    line = 1
    previous_end = 0
    for match in matches:
        gap = text[previous_end:match.start()]
        line += gap.count("\n")
        if not shown or _UNIT_BREAK_RE.search(gap):
            starts.append(len(shown))
        shown.append(match.group(0))
        folded.append(match.group(0).casefold())
        lines.append(line)
        previous_end = match.end()
    return shown, folded, lines, starts


def _word_opcodes(old: list[str], new: list[str], old_starts: list[int],
                  new_starts: list[int]):
    """Word-level opcodes of ``old`` → ``new``, without the ``equal`` ones.

    One ``SequenceMatcher`` over a whole book's words takes minutes (common
    words make its candidate lists huge), so the units are matched first and
    only the spans of units that differ are compared word by word.
    """
    def units(words, starts):
        bounds = starts + [len(words)]
        return [tuple(words[bounds[k]:bounds[k + 1]]) for k in range(len(starts))]

    old_bounds, new_bounds = old_starts + [len(old)], new_starts + [len(new)]
    coarse = difflib.SequenceMatcher(None, units(old, old_starts), units(new, new_starts),
                                     autojunk=False)
    for tag, a1, a2, b1, b2 in coarse.get_opcodes():
        if tag == "equal":
            continue
        i_lo, i_hi = old_bounds[a1], old_bounds[a2]
        j_lo, j_hi = new_bounds[b1], new_bounds[b2]
        fine = difflib.SequenceMatcher(None, old[i_lo:i_hi], new[j_lo:j_hi], autojunk=False)
        for ftag, i1, i2, j1, j2 in fine.get_opcodes():
            if ftag != "equal":
                yield ftag, i_lo + i1, i_lo + i2, j_lo + j1, j_lo + j2


def _size_band(words: int) -> str:
    return "1-2 words" if words <= 2 else ("3-9 words" if words <= 9 else "10+ words")


def source_diff(project_dir: Path, against: Path, *, max_hunks: int = 80,
                min_words: int = 1, context_words: int = 6) -> dict[str, Any]:
    """What ``source.txt`` changed against ``against``, the text a review was written on.

    Compared word by word, blind to case, punctuation and line breaks, so a
    re-wrapped or re-punctuated paragraph is not a difference. Each hunk gives
    the words removed and added with a few words of context and the line in
    each file. ``by_size`` counts every hunk by how many words it touches;
    ``hunks`` lists those of at least ``min_words``, in book order, capped at
    ``max_hunks``. A scan's small fixes are many and a rewritten paragraph is
    one large hunk, so raising ``min_words`` is how to find the rewrites.
    """
    source = Path(project_dir) / "source.txt"
    against = Path(against)
    for path in (source, against):
        if not path.exists():
            return {"ok": False, "error": f"not found: {path}"}
    old_shown, old_folded, old_lines, old_starts = _tokens(
        against.read_text(encoding="utf-8", errors="replace"))
    new_shown, new_folded, new_lines, new_starts = _tokens(
        source.read_text(encoding="utf-8", errors="replace"))
    hunks: list[dict[str, Any]] = []
    by_size: Counter = Counter()
    removed_words = added_words = hunk_count = matching = 0
    for tag, i1, i2, j1, j2 in _word_opcodes(old_folded, new_folded, old_starts, new_starts):
        hunk_count += 1
        removed_words += i2 - i1
        added_words += j2 - j1
        size = max(i2 - i1, j2 - j1)
        by_size[_size_band(size)] += 1
        if size < min_words:
            continue
        matching += 1
        if len(hunks) >= max_hunks:
            continue
        hunks.append({
            "kind": {"delete": "removed", "insert": "added", "replace": "changed"}[tag],
            "reviewed_line": old_lines[min(i1, len(old_lines) - 1)] if old_lines else None,
            "source_line": new_lines[min(j1, len(new_lines) - 1)] if new_lines else None,
            "context": " ".join(old_shown[max(0, i1 - context_words):i1]),
            "removed": _clip(" ".join(old_shown[i1:i2]), 400),
            "added": _clip(" ".join(new_shown[j1:j2]), 400),
        })
    return {
        "ok": True,
        "reviewed": str(against),
        "source": str(source),
        "reviewed_words": len(old_folded),
        "source_words": len(new_folded),
        "removed_words": removed_words,
        "added_words": added_words,
        "hunk_count": hunk_count,
        "by_size": dict(by_size),
        "min_words": min_words,
        "hunks": hunks,
        "truncated": matching > len(hunks),
    }
