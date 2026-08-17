"""
Backfill of ``[CAPTION]`` markers into already-translated books.

New books get their captions at ingest: ``scripts/ingest_gutenberg.py`` reads
``<figcaption>`` / ``<p class="caption">`` straight out of the source HTML.
Books translated before that existed carry the caption as an ordinary paragraph
sitting under its image, and cannot simply be re-ingested -- the original HTML
is not cached anywhere (only ``source.txt``), and re-running ingest would
rewrite ``source.txt`` and put committed translations at risk.

This module recovers those captions from the text itself. For every block that
is a sole ``[IMAGE:...]`` token, the block below it is classified into a
confidence tier; the caller chooses which tiers to accept, and the accepted
paragraphs are marked in place.

Two properties make it safe:

- Writes go to ``source.txt`` and ``chunks/*.json``, never ``chapters/*.txt``.
  Chapter files are derived (``combiner.combine_chunks`` ->
  ``save_chapters_to_files``), so a marker written there is erased by the next
  combine.
- Source and translation are paired by IMAGE FILENAME, which
  ``harness_guard.guard_translation_draft`` already enforces as identical across
  the pair. One accept decision therefore marks the English paragraph and the
  Spanish paragraph of the same figure.

Tiers, and why the low ones are not auto-accepted:

    A  the block matches the image's own alt text        -- certain
    B  the block is a fully ``_underscored_`` paragraph  -- near-certain
    C  ALL-CAPS short line                               -- confirm
    D  short phrase, no terminal punctuation             -- confirm
    E  anything else                                     -- default NO

Tier E must default to NO. In ``home-geography``,
``[IMAGE:images/006.jpg:A COMPASS.]`` is followed by genuine body prose, so
blind auto-marking would corrupt the book.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from src.utils.text_utils import CAPTION_MARKER, is_caption_block

# Same shape as epub_builder._IMAGE_RE -- an image block is a sole token.
_IMAGE_RE = re.compile(r'\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]')
_BLOCK_SEP_RE = re.compile(r'\n\s*\n')
_CHUNK_FILE_RE = re.compile(r'^(.+)_chunk_(\d+)\.json$')
_CHAPTER_NUM_RE = re.compile(r'(\d+)')
_HR_RE = re.compile(r'^-{3,}$')
_FULLY_ITALIC_RE = re.compile(r'^_[^_]+_$')

TIERS: Tuple[str, ...] = ("A", "B", "C", "D", "E")
#: Tiers safe to accept without a human looking at them.
AUTO_TIERS = frozenset({"A", "B"})

#: A caption is a short line. Anything longer is body prose.
_MAX_CAPTION_WORDS = 40

_TIER_REASON = {
    "A": "matches the image's alt text",
    "B": "fully italicized paragraph",
    "C": "ALL-CAPS short line",
    "D": "short phrase, no terminal punctuation",
    "E": "follows an image, but reads like body prose",
}


# ---------------------------------------------------------------------------
# Block scanning
# ---------------------------------------------------------------------------

def iter_blocks(text: str) -> Iterator[Tuple[int, int, str]]:
    """Yield ``(start, end, block)`` for each blank-line-separated block.

    Offsets are into the original *text* and point at the first and last
    non-whitespace character of the block, so an insertion at ``start`` lands
    exactly ahead of the block's first character without disturbing the
    surrounding blank lines.
    """
    pos = 0
    for sep in _BLOCK_SEP_RE.finditer(text):
        seg = text[pos:sep.start()]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            yield (pos + lead, pos + len(seg.rstrip()), seg.strip())
        pos = sep.end()
    seg = text[pos:]
    if seg.strip():
        lead = len(seg) - len(seg.lstrip())
        yield (pos + lead, pos + len(seg.rstrip()), seg.strip())


def _norm(s: str) -> str:
    """Fold for comparison: strip accents, case and punctuation."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _is_candidate_text(block: str) -> bool:
    """Could *block* be a caption at all? (shape only, no confidence)"""
    if not block or is_caption_block(block):
        return False
    if _IMAGE_RE.fullmatch(block) or _HR_RE.match(block):
        return False
    return len(block.split()) <= _MAX_CAPTION_WORDS


def _alt_matches(alt: str, text: str) -> bool:
    """True when *text* is the image's alt text again, modulo trailing period.

    Exact (folded) equality always counts. Containment only counts when the
    shorter string is a substantial share of the longer one -- otherwise a
    one-word alt would swallow any paragraph that happens to contain it.
    """
    na, nt = _norm(alt), _norm(text)
    if not na or not nt:
        return False
    if na == nt:
        return True
    shorter, longer = (na, nt) if len(na) <= len(nt) else (nt, na)
    if shorter not in longer:
        return False
    return len(shorter) >= 12 and len(shorter) / len(longer) >= 0.6


def classify(alt: str, text: str) -> str:
    """Return the confidence tier (``A``..``E``) for a caption candidate."""
    if _alt_matches(alt, text):
        return "A"
    if _FULLY_ITALIC_RE.match(text.strip()):
        return "B"
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(text.split()) <= 10:
        return "C"
    if (
        len(text.split()) <= 8
        and not text.rstrip().endswith((".", "?", "!", "…", ":"))
        and not text.lstrip().startswith(("—", "«", '"', "“"))
    ):
        return "D"
    return "E"


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One image whose following paragraph might be its caption."""

    index: int                      # 1-based, stable within a scan
    chapter_id: str
    image: str                      # e.g. "images/i010.jpg"
    occurrence: int                 # nth use of this filename in the book
    alt: str
    tier: str
    text: str                       # the candidate paragraph as displayed
    chunk_id: str = ""              # chunk file stem holding `text` ('' if none)
    chunk_offset: Optional[int] = None    # insertion offset within translated_text
    source_text: str = ""           # parallel English paragraph ('' if none)
    source_offset: Optional[int] = None   # insertion offset within source.txt

    @property
    def reason(self) -> str:
        return _TIER_REASON[self.tier]

    @property
    def source_only(self) -> bool:
        """True when the chapter is not translated yet, so only source.txt is marked."""
        return self.chunk_offset is None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "chapter_id": self.chapter_id,
            "image": self.image,
            "alt": self.alt,
            "tier": self.tier,
            "reason": self.reason,
            "text": self.text,
            "source_text": self.source_text,
            "chunk_id": self.chunk_id,
            "has_source_match": self.source_offset is not None,
            "source_only": self.source_only,
        }


@dataclass
class _ChunkBlocks:
    """A chapter's blocks, flattened across its chunks in document order."""

    chapter_id: str
    # (chunk_id, offset_in_that_chunk, block_text)
    blocks: List[Tuple[str, int, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scanning a project
# ---------------------------------------------------------------------------

def _chunk_files(project_dir: Path) -> Dict[str, List[Tuple[int, Path]]]:
    """Map chapter_id -> [(chunk_number, path)] sorted by chunk number."""
    out: Dict[str, List[Tuple[int, Path]]] = {}
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return out
    for path in chunks_dir.glob("*_chunk_*.json"):
        m = _CHUNK_FILE_RE.match(path.name)
        if not m:
            continue
        out.setdefault(m.group(1), []).append((int(m.group(2)), path))
    for chapter_id in out:
        out[chapter_id].sort()
    return out


def _load_translated(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("translated_text") or ""


def _flatten_chapter(chapter_id: str, files: List[Tuple[int, Path]]) -> _ChunkBlocks:
    """Flatten a chapter's chunks into one block list.

    Blocks keep the chunk they came from, so an image at the end of one chunk
    can pair with a caption at the start of the next and the marker still lands
    in the right file.
    """
    flat = _ChunkBlocks(chapter_id=chapter_id)
    for _, path in files:
        text = _load_translated(path)
        for start, _end, block in iter_blocks(text):
            flat.blocks.append((path.stem, start, block))
    return flat


def _heading_strings(project_dir: Path, chapter_id: str, chapter_text: str) -> set:
    """Folded heading + subtitle for a chapter, used to reject chapter titles.

    A decorative ornament above a chapter title looks exactly like an image
    above a caption. Reusing the EPUB builder's own heading detection -- rather
    than a positional guess -- is what keeps gaudenzia's 31 ornaments out of the
    candidate list.
    """
    from src.epub_builder import _load_chapter_heading_config, detect_chapter_heading

    num_match = _CHAPTER_NUM_RE.search(chapter_id)
    chapter_number = int(num_match.group(1)) if num_match else None
    try:
        heading, subtitle, _body = detect_chapter_heading(
            chapter_text,
            chapter_number=chapter_number,
            heading_config=_load_chapter_heading_config(project_dir),
        )
    except Exception:
        return set()
    return {_norm(s) for s in (heading, subtitle) if s}


def _source_captions(
    project_dir: Path,
) -> Tuple[Dict[Tuple[str, int], Tuple[int, str]], Dict[Tuple[str, int], str]]:
    """Scan source.txt for caption candidates.

    Returns ``(blocks, alts)``, both keyed by ``(image filename, occurrence)``:
    ``blocks`` maps to ``(insert offset, block text)``, ``alts`` to that image's
    alt text. The filename/occurrence key is what pairs a source paragraph with
    its translated counterpart -- image filenames are identical across the pair
    because harness_guard enforces that.
    """
    source = project_dir / "source.txt"
    if not source.exists():
        return {}, {}
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return {}, {}

    out: Dict[Tuple[str, int], Tuple[int, str]] = {}
    alts: Dict[Tuple[str, int], str] = {}
    seen: Dict[str, int] = {}
    blocks = list(iter_blocks(text))
    for i, (_start, _end, block) in enumerate(blocks):
        m = _IMAGE_RE.fullmatch(block)
        if not m:
            continue
        filename = m.group(1)
        seen[filename] = seen.get(filename, 0) + 1
        key = (filename, seen[filename])
        alts[key] = m.group(2) or ""
        if i + 1 >= len(blocks):
            continue
        nxt_start, _nxt_end, nxt = blocks[i + 1]
        if not _is_candidate_text(nxt):
            continue
        out[key] = (nxt_start, nxt)
    return out, alts


def scan_project(project_dir: Path) -> List[Candidate]:
    """Return every caption candidate in *project_dir*, in document order."""
    project_dir = Path(project_dir)
    per_chapter = _chunk_files(project_dir)
    source_map, _source_alts = _source_captions(project_dir)

    candidates: List[Candidate] = []
    seen_images: Dict[str, int] = {}
    matched_source: set = set()
    index = 0

    for chapter_id in sorted(per_chapter):
        flat = _flatten_chapter(chapter_id, per_chapter[chapter_id])
        chapter_text = "\n\n".join(b for _cid, _off, b in flat.blocks)
        headings = _heading_strings(project_dir, chapter_id, chapter_text)

        for i, (_cid, _off, block) in enumerate(flat.blocks):
            m = _IMAGE_RE.fullmatch(block)
            if not m:
                continue
            filename = m.group(1)
            alt = m.group(2) or ""
            seen_images[filename] = seen_images.get(filename, 0) + 1
            occurrence = seen_images[filename]
            # Claim the image regardless of whether it yields a candidate. The
            # translated side is the authority: if it decided this image has no
            # caption (heading ornament, body prose below it), the source-only
            # pass must not re-admit it without that context.
            matched_source.add((filename, occurrence))

            if i + 1 >= len(flat.blocks):
                continue
            nxt_chunk, nxt_offset, nxt = flat.blocks[i + 1]
            if not _is_candidate_text(nxt):
                continue

            tier = classify(alt, nxt)
            # A chapter title sitting under a header ornament is not a caption.
            # Narrowly scoped, because detect_chapter_heading will happily
            # promote any short opening paragraph to "subtitle": the block must
            # be title-shaped (tier C/D -- a title is never tier E prose, and
            # never an alt duplicate or an italic run), and the ornament must
            # actually lead the chapter rather than sit in the middle of it.
            # "Leads the chapter" allows the heading line itself to precede the
            # ornament -- the real shape is  "Capítulo I" / [IMAGE] / title.
            leads_chapter = all(
                _IMAGE_RE.fullmatch(b) or _norm(b) in headings
                for _c, _o, b in flat.blocks[:i]
            )
            if tier in ("C", "D") and _norm(nxt) in headings and leads_chapter:
                continue

            index += 1
            src_offset, src_text = source_map.get((filename, occurrence), (None, ""))
            candidates.append(
                Candidate(
                    index=index,
                    chapter_id=chapter_id,
                    image=filename,
                    occurrence=occurrence,
                    alt=alt,
                    tier=tier,
                    text=nxt,
                    chunk_id=nxt_chunk,
                    chunk_offset=nxt_offset,
                    source_text=src_text,
                    source_offset=src_offset,
                )
            )

    # Chapters that are not translated yet contribute no chunk blocks, so their
    # captions are invisible above. Marking source.txt for them now means the
    # marker flows through translation when they are eventually translated,
    # instead of needing a second backfill later.
    for (filename, occurrence), (offset, text) in sorted(source_map.items()):
        if (filename, occurrence) in matched_source:
            continue
        index += 1
        candidates.append(
            Candidate(
                index=index,
                chapter_id="(untranslated)",
                image=filename,
                occurrence=occurrence,
                alt=_source_alts.get((filename, occurrence), ""),
                tier=classify(_source_alts.get((filename, occurrence), ""), text),
                text=text,
                source_text=text,
                source_offset=offset,
            )
        )
    return candidates


def select(
    candidates: List[Candidate],
    *,
    accept_tiers: Optional[set] = None,
    accept: Optional[set] = None,
    reject: Optional[set] = None,
) -> List[Candidate]:
    """Resolve a tier/index selection into the candidates to mark.

    ``accept`` adds individual indices on top of the accepted tiers; ``reject``
    removes them afterwards, so ``--accept-tiers A,B,C,D --reject 61`` reads the
    way it looks.
    """
    tiers = set(accept_tiers or AUTO_TIERS)
    picked = {c.index for c in candidates if c.tier in tiers}
    picked |= set(accept or ())
    picked -= set(reject or ())
    return [c for c in candidates if c.index in picked]


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _insert_all(text: str, offsets: List[int]) -> Tuple[str, int]:
    """Insert the marker at each offset. Returns (new_text, n_inserted).

    Applied high-offset-first so earlier offsets stay valid, and idempotent:
    an offset already carrying the marker is skipped.
    """
    inserted = 0
    for off in sorted(set(offsets), reverse=True):
        if is_caption_block(text[off:off + len(CAPTION_MARKER) + 1]):
            continue
        text = f"{text[:off]}{CAPTION_MARKER} {text[off:]}"
        inserted += 1
    return text, inserted


def apply_marks(project_dir: Path, accepted: List[Candidate]) -> dict:
    """Write ``[CAPTION]`` markers for *accepted* into chunks and source.txt.

    Returns a report dict. Idempotent -- re-running with the same selection is a
    no-op, so a partially applied run can simply be repeated.
    """
    project_dir = Path(project_dir)
    report = {
        "chunks_written": 0,
        "chunk_marks": 0,
        "source_marks": 0,
        "source_written": False,
        "missing_source": [],
    }
    if not accepted:
        return report

    # --- chunks (skipped for source-only candidates) ---
    by_chunk: Dict[str, List[int]] = {}
    for c in accepted:
        if c.chunk_offset is None or not c.chunk_id:
            continue
        by_chunk.setdefault(c.chunk_id, []).append(c.chunk_offset)

    for chunk_id, offsets in by_chunk.items():
        path = project_dir / "chunks" / f"{chunk_id}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        original = data.get("translated_text") or ""
        updated, n = _insert_all(original, offsets)
        if n:
            data["translated_text"] = updated
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report["chunks_written"] += 1
            report["chunk_marks"] += n

    # --- source.txt ---
    source = project_dir / "source.txt"
    src_offsets = [c.source_offset for c in accepted if c.source_offset is not None]
    report["missing_source"] = [c.index for c in accepted if c.source_offset is None]
    if source.exists() and src_offsets:
        text = source.read_text(encoding="utf-8")
        updated, n = _insert_all(text, src_offsets)
        if n:
            source.write_text(updated, encoding="utf-8")
            report["source_written"] = True
            report["source_marks"] = n

    return report


def tier_counts(candidates: List[Candidate]) -> Dict[str, int]:
    """Count candidates per tier, including tiers with no hits."""
    counts = {t: 0 for t in TIERS}
    for c in candidates:
        counts[c.tier] += 1
    return counts
