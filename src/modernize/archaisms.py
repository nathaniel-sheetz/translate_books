"""Apply a modernize book's archaism list to the modernizer's output.

The list (``<project>/archaisms.json``, built by the pilot's ``book_run.py``)
is applied **after** the modernizer, never shown to it: a partial list in the
prompt switched off the model's own judgment (pilot 2026-11-19). Each entry
has a ``mode``:

* ``literal``: swapped here by code, at commit. The old form is matched as
  whole words, its first letter in either case (the text's case is kept),
  any run of spaces, and longest entry first, so ``es menester`` wins over
  ``menester``. One pass: a replacement is never matched again, and a match
  that already sits inside its replacement (``cuanto a`` in ``En cuanto a``)
  is left alone.
* ``guided``: needs a model, one occurrence at a time, to fit the sentence or
  to decide whether the sense applies (``scope: only``). :func:`guided_hits`
  finds them; the harness's ``archaisms`` commands do the rest.

Old forms are spelling-fixed before matching, since the text they are matched
against has been. Verse (a paragraph of several lines) is left alone: it gets
spelling fixes only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from src.modernize.orthography import modernize_spelling

LIST_NAME = "archaisms.json"
LOG_NAME = "archaism_fixes.jsonl"
_W = r"[^\W\d_]"


@dataclass(frozen=True)
class Entry:
    id: str
    old: str
    replacement: str
    mode: str
    scope: str
    only_when: str
    why: str

    @classmethod
    def from_json(cls, d: dict) -> "Entry":
        return cls(
            id=str(d.get("id", "")),
            old=modernize_spelling(str(d["old"])).strip(),
            replacement=str(d.get("replacement", "")).strip(),
            mode=str(d.get("mode", "literal")),
            scope=str(d.get("scope", "all")),
            only_when=str(d.get("only_when", "")),
            why=str(d.get("why", "")),
        )


def load_list(project_dir: Path) -> list[Entry]:
    """The book's archaism entries; empty when it has no list."""
    path = Path(project_dir) / LIST_NAME
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("entries", []) if isinstance(doc, dict) else doc
    return [Entry.from_json(r) for r in rows if r.get("old") and r.get("replacement")]


def _pattern(old: str) -> str:
    first, rest = old[0], old[1:]
    head = f"[{re.escape(first.lower())}{re.escape(first.upper())}]" if first.isalpha() else re.escape(first)
    body = r"\s+".join(re.escape(part) for part in rest.split(" ")) if rest else ""
    # The split above keeps a leading space of ``rest`` as an empty first part.
    return rf"(?<!{_W}){head}{body}(?!{_W})"


def _compile(entries: Iterable[Entry]) -> tuple[Optional[re.Pattern], list[Entry]]:
    ordered = sorted(entries, key=lambda e: len(e.old), reverse=True)
    if not ordered:
        return None, []
    alts = "|".join(f"(?P<e{i}>{_pattern(e.old)})" for i, e in enumerate(ordered))
    return re.compile(alts), ordered


def _match_case(matched: str, repl: str) -> str:
    if not repl:
        return repl
    if matched[:1].isupper():
        return repl[0].upper() + repl[1:]
    if matched[:1].islower():
        return repl[0].lower() + repl[1:]
    return repl


def _is_verse(paragraph: str) -> bool:
    return "\n" in paragraph.strip()


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for m in re.finditer(r"\n\s*\n", text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    return spans


def apply_literal(text: str, entries: Iterable[Entry]) -> tuple[str, list[dict]]:
    """Swap every literal, ``scope: all`` entry in ``text``; return it and the changes."""
    rx, ordered = _compile(e for e in entries if e.mode == "literal" and e.scope == "all")
    if rx is None:
        return text, []
    changes: list[dict] = []
    out: list[str] = []
    last = 0
    for start, end in _paragraph_spans(text):
        para = text[start:end]
        out.append(text[last:start])
        last = end
        if _is_verse(para):
            out.append(para)
            continue

        def swap(m: re.Match, para: str = para) -> str:
            entry = ordered[int(m.lastgroup[1:])]
            if _already_replaced(para, m.start(), m.end(), entry):
                # The modernizer already wrote the replacement: «En cuanto a su
                # madre» must not become «En en cuanto a su madre».
                return m.group(0)
            new = _match_case(m.group(0), entry.replacement)
            changes.append({
                "id": entry.id, "old": m.group(0), "new": new,
                "context": para[max(0, m.start() - 60): m.end() + 60],
            })
            return new

        out.append(rx.sub(swap, para))
    out.append(text[last:])
    return "".join(out), changes


def guided_hits(text: str, entries: Iterable[Entry]) -> list[dict]:
    """Occurrences of guided (and ``scope: only``) entries, for a model to settle."""
    pending = [e for e in entries if not (e.mode == "literal" and e.scope == "all")]
    rx, ordered = _compile(pending)
    if rx is None:
        return []
    hits = []
    for start, end in _paragraph_spans(text):
        para = text[start:end]
        if _is_verse(para):
            continue
        for m in rx.finditer(para):
            entry = ordered[int(m.lastgroup[1:])]
            if _already_replaced(para, m.start(), m.end(), entry):
                continue
            hits.append({"id": entry.id, "old": m.group(0), "offset": start + m.start(),
                         "paragraph": para, "paragraph_start": start})
    return hits


def _already_replaced(para: str, start: int, end: int, entry: Entry) -> bool:
    """True when the match sits inside the entry's own replacement («cuanto al» in «En cuanto al»)."""
    if not entry.replacement:
        return False
    for m in re.finditer(_pattern(entry.replacement), para):
        if m.start() <= start and end <= m.end() and (m.start(), m.end()) != (start, end):
            return True
    return False


def log_changes(project_dir: Path, chunk_id: str, changes: list[dict], *, writer: str) -> None:
    if not changes:
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    with (Path(project_dir) / LOG_NAME).open("a", encoding="utf-8") as fh:
        for c in changes:
            fh.write(json.dumps({"at": stamp, "chunk_id": chunk_id, "writer": writer, **c},
                                ensure_ascii=False) + "\n")
