"""Collect the coded findings a triage pass should read, each with its sentence.

A stored finding is a word and a character offset. The model cannot judge either
without the sentence they sit in — the same token is a typo in one paragraph and
a character's name in the next — so this module joins each finding back onto the
prose it fired on.

**It does not re-implement that join.** The reader already anchors findings onto
sentences for Review Mode, and this walks the same path with the same helpers
(``attach_text_in_chunk`` stamps each alignment row with its chunk offsets,
``row_containing_offset`` resolves one offset onto a row). Two coordinate spaces
that disagree would put a verdict against the wrong sentence, which is the one
failure this pass must never have.

What is skipped, and why each is skipped rather than guessed at:

- **Findings already ruled on.** A human dismissal, a book-wide ignored term, or
  a standing triage verdict all mean the question is answered; re-asking spends
  tokens to overwrite a human.
- **Findings that will not anchor.** An offset landing on no sentence reaches the
  reader's overflow bin today, and a model given no sentence would be guessing.
  They are counted and reported, never sent.
- **Findings on stale chunks whose snippet has moved.** Same rule
  ``_build_chapter_review`` applies: a verdict formed against earlier prose
  cannot vouch for an offset the text has shifted under.

One item is one *finding*, not one occurrence. A checker reports a repeated
unknown word once — ``'pudín': Unknown word ... (found 3 time(s))`` — and the
normalizer fans that into an entry per occurrence so the reader can highlight
each span. Those entries share ``issue_index``, ``severity``, ``message`` and
``location.raw``, which are the four fields :func:`issue_key` hashes, so they
are a single identity to the sidecar, to the dismissal corpus, and to all three
read-time gates. They are collapsed back here, carrying every occurrence's
sentence, because one verdict is all any of them can ever store.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: Coded checkers this pass triages. Deliberately not every coded evaluator:
#: these two produce ~70% of all human clearing work at 7-8% and 16-19% accept
#: rates, and they are the only two with a labelled corpus large enough to set a
#: cutoff against (``scripts/replay_triage.py``). ``blacklist`` and
#: ``completeness`` are precise enough not to need this, and ``glossary`` has 24
#: marks and no ``resolved`` row, so its threshold could not be calibrated.
TRIAGE_EVAL_NAMES: tuple[str, ...] = ("dictionary", "grammar")

#: Image placeholders are not prose. A finding landing inside one has no
#: sentence to show, and the reader filters these rows out of the alignment, so
#: the span is deliberately left uncovered. Mirrors ``web_ui/app.py``.
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]")


def _skip_reason_counts() -> dict[str, int]:
    """A fresh zero-filled tally of why findings were not sent for triage."""
    return {
        "dismissed": 0,
        "ignored": 0,
        "already_triaged": 0,
        "unanchored": 0,
        "stale_moved": 0,
        "no_evaluation": 0,
    }


def iter_chapters(project_dir: Path) -> Iterator[str]:
    """Chapter ids this book has an alignment for, in order.

    The alignment is what carries sentences, so a chapter without one cannot be
    triaged however many findings it holds.
    """
    align_dir = Path(project_dir) / "alignments"
    if not align_dir.exists():
        return
    for path in sorted(align_dir.glob("*.json")):
        yield path.stem


def collect_chapter(
    project_dir: Path,
    chapter: str,
    *,
    eval_names: tuple[str, ...] = TRIAGE_EVAL_NAMES,
    skips: Optional[dict[str, int]] = None,
) -> list[dict[str, Any]]:
    """Every triage-worthy finding in one chapter, each with its sentence.

    Each item carries what the model is asked to judge and what ``commit`` needs
    to write a verdict back:

    ``{id, chunk_id, eval_name, issue_index, issue_key, term, rule_id, message,
    suggestion, match, sentences, occurrences}``

    ``id`` is ``<chunk_id>:<eval_name>:<issue_index>:<issue_key>`` — unique
    within a run, and carrying its own join back to the sidecar so a draft that
    answers about the wrong item cannot be silently mis-filed.

    ``sentences`` holds one entry per distinct sentence the finding fired in, in
    document order, and ``occurrences`` how many offsets it covers. They differ
    only for a word a checker found more than once in one chunk.

    ``skips``, when given, is incremented in place with why findings were passed
    over, so ``prepare`` can report the shape of what it did not send.
    """
    from web_ui.evaluations import (  # local import: web_ui is the persistence layer
        attach_text_in_chunk,
        build_dismissed,
        build_triaged,
        current_chunk_sha,
        evaluator_freshness_detail,
        feedback_mark,
        is_ignored,
        issue_key,
        issue_term,
        load_all_feedback_by_chunk,
        load_all_triage_by_chunk,
        load_chunk_evaluation,
        load_project_ignored_terms,
        row_containing_offset,
        triage_mark,
    )

    project_dir = Path(project_dir)
    tally = _skip_reason_counts() if skips is None else skips

    align_path = project_dir / "alignments" / f"{chapter}.json"
    try:
        data = json.loads(align_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable alignment %s: %s", align_path, exc)
        return []

    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        attach_text_in_chunk(data, chunks_dir)

    rows_by_chunk: dict[str, list[dict]] = {}
    for row in data.get("alignments", []):
        if not isinstance(row, dict) or "es_idx" not in row:
            continue
        chunk_id = row.get("chunk_id")
        if not chunk_id:
            continue
        if row.get("chunk_offset_start") is None or row.get("text_in_chunk") is None:
            continue
        if _IMAGE_PLACEHOLDER_RE.fullmatch((row.get("es") or "").strip()):
            continue
        rows_by_chunk.setdefault(chunk_id, []).append(row)

    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)
    triage_by_chunk = load_all_triage_by_chunk(project_dir)
    ignored_terms = load_project_ignored_terms(project_dir)

    items: list[dict[str, Any]] = []
    for chunk_id, crows in sorted(rows_by_chunk.items()):
        payload = load_chunk_evaluation(project_dir, chunk_id)
        if not payload:
            tally["no_evaluation"] += 1
            continue

        fb_by_key, fb_by_index = build_dismissed(feedback_by_chunk.get(chunk_id, []))
        tr_by_key = build_triaged(triage_by_chunk.get(chunk_id, []))
        crows_sorted = sorted(crows, key=lambda r: r["chunk_offset_start"])

        raw_text = ""
        chunk_mtime = None
        chunk_path = chunks_dir / f"{chunk_id}.json"
        try:
            cdata = json.loads(chunk_path.read_text(encoding="utf-8"))
            raw_text = cdata.get("translated_text") or ""
            chunk_mtime = chunk_path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            raw_text = ""

        freshness = evaluator_freshness_detail(
            payload,
            current_chunk_sha(project_dir, chunk_id),
            chunk_mtime=chunk_mtime,
        )

        # Keyed by item id, so the occurrences of one repeated word land on one
        # item instead of two or three that no key downstream can tell apart.
        # Emitting them separately put the same id in a prompt more than once,
        # which `pass_.parse_draft` rejects outright, and left a verdict formed
        # on one sentence suppressing occurrences that were never judged.
        by_id: dict[str, dict[str, Any]] = {}
        ruled: set[str] = set()
        unanchorable: dict[str, str] = {}

        for ni in payload.get("normalized_issues") or []:
            if not isinstance(ni, dict):
                continue
            eval_name = ni.get("eval_name")
            if eval_name not in eval_names:
                continue
            loc = ni.get("location") or {}
            if loc.get("side") != "target":
                continue
            char_start = loc.get("char_start")
            if not isinstance(char_start, int):
                continue

            issue_index = ni.get("issue_index")
            key = issue_key(eval_name, ni)
            item_id = f"{chunk_id}:{eval_name}:{issue_index}:{key}"

            existing = by_id.get(item_id)
            if existing is not None:
                existing["occurrences"] += 1
                row = row_containing_offset(crows_sorted, char_start, raw_text)
                if row is not None and row["text_in_chunk"] not in existing["sentences"]:
                    existing["sentences"].append(row["text_in_chunk"])
                continue
            # Ruled on under an earlier occurrence. The answer cannot change with
            # the offset, and counting it again would report three skips for one
            # finding — which is what made `skipped.dismissed` exceed the number
            # of findings a human has actually marked.
            if item_id in ruled:
                continue

            if feedback_mark(fb_by_key, fb_by_index, eval_name, issue_index, ni):
                tally["dismissed"] += 1
                ruled.add(item_id)
                continue
            if is_ignored(ignored_terms, eval_name, ni):
                tally["ignored"] += 1
                ruled.add(item_id)
                continue
            # Any standing verdict counts, `keep` included: re-asking a question
            # already answered spends tokens to learn nothing. A re-run that
            # *should* re-ask clears the sidecar first.
            if triage_mark(tr_by_key, eval_name, ni):
                tally["already_triaged"] += 1
                ruled.add(item_id)
                continue

            match_text = loc.get("match") or ""
            row = row_containing_offset(crows_sorted, char_start, raw_text)
            if row is not None and (
                (freshness.get(eval_name) or {}).get("state") == "stale"
            ) and (not match_text or match_text not in row["text_in_chunk"]):
                unanchorable[item_id] = "stale_moved"
                continue
            if row is None:
                unanchorable[item_id] = "unanchored"
                continue

            by_id[item_id] = {
                "id": item_id,
                "chunk_id": chunk_id,
                "eval_name": eval_name,
                "issue_index": issue_index,
                "issue_key": key,
                "term": issue_term(eval_name, ni) or match_text,
                "rule_id": ni.get("rule_id"),
                "message": ni.get("message") or "",
                "suggestion": ni.get("suggestion"),
                "match": match_text,
                "sentences": [row["text_in_chunk"]],
                "occurrences": 1,
            }

        # Deliberately after the loop: a later occurrence of the same word may
        # anchor where an earlier one could not, so the skip is only real for an
        # id that never produced an item.
        for item_id, reason in unanchorable.items():
            if item_id not in by_id:
                tally[reason] += 1

        items.extend(by_id.values())

    return items


def collect_book(
    project_dir: Path,
    *,
    chapters: Optional[list[str]] = None,
    eval_names: tuple[str, ...] = TRIAGE_EVAL_NAMES,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every triage-worthy finding in a book, and why the rest were passed over.

    ``chapters`` limits the walk; by default every chapter with an alignment is
    read. Returns ``(items, skips)``.
    """
    project_dir = Path(project_dir)
    wanted = list(chapters) if chapters else list(iter_chapters(project_dir))
    skips = _skip_reason_counts()
    items: list[dict[str, Any]] = []
    for chapter in wanted:
        items.extend(
            collect_chapter(project_dir, chapter, eval_names=eval_names, skips=skips)
        )
    return items, skips


def item_prompt_view(
    item: dict[str, Any],
    glossary: Optional[list[str]] = None,
    *,
    number: int,
) -> dict[str, Any]:
    """One item as the model reads it. The key order is the prompt's.

    Deliberately narrower than the stored item: ``issue_key``, ``chunk_id`` and
    ``issue_index`` are the sidecar's business and would only invite the model to
    reason about bookkeeping. ``suggestion`` is left out for the same reason the
    pass proposes no rewrites — it is the checker's guess, and showing it anchors
    the verdict to it.

    ``number`` is the item's 1-based position in its job, and is what the model
    echoes back. The stored ``id`` ends in a 16-hex-character ``issue_key``, and
    a model asked to copy that gets it wrong: on the first real wave, one item
    came back as ``8f3aae189ee24ad1e`` (seventeen characters) and then, on a
    re-run of the same job, as ``8f3c10691e2fdf8c`` — the true key's first three
    characters followed by invention, twice. ``parse_draft`` rejects a draft
    whose ids are not exactly the job's, so each of those cost all 18 findings in
    the job. A small integer is inside what a model can copy reliably, and
    position is the one thing the prompt and the manifest already agree on.
    """
    view = {
        "item": number,
        "eval_name": item["eval_name"],
        "term": item.get("term") or "",
        "message": item.get("message") or "",
        "sentences": list(item.get("sentences") or ()),
    }
    if item.get("rule_id"):
        view["rule_id"] = item["rule_id"]
    view["glossary"] = list(glossary or ())
    return view


__all__ = [
    "TRIAGE_EVAL_NAMES",
    "collect_book",
    "collect_chapter",
    "item_prompt_view",
    "iter_chapters",
]
