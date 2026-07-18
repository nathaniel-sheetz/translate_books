"""
Import Project Gutenberg footnotes as editable reader footnote annotations.

Gutenberg books express footnotes as an inline reference anchor in the body
(``<a href="#frag">[1]</a>``) linked to a definition elsewhere whose ``id``/
``name`` equals ``frag`` and which usually back-links to the reference. The two
common conventions differ only in class names, ``id`` vs ``name`` anchoring, and
whether the definitions are interspersed or collected at the end; both reduce to
that one structure, so the detector keys on the *structure*, not on class names.

Pipeline (see ``docs`` / plan):

1.  At ingest, :func:`find_footnotes` locates ref/def pairs on the parsed soup.
    :func:`apply_import` replaces each reference with a survivable
    ``[FOOTNOTE:N]`` token (the same strategy as ``[IMAGE:...]``) and removes the
    definition blocks; the source note bodies are written to ``footnotes.json``.
    :func:`apply_drop` is the opt-out: it removes references and definitions
    cleanly, leaving no ``[1]`` residue.
2.  The tokens ride through translation verbatim.
3.  The note bodies are translated (whole-book, on demand) into
    ``footnotes.json``'s ``translated_body``.
4.  After alignment, :func:`convert_chapter_footnotes` turns each surviving
    ``[FOOTNOTE:N]`` token into a ``type:"footnote"`` record in
    ``annotations.jsonl`` (with a verbatim ``[anchor]`` derived from the
    preceding words, and a stable ``sub_id`` so several notes can share one
    aligned sentence). From there the *existing* endnote machinery
    (``src/endnotes.py`` + ``src/epub_builder.py``) renders them into the EPUB
    and the reader displays them as editable annotations.

Only :func:`find_footnotes` / :func:`apply_import` / :func:`apply_drop` need
BeautifulSoup; the conversion + sidecar helpers are pure text/JSON so they can
run without ``bs4`` installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.utils.text_utils import strip_footnote_tokens

# ---------------------------------------------------------------------------
# Detection tuning
# ---------------------------------------------------------------------------

# Block tags that most tightly wrap a single note definition (preferred).
_NOTE_BLOCK_TAGS = {"p", "li", "dd"}
# Coarser containers to fall back to when a note is not wrapped in a paragraph.
_FALLBACK_BLOCK_TAGS = {"div", "td", "blockquote", "section", "aside"}
# CSS classes that independently mark an element as footnote-ish.
_FOOTNOTE_CLASS_HINTS = {
    "footnote", "footnotes", "endnote", "endnotes", "fn", "fnote", "note", "notes",
}
# A footnote reference marker's visible text: a small number (optionally
# bracketed/parenthesised) or one-to-three footnote symbols.
_MARKER_RE = re.compile(r"^[\[\(]?\s*(?:\d{1,4}|[*†‡§¶#]{1,3})\s*[\]\).]?$")
# Leading standalone marker to trim off an extracted note body ("[1] ", "1. ").
_LEADING_MARKER_RE = re.compile(r"^\s*[\[\(]?\s*(?:\d{1,4}|[*†‡§¶#]{1,3})\s*[\]\).]?\s+")


def _looks_like_marker(text: str) -> bool:
    return bool(text) and len(text) <= 8 and bool(_MARKER_RE.match(text.strip()))


# ---------------------------------------------------------------------------
# Records / sidecar
# ---------------------------------------------------------------------------

@dataclass
class FootnoteRecord:
    """One imported footnote, persisted to ``footnotes.json``."""
    number: int
    ref_marker: str
    source_body: str
    detected: str
    translated_body: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "ref_marker": self.ref_marker,
            "source_body": self.source_body,
            "translated_body": self.translated_body,
            "detected": self.detected,
        }


@dataclass
class FootnoteMatch:
    """A detected ref/def pair, holding the soup nodes to mutate at apply time."""
    number: int
    ref_marker: str
    source_body: str
    detected: str
    ref: object = field(repr=False, default=None)          # the visible <a href="#..."> reference
    landing: object = field(repr=False, default=None)       # empty landing anchor by the reference
    def_block: object = field(repr=False, default=None)     # the definition block to remove

    def to_record(self) -> FootnoteRecord:
        return FootnoteRecord(
            number=self.number,
            ref_marker=self.ref_marker,
            source_body=self.source_body,
            detected=self.detected,
        )


def footnotes_sidecar_path(project_path) -> Path:
    return Path(project_path) / "footnotes.json"


def write_footnotes_sidecar(project_path, records: List[FootnoteRecord]) -> Path:
    path = footnotes_sidecar_path(project_path)
    data = [r.to_dict() for r in records]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_footnotes_sidecar(project_path) -> List[dict]:
    path = footnotes_sidecar_path(project_path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Detection (BeautifulSoup)
# ---------------------------------------------------------------------------

def _anchor_maps(root) -> Tuple[dict, dict]:
    """Return ``(id_map, name_map)`` of every id/name to its element (first wins)."""
    id_map: dict = {}
    name_map: dict = {}
    for el in root.find_all(True):
        i = el.get("id")
        n = el.get("name")
        if i and i not in id_map:
            id_map[i] = el
        if n and n not in name_map:
            name_map[n] = el
    return id_map, name_map


def _classes(node) -> set:
    try:
        return set(node.get("class") or [])
    except AttributeError:
        return set()


def _within_note_block(node) -> bool:
    """True when *node* sits inside an element flagged as footnote-ish by class."""
    cur = node
    for _ in range(8):
        if cur is None:
            return False
        if _classes(cur) & _FOOTNOTE_CLASS_HINTS:
            return True
        cur = getattr(cur, "parent", None)
    return False


def _note_block(target):
    """The block that holds the note text: nearest paragraph-ish ancestor of the
    definition anchor, else nearest coarse container, else the anchor's parent."""
    cur = getattr(target, "parent", None)
    fallback = None
    for _ in range(8):
        if cur is None:
            break
        name = (getattr(cur, "name", "") or "").lower()
        if name in _NOTE_BLOCK_TAGS:
            return cur
        if fallback is None and name in _FALLBACK_BLOCK_TAGS:
            fallback = cur
        cur = getattr(cur, "parent", None)
    return fallback or getattr(target, "parent", None)


def _landing_anchor(ref):
    """Return ``(refid, landing_node)`` for the empty anchor just before *ref*.

    Both conventions place an empty ``<a id=…>``/``<a name=…>`` immediately
    before the visible reference; its id/name is what the definition back-links
    to. Falls back to the reference's own id/name when there is no such sibling.
    """
    from bs4 import NavigableString, Tag  # lazy: bs4 only needed for detection

    self_id = ref.get("id") or ref.get("name")
    for sib in ref.previous_siblings:
        if isinstance(sib, NavigableString):
            if sib.strip():
                break  # real text before the ref: no landing anchor
            continue
        if isinstance(sib, Tag):
            if sib.name == "a" and not sib.get_text(strip=True) and (sib.get("id") or sib.get("name")):
                return (sib.get("id") or sib.get("name")), sib
            break
    return self_id, None


def _classify(def_block, refid, ref, id_map, name_map) -> Tuple[Optional[str], object]:
    """Confirm *def_block* is a real footnote definition.

    Returns ``(signal, backlink_node)`` — ``signal`` is the reason it qualified
    (or ``None``), and ``backlink_node`` is the definition's back-reference
    anchor when there is one, so the caller can consume it and not mistake it
    for a second footnote reference (the ref/def link is symmetric).
    """
    from bs4 import Tag

    if def_block is None:
        return None, None

    # (1) Explicit back-link to the reference's landing id.
    if refid:
        for a in def_block.find_all("a", href=True):
            if a.get("href") == f"#{refid}":
                return "backlink", a

    # (2) Footnote-ish class on the block or a near ancestor.
    cur = def_block
    for _ in range(4):
        if cur is None:
            break
        if _classes(cur) & _FOOTNOTE_CLASS_HINTS:
            return "class", None
        cur = getattr(cur, "parent", None)

    # (3) Loose back-link: the block points at a body element (near the ref),
    #     which is the hallmark of a note even without known classes.
    for a in def_block.find_all("a", href=True):
        href = a.get("href") or ""
        if href.startswith("#"):
            t = id_map.get(href[1:]) or name_map.get(href[1:])
            if isinstance(t, Tag) and not _within_note_block(t):
                return "backlink-loose", a
    return None, None


def _note_text(block, ref_marker: str, refid: Optional[str]) -> str:
    """Extract a definition's note text, dropping marker/anchor cruft.

    Empty anchors (landing/target), the back-link marker, and ``<span
    class="label">`` are skipped; ``<i>``/``<em>`` become ``_underscore_``
    italics so the endnote renderer promotes them to ``<em>``.
    """
    from bs4 import NavigableString, Tag

    def is_marker_anchor(a) -> bool:
        href = a.get("href")
        txt = a.get_text(strip=True)
        if not txt:
            return True  # empty landing/target anchor
        if href and refid and href == f"#{refid}":
            return True  # explicit back-link
        if href and href.startswith("#") and txt == ref_marker:
            return True  # back-link marker matched by text
        return False

    parts: List[str] = []

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                name = (child.name or "").lower()
                if name == "a" and is_marker_anchor(child):
                    continue
                if name == "span" and "label" in _classes(child):
                    continue
                if name in ("i", "em"):
                    inner: List[str] = []
                    _collect(child, inner)
                    t = re.sub(r"\s+", " ", "".join(inner)).strip()
                    if t:
                        parts.append(f"_{t}_")
                    continue
                walk(child)

    def _collect(node, buf):
        for child in node.children:
            if isinstance(child, NavigableString):
                buf.append(str(child))
            elif isinstance(child, Tag):
                _collect(child, buf)

    walk(block)
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    text = _LEADING_MARKER_RE.sub("", text)
    return text.strip()


def find_footnotes(root) -> List[FootnoteMatch]:
    """Detect footnote ref/def pairs under *root*, numbered in document order."""
    id_map, name_map = _anchor_maps(root)
    matches: List[FootnoteMatch] = []
    seen_defs: set = set()
    consumed: set = set()  # back-link anchors already claimed by a detected pair
    number = 0

    for a in root.find_all("a"):
        if id(a) in consumed:
            continue  # this <a> is a definition's back-link, not a new reference
        href = a.get("href")
        if not href or not href.startswith("#"):
            continue
        marker = a.get_text(strip=True)
        if not _looks_like_marker(marker):
            continue
        if _within_note_block(a):
            continue  # this <a> is itself a back-link living inside a note

        frag = href[1:]
        target = id_map.get(frag) or name_map.get(frag)
        if target is None:
            continue

        def_block = _note_block(target)
        if def_block is None or id(def_block) in seen_defs:
            continue

        refid, landing = _landing_anchor(a)
        detected, backlink = _classify(def_block, refid, a, id_map, name_map)
        if not detected:
            continue
        if backlink is not None:
            consumed.add(id(backlink))

        seen_defs.add(id(def_block))
        number += 1
        matches.append(
            FootnoteMatch(
                number=number,
                ref_marker=marker,
                source_body=_note_text(def_block, marker, refid),
                detected=detected,
                ref=a,
                landing=landing,
                def_block=def_block,
            )
        )
    return matches


def apply_import(matches: List[FootnoteMatch]) -> None:
    """Replace each reference with ``[FOOTNOTE:N]`` and remove the definitions."""
    from bs4 import NavigableString

    for m in matches:
        if m.ref is not None and m.ref.parent is not None:
            m.ref.replace_with(NavigableString(f"[FOOTNOTE:{m.number}]"))
        if m.landing is not None and m.landing.parent is not None:
            m.landing.decompose()
        if m.def_block is not None and m.def_block.parent is not None:
            m.def_block.decompose()


def apply_drop(matches: List[FootnoteMatch]) -> None:
    """Remove references and definitions cleanly (no ``[1]`` residue)."""
    for m in matches:
        if m.ref is not None and m.ref.parent is not None:
            m.ref.decompose()
        if m.landing is not None and m.landing.parent is not None:
            m.landing.decompose()
        if m.def_block is not None and m.def_block.parent is not None:
            m.def_block.decompose()


def records_from_matches(matches: List[FootnoteMatch]) -> List[FootnoteRecord]:
    return [m.to_record() for m in matches]


# ---------------------------------------------------------------------------
# Post-translation conversion: [FOOTNOTE:N] -> annotations.jsonl
# ---------------------------------------------------------------------------

def _sentence_spans(clean_text: str, es_map: Dict[int, str]) -> List[Tuple[int, int, int]]:
    """Locate each aligned sentence in *clean_text*: ``[(start, end, es_idx), …]``.

    Sentences are searched in ``es_idx`` order from a moving cursor so a repeated
    sentence resolves to its in-order occurrence (mirroring how ``endnotes``
    later re-finds them). Token-bearing alignment text is tolerated by stripping
    tokens from the sentence before matching.
    """
    spans: List[Tuple[int, int, int]] = []
    cursor = 0
    for es_idx in sorted(es_map):
        # Strip tokens but do not trim surrounding whitespace, so the span
        # matches what endnotes.build_endnote_artifacts later finds verbatim.
        sentence = strip_footnote_tokens(es_map[es_idx])[0]
        if not sentence.strip():
            continue
        i = clean_text.find(sentence, cursor)
        if i == -1:
            i = clean_text.find(sentence)
        if i == -1:
            continue
        spans.append((i, i + len(sentence), es_idx))
        cursor = i + len(sentence)
    return spans


def _locate_span(spans: List[Tuple[int, int, int]], pos: int) -> Optional[Tuple[int, int, int]]:
    """The sentence span a token at *pos* belongs to (the word it follows)."""
    containing = [s for s in spans if s[0] <= pos <= s[1]]
    if containing:
        # Prefer the span the marker sits *after* (largest start ≤ pos).
        return max(containing, key=lambda s: s[0])
    preceding = [s for s in spans if s[1] <= pos]
    if preceding:
        return max(preceding, key=lambda s: s[1])
    return None


def _unique_anchor(clean_text: str, sent_start: int, sent_end: int, pos: int) -> Optional[str]:
    """A verbatim substring ending at *pos*, unique within its sentence.

    Grows word by word backward from *pos* until the candidate occurs exactly
    once in the sentence, so ``endnotes._injection_point`` places the marker at
    exactly *pos*. Returns ``None`` when the token sits at the sentence start.
    """
    if pos <= sent_start:
        return None
    seg = clean_text[sent_start:pos]
    sentence = clean_text[sent_start:sent_end]
    words = list(re.finditer(r"\S+", seg))
    if not words:
        return None
    limit = min(len(words), 6)
    best: Optional[str] = None
    for k in range(1, limit + 1):
        cand = seg[words[-k].start():]
        best = cand
        if sentence.count(cand) == 1:
            return cand
    return best


def convert_chapter_footnotes(
    chapter_id: str,
    project_id: str,
    combined_text: str,
    es_map: Dict[int, str],
    bodies: Dict[int, str],
) -> Tuple[str, List[dict]]:
    """Turn ``[FOOTNOTE:N]`` tokens in a translated chapter into annotations.

    Args:
        combined_text: the combined *translated* chapter, still bearing tokens.
        es_map: ``{es_idx: es_sentence}`` from the chapter's alignment file.
        bodies: ``{number: translated_note_text}`` (falls back to source body
            upstream when a note has not been translated yet).

    Returns ``(clean_text, records)`` where ``clean_text`` is token-free and each
    record is ready to append to ``annotations.jsonl``.
    """
    clean_text, placements = strip_footnote_tokens(combined_text)
    if not placements:
        return clean_text, []

    spans = _sentence_spans(clean_text, es_map)
    now = datetime.now().isoformat()
    records: List[dict] = []

    for number, pos in placements:
        body = (bodies.get(number) or "").strip()
        if not body:
            continue  # nothing to show; skip rather than emit an empty endnote
        span = _locate_span(spans, pos)
        if span is None:
            continue
        sent_start, sent_end, es_idx = span
        anchor = _unique_anchor(clean_text, sent_start, sent_end, pos)
        content = f"[{anchor}] {body}" if anchor else body
        records.append(
            {
                "project_id": project_id,
                "chapter_id": chapter_id,
                "es_idx": es_idx,
                "sub_id": f"gb{number}",
                "type": "footnote",
                "content": content,
                "origin": "gutenberg",
                "fn_number": number,
                "timestamp": now,
            }
        )
    return clean_text, records


def _load_existing_gutenberg_keys(annotations_path: Path, chapter_id: str) -> List[Tuple[int, str]]:
    """Active ``(es_idx, sub_id)`` keys previously written by a footnote import."""
    if not annotations_path.exists():
        return []
    active: dict = {}
    for line in annotations_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("chapter_id") != chapter_id:
            continue
        key = (rec.get("es_idx"), rec.get("sub_id"))
        if rec.get("removed"):
            active.pop(key, None)
        elif rec.get("origin") == "gutenberg":
            active[key] = True
        else:
            # a manual edit at this key supersedes the imported one
            active.pop(key, None)
    return [k for k in active if k[0] is not None]


def write_footnote_annotations(project_path, chapter_id: str, records: List[dict]) -> int:
    """Append imported footnote annotations, replacing any prior import.

    Idempotent: tombstones every still-active ``origin:"gutenberg"`` record for
    the chapter before appending the fresh ones, so re-runs never accumulate
    duplicates. Hand-authored annotations (no ``origin``) are left untouched.
    Returns the number of annotations written.
    """
    annotations_path = Path(project_path) / "annotations.jsonl"
    now = datetime.now().isoformat()
    project_id = Path(project_path).name

    lines: List[str] = []
    for es_idx, sub_id in _load_existing_gutenberg_keys(annotations_path, chapter_id):
        lines.append(json.dumps({
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "sub_id": sub_id,
            "removed": True,
            "origin": "gutenberg",
            "timestamp": now,
        }, ensure_ascii=False))
    for rec in records:
        lines.append(json.dumps(rec, ensure_ascii=False))

    if not lines:
        return 0
    with open(annotations_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(records)
