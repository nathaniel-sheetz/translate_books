"""
The write front door: ``add`` mints footnote records, ``verify`` audits them.

This module exists because ``src/endnotes.py`` drops a footnote **silently**. A
record can be perfectly well-formed JSON, sit in ``annotations.jsonl`` for ever,
and publish nothing at all, in three ways:

==========================  ==================================================
``no_aligned_sentence``     no alignment row carries that ``es_idx``
                            (``endnotes.py:161`` — logs a warning)
``sentence_not_in_body``    the aligned sentence is not findable in
                            ``chapters/<id>.txt`` (``endnotes.py:168`` — logs)
``empty_gloss``             nothing left after the first ``[bracket]`` is
                            stripped (``endnotes.py:180`` — **logs nothing**)
==========================  ==================================================

Plus three anchor problems that are not silent but are still wrong:

==========================  ==================================================
``anchor_not_found``        warning only; the marker falls to the sentence end
``ambiguous_anchor``        the anchor occurs more than once, so the marker
                            lands on the first occurrence, not the intended one
``multi_anchor``            more than one bracket: ``endnotes`` consumes the
                            first and publishes the rest verbatim into the book
                            (``targets.py:292``) — refused
==========================  ==================================================

Every one of those is checked *before* the record is appended. ``verify`` runs the
same checks over notes already on disk, which is the half that catches the
pre-existing ones — and the reason to re-run it after any ``harness.py align``:
``es_idx`` is a position in the alignment, not an identity.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.annotations import store
from src.endnotes import _injection_point, parse_endnote_content
from src.footnote_import import _unique_anchor
from src.footnote_pass import FOOTNOTE_TYPE, ORIGIN
from src.footnote_pass.corpus import (
    chapter_body_path,
    load_alignment_es_map,
    read_chapter_body,
)

logger = logging.getLogger(__name__)

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")

# Failure codes. Named rather than free-text so the skill, the tests and the
# report all say the same thing about the same defect.
NO_ALIGNED_SENTENCE = "no_aligned_sentence"
SENTENCE_NOT_IN_BODY = "sentence_not_in_body"
EMPTY_GLOSS = "empty_gloss"
ANCHOR_NOT_FOUND = "anchor_not_found"
AMBIGUOUS_ANCHOR = "ambiguous_anchor"
MULTI_ANCHOR = "multi_anchor"
NO_CHAPTER_BODY = "no_chapter_body"
DUPLICATE = "duplicate"
SENTENCE_DRIFTED = "sentence_drifted"

# ``anchor_not_found``, ``ambiguous_anchor`` and ``sentence_drifted`` degrade
# *placement*; the note still publishes, so they warn rather than refuse.
# Everything else means nothing reaches the book at all.
WARNING_CODES = frozenset({ANCHOR_NOT_FOUND, AMBIGUOUS_ANCHOR, SENTENCE_DRIFTED})


def mint_sub_id() -> str:
    """A reader-convention ``sub_id``: ``u`` + 8 hex chars.

    The same shape ``web_ui/app.py:save_annotation`` mints, and deliberately
    clear of the ``gb<n>`` namespace ``footnote_import`` owns, so a Gutenberg
    re-import can never collide with a note written here.
    """
    return "u" + secrets.token_hex(4)


@dataclass
class Problem:
    """One validation failure or warning on a proposed or existing note."""

    code: str
    detail: str

    @property
    def is_warning(self) -> bool:
        return self.code in WARNING_CODES

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "warning": self.is_warning}


@dataclass
class Proposal:
    """One footnote to write, after validation."""

    chapter_id: str
    es_idx: int
    anchor: Optional[str]
    note: str
    content: str
    es_text: str
    problems: list[Problem]
    injection_preview: str = ""
    suggested_anchor: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return any(not p.is_warning for p in self.problems)


def _strip_brackets(text: str) -> str:
    """The text with every ``[...]`` token removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", _BRACKET_RE.sub(" ", text)).strip()


def compose_content(anchor: Optional[str], note: str) -> str:
    """The stored ``content`` for a note: ``[anchor] gloss``, or just the gloss.

    The same composition ``review._planned_content`` uses for a footnote, kept
    here so the create path and the review path cannot disagree about the wire
    shape of a record.
    """
    note = (note or "").strip()
    anchor = (anchor or "").strip()
    return f"[{anchor}] {note}" if anchor else note


def _preview(body: str, sent_start: int, sent_end: int, anchor: Optional[str]) -> str:
    """Show where the superscript marker would land, with ``‹N›`` standing in.

    Computed through ``endnotes._injection_point`` itself rather than a private
    re-implementation: a preview that agrees with a different algorithm than the
    publisher is worse than no preview.
    """
    pos = _injection_point(body, sent_start, sent_end, anchor)
    sentence = body[sent_start:sent_end]
    marked = body[sent_start:pos] + "‹N›" + body[pos:sent_end]
    return marked if sentence else ""


def validate_proposal(
    project_dir: Path,
    *,
    chapter_id: str,
    es_idx: Any,
    anchor: Optional[str],
    note: str,
    es_map: Optional[dict[int, str]] = None,
    body: Optional[str] = None,
    existing: Optional[set[tuple[str, Optional[int], str]]] = None,
) -> Proposal:
    """Validate one proposed footnote against every silent-failure mode.

    ``es_map`` / ``body`` are caches so a batch does not re-read one chapter's
    alignment and text per note. ``existing`` is ``{(chapter_id, es_idx, content)}``
    of live notes, used for the idempotency check — re-running ``add`` with
    identical arguments must be a no-op, not a second endnote.
    """
    project_dir = Path(project_dir)
    problems: list[Problem] = []
    note = (note or "").strip()
    anchor = (anchor or "").strip() or None

    try:
        es_idx_int = int(es_idx)
    except (TypeError, ValueError):
        return Proposal(
            chapter_id=chapter_id,
            es_idx=-1,
            anchor=anchor,
            note=note,
            content=compose_content(anchor, note),
            es_text="",
            problems=[Problem(NO_ALIGNED_SENTENCE, f"es_idx is not an integer: {es_idx!r}")],
        )

    content = compose_content(anchor, note)

    # empty_gloss first: it is the one endnotes.py does not log, so it is the one
    # most worth naming loudly. Checked against the bracket-stripped text, which
    # is exactly what ``parse_endnote_content`` publishes.
    if not _strip_brackets(content):
        problems.append(
            Problem(
                EMPTY_GLOSS,
                "the note is empty once the [anchor] bracket is stripped, so it "
                "would be skipped with no log line and publish nothing",
            )
        )

    # More than one bracket publishes the extras literally into the book.
    brackets = [m.group(1).strip() for m in _BRACKET_RE.finditer(content)]
    if len(brackets) > 1:
        problems.append(
            Problem(
                MULTI_ANCHOR,
                f"{len(brackets)} bracketed tokens ({brackets!r}); endnotes consumes "
                "the first and publishes the rest verbatim — write one note per anchor",
            )
        )

    if es_map is None:
        es_map = load_alignment_es_map(project_dir, chapter_id)
    es_text = es_map.get(es_idx_int, "")
    if not es_text:
        problems.append(
            Problem(
                NO_ALIGNED_SENTENCE,
                f"no alignment row for {chapter_id} es_idx={es_idx_int} in "
                f"alignments/{chapter_id}.json",
            )
        )

    if body is None:
        body = read_chapter_body(project_dir, chapter_id)
    preview = ""
    suggested: Optional[str] = None
    if body is None:
        problems.append(
            Problem(
                NO_CHAPTER_BODY,
                f"no chapter body at {chapter_body_path(project_dir, chapter_id)}",
            )
        )
    elif es_text:
        sent_start = body.find(es_text)
        if sent_start == -1:
            problems.append(
                Problem(
                    SENTENCE_NOT_IN_BODY,
                    "the aligned sentence is not findable verbatim in "
                    f"chapters/{chapter_id}.txt — the note would be skipped",
                )
            )
        else:
            sent_end = sent_start + len(es_text)
            if anchor:
                occurrences = es_text.count(anchor)
                if occurrences == 0:
                    problems.append(
                        Problem(
                            ANCHOR_NOT_FOUND,
                            f"anchor {anchor!r} does not occur in the sentence; the "
                            "marker will fall to the end of the sentence",
                        )
                    )
                elif occurrences > 1:
                    # Grown backward from the END OF THE FIRST occurrence, because
                    # that is where ``_injection_point`` actually puts the marker.
                    # The suggestion therefore pins the current behaviour
                    # explicitly; it cannot guess that a later occurrence was
                    # meant. Same helper footnote_import uses, so a grown anchor
                    # behaves identically to an imported one.
                    first_end = sent_start + es_text.find(anchor) + len(anchor)
                    grown = _unique_anchor(body, sent_start, sent_end, first_end)
                    suggested = grown if grown and grown != anchor else None
                    problems.append(
                        Problem(
                            AMBIGUOUS_ANCHOR,
                            f"anchor {anchor!r} occurs {occurrences}x in the sentence; "
                            "the marker lands on the first one"
                            + (
                                f". --anchor {suggested!r} pins that spot; for a later "
                                "occurrence, extend the anchor by hand"
                                if suggested
                                else ""
                            ),
                        )
                    )
            preview = _preview(body, sent_start, sent_end, anchor)

    if existing is not None and (chapter_id, es_idx_int, content) in existing:
        problems.append(
            Problem(
                DUPLICATE,
                "an active footnote on this sentence already holds exactly this "
                "text — adding it again would publish two identical endnotes",
            )
        )

    return Proposal(
        chapter_id=chapter_id,
        es_idx=es_idx_int,
        anchor=anchor,
        note=note,
        content=content,
        es_text=es_text,
        problems=problems,
        injection_preview=preview,
        suggested_anchor=suggested,
    )


def _live_contents(project_dir: Path) -> set[tuple[str, Optional[int], str]]:
    """``{(chapter_id, es_idx, content)}`` for every active footnote."""
    return {
        (r.get("chapter_id") or "", r.get("es_idx"), r.get("content") or "")
        for r in store.load_active(project_dir, types=(FOOTNOTE_TYPE,))
    }


def build_record(project_dir: Path, proposal: Proposal) -> dict:
    """The ``annotations.jsonl`` record for a validated proposal.

    Mirrors the wire shape ``web_ui/app.py:save_annotation`` writes — the reader
    has to be able to open, edit and delete this note like any other — plus
    ``origin`` for provenance. ``origin`` is inert downstream: only
    ``"gutenberg"`` is special-cased (``src/annotations/targets.py``).
    """
    return {
        "project_id": Path(project_dir).name,
        "chapter_id": proposal.chapter_id,
        "es_idx": proposal.es_idx,
        "sub_id": mint_sub_id(),
        "type": FOOTNOTE_TYPE,
        "content": proposal.content,
        "es_text": proposal.es_text,
        "origin": ORIGIN,
        "timestamp": datetime.now().isoformat(),
    }


def add(
    project_dir: Path,
    notes: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and append footnote records.

    ``notes`` is ``[{chapter_id, es_idx, anchor?, note}, ...]`` — one entry from
    ``--chapter/--es-idx/--anchor/--note``, or the whole approved batch from
    ``--json-file``. Every entry is validated independently and reported
    independently: a batch with one bad anchor lands the rest rather than
    refusing as a unit, which is what makes re-running it after a fix cheap.

    Writes go through ``store.append_record`` — never a hand-rolled
    ``open(..., "a")``; that module is the single writer, and keeping the file
    append-only is what makes every run recoverable.
    """
    project_dir = Path(project_dir)
    existing = _live_contents(project_dir)
    es_maps: dict[str, dict[int, str]] = {}
    bodies: dict[str, Optional[str]] = {}

    added: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for raw in notes:
        chapter_id = str(raw.get("chapter_id") or raw.get("chapter") or "")
        if chapter_id not in es_maps:
            es_maps[chapter_id] = load_alignment_es_map(project_dir, chapter_id)
            bodies[chapter_id] = read_chapter_body(project_dir, chapter_id)

        proposal = validate_proposal(
            project_dir,
            chapter_id=chapter_id,
            es_idx=raw.get("es_idx"),
            anchor=raw.get("anchor"),
            note=raw.get("note") or raw.get("content") or "",
            es_map=es_maps[chapter_id],
            body=bodies[chapter_id],
            existing=existing,
        )

        row = {
            "chapter_id": proposal.chapter_id,
            "es_idx": proposal.es_idx,
            "anchor": proposal.anchor,
            "content": proposal.content,
            "problems": [p.as_dict() for p in proposal.problems],
            "injection_preview": proposal.injection_preview,
        }
        if proposal.suggested_anchor:
            row["suggested_anchor"] = proposal.suggested_anchor

        if proposal.blocked:
            refused.append(row)
            continue
        if proposal.problems:
            # A pointer, not a second copy: the full row is already in `added` or
            # `planned`, and echoing it twice is the stdout bloat every other CLI
            # in this repo has had to walk back.
            warnings.append(
                {
                    "chapter_id": proposal.chapter_id,
                    "es_idx": proposal.es_idx,
                    "codes": [p.code for p in proposal.problems],
                }
            )

        if dry_run:
            planned.append(row)
            continue

        record = build_record(project_dir, proposal)
        store.append_record(project_dir, record)
        # Keep the in-run dedupe honest: two identical entries in one --json-file
        # must not both land.
        existing.add((proposal.chapter_id, proposal.es_idx, proposal.content))
        added.append({**row, "sub_id": record["sub_id"]})

    return {
        "status": "ok" if not refused else "partial",
        "dry_run": dry_run,
        "added": added,
        "planned": planned,
        "refused": refused,
        "warnings": warnings,
        "counts": {
            "requested": len(notes),
            "added": len(added),
            "planned": len(planned),
            "refused": len(refused),
            "warnings": len(warnings),
        },
        "annotations_path": str(store.annotations_path(project_dir)),
        "instructions": (
            "Fix each refused entry and re-run; a refused note is not on disk. "
            if refused
            else ""
        )
        + (
            "Nothing was written (--dry-run). Re-run without it to land these."
            if dry_run
            else "Run `verify` next, then `python scripts/harness.py epub "
            "--project <id>` — an added note only reaches the book on the next build."
        ),
    }


def verify(
    project_dir: Path, *, chapters: Optional[list[str]] = None
) -> dict[str, Any]:
    """Audit every active footnote in scope against the validation table.

    This is the half that catches the notes already in the file rather than only
    the ones just added, which is why the flow runs it after ``add`` *and* after
    any ``harness.py align``: re-aligning a chapter moves every ``es_idx``, and a
    note whose sentence moved fails ``no_aligned_sentence`` or
    ``sentence_not_in_body`` from then on, publishing nothing and saying nothing.
    """
    project_dir = Path(project_dir)
    wanted = set(chapters) if chapters else None

    records = store.load_active(project_dir, types=(FOOTNOTE_TYPE,))
    if wanted is not None:
        records = [r for r in records if r.get("chapter_id") in wanted]

    es_maps: dict[str, dict[int, str]] = {}
    bodies: dict[str, Optional[str]] = {}

    ok: list[str] = []
    broken: list[dict[str, Any]] = []
    warned: list[dict[str, Any]] = []
    by_code: dict[str, int] = {}

    for record in records:
        chapter_id = record.get("chapter_id") or ""
        if chapter_id not in es_maps:
            es_maps[chapter_id] = load_alignment_es_map(project_dir, chapter_id)
            bodies[chapter_id] = read_chapter_body(project_dir, chapter_id)

        content = record.get("content") or ""
        anchor, note = parse_endnote_content(content)
        proposal = validate_proposal(
            project_dir,
            chapter_id=chapter_id,
            es_idx=record.get("es_idx"),
            anchor=anchor,
            note=note,
            es_map=es_maps[chapter_id],
            body=bodies[chapter_id],
        )

        problems = list(proposal.problems)

        # Drift: the record's own ``es_text`` snapshot says which sentence the note
        # was written about, and the alignment says which sentence that ``es_idx``
        # names *now*. When they disagree, a re-align or a retranslation moved the
        # rows under the note: it still publishes, but against a sentence nobody
        # chose. Only checkable here — a fresh proposal has no snapshot to compare.
        snapshot = record.get("es_text")
        if snapshot and proposal.es_text and snapshot != proposal.es_text:
            problems.append(
                Problem(
                    SENTENCE_DRIFTED,
                    f"es_idx={record.get('es_idx')} now names a different sentence "
                    f"than this note was written against ({snapshot!r}); re-anchor it",
                )
            )

        key = store.target_key(record)
        row = {
            "key": key,
            "chapter_id": chapter_id,
            "es_idx": record.get("es_idx"),
            "origin": record.get("origin"),
            "content": content,
            "problems": [p.as_dict() for p in problems],
        }
        for problem in problems:
            by_code[problem.code] = by_code.get(problem.code, 0) + 1
        if any(not p.is_warning for p in problems):
            broken.append(row)
        elif problems:
            warned.append(row)
        else:
            ok.append(key)

    return {
        "status": "ok" if not broken else "broken",
        "counts": {
            "audited": len(records),
            "ok": len(ok),
            "broken": len(broken),
            "warned": len(warned),
        },
        "by_code": by_code,
        "broken": broken,
        "warned": warned,
        "chapters": sorted(chapters) if chapters else None,
        "instructions": (
            (
                "Each broken note publishes NOTHING today. Relay them: an "
                f"{EMPTY_GLOSS} is a note never written up; the other codes mean "
                "the sentence moved (usually a re-align or a retranslation) and "
                "the note needs re-anchoring in the reader or by a fresh `add`. "
                if broken
                else "Every active footnote resolves to a sentence and publishes "
                "text. Safe to build the EPUB. "
            )
            + (
                f"{len(warned)} note(s) publish but are misplaced — a "
                f"{SENTENCE_DRIFTED} means the note is now attached to a sentence "
                "nobody chose, which is worth raising even though it is not fatal."
                if warned
                else ""
            )
        ).strip(),
    }
