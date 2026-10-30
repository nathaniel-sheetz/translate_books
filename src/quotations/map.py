"""
The mapping wave and its validator: from a resolved quotation to proposed edits.

One job per chapter that has resolved quotations, epigraphs or allusions, on the
manifest pattern of :mod:`src.decisions.wave`. The body carries the whole chapter
(numbered ``es_idx | EN | ES`` rows; the largest short-sermons chapter is about
1,350 words) and, per candidate, the full NBLA text of its verses. The worker
returns, per candidate:

- ``replace``: ``[{es_idx, old, pieces}]``, one item per Spanish row the
  quotation spans: the translator's words to replace and the NBLA words that
  replace them, in order;
- ``echo``: ``[{es_idx, old, new, en_evidence}]``, nearby prose that should take
  the verse's NBLA wording;
- ``drift``: a note when the KJV and the NBLA differ in meaning.

**The worker never writes the quotation's Spanish.** :func:`build_card` checks
every string and builds it with :mod:`src.quotations.conventions`:

- each piece must be a verbatim substring of the indexed verses (markers and
  numbers stripped, whitespace normalized);
- each ``old`` must occur exactly once in its row, and each ``en_evidence`` in
  that row's English;
- overlapping edits are refused, an echo edit inside a quotation included.

``map-commit`` writes ``mapped.json``: one **card** per candidate the scan kept,
whatever happened to it, which is what the board shows. A revision the user
asked for (``revisions.json``) replaces the wave's mapping for its card, and a
card already applied is carried over as it was.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.decisions.scan import normalize_for_match
from src.decisions.wave import fanout_error, load_manifest, run_wave
from src.judges.llm_io import JudgeParseError, load_template, parse_judge_json, prompt_version
from src.quotations import quotations_dir
from src.quotations import scan as qscan
from src.quotations.conventions import (
    fills_paragraph,
    is_sentence_initial,
    locate,
    render_quotation,
    splice,
)
from src.quotations.index import IndexError_, load_index, plain
from src.quotations.resolve import STATUS_RESOLVED, resolved_path

logger = logging.getLogger(__name__)

TEMPLATE = "quotation_map.txt"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_quotations_map``.
COMMAND = "quotations_map"

#: Kinds whose wording the wave maps. A reference-only card gets its reference
#: fix and nothing else; not-Scripture gets a paste box.
MAPPED_KINDS = ("quotation", "epigraph", "allusion")

REQUIRED_FIELDS = ("chapter_id", "mappings")

# Card status, what the board shows first.
CARD_PROPOSED = "proposed"          # at least one edit that changes the text
CARD_MATCHES = "matches"            # mapped, and the Spanish already is the NBLA
CARD_NO_MAPPING = "no_mapping"      # the worker returned nothing to replace
CARD_REFUSED = "refused"            # everything the worker returned failed validation
CARD_REFERENCE = "reference"        # reference only: the reference fix, if any
CARD_UNRESOLVED = "unresolved"
CARD_NOT_SCRIPTURE = "not_scripture"
CARD_UNMAPPED = "unmapped"          # its chapter's draft is missing or failed

# Why an edit was refused.
REFUSE_PIECE = "piece_not_verbatim"
REFUSE_OLD = "old_not_in_row"
REFUSE_AMBIGUOUS = "old_ambiguous_in_row"
REFUSE_ROW = "unknown_row"
REFUSE_EVIDENCE = "en_evidence_not_in_row"
REFUSE_OVERLAP = "overlaps_another_edit"
REFUSE_EMPTY = "missing_fields"
REFUSE_PIECE_NOT_IN_NEW = "piece_not_in_new"     # a revision's own wording drops an NBLA piece


def _manifest_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "map.manifest.json"


def mapped_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "mapped.json"


def revisions_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "revisions.json"


def load_revisions(project_dir: Path) -> dict[str, dict[str, Any]]:
    """``{card_key: {mapping, ask, at}}``: the wordings the user asked for, which win
    over the wave's mapping on every commit (:mod:`src.quotations.revise`)."""
    try:
        doc = json.loads(revisions_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict) and isinstance(v.get("mapping"), dict)} \
        if isinstance(doc, dict) else {}


def mark_revised(card: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    """Label a card built from a revision; one with its own wording says so in ``why``."""
    ask = revision.get("ask") or ""
    card["revised"] = {"ask": ask, "at": revision.get("at")}
    if card.get("adapted") and not card.get("why"):
        card["why"] = f"revised: {ask}" if ask else "revised"
    return card


def load_resolved(project_dir: Path) -> Optional[dict[str, Any]]:
    path = resolved_path(project_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def _mappable(cand: dict[str, Any]) -> bool:
    return cand.get("status") == STATUS_RESOLVED and cand.get("kind") in MAPPED_KINDS


def render_candidates(cands: list[tuple[str, dict[str, Any]]]) -> str:
    lines = ["CANDIDATES:"]
    for cid, cand in cands:
        rows = ", ".join(str(i) for i in cand.get("es_idx") or []) or "none found"
        lines.append("")
        lines.append(f"{cid}: {cand['kind']}, {cand.get('ref')}, rows [{rows}]")
        lines.append(f"  EN: {cand['en_span']}")
        if cand.get("es_span"):
            lines.append(f"  Current ES: {cand['es_span']}")
        if cand.get("adapted"):
            lines.append(f"  adapted on purpose: {cand.get('why') or 'yes'}")
        elif cand.get("kind") == "allusion" and cand.get("why"):
            lines.append(f"  note: {cand['why']}")
        lines.append("  NBLA:")
        for verse in cand.get("verses") or []:
            lines.append(f"    ({verse['label']}) {plain(verse['text'])}")
    return "\n".join(lines)


def render_body(chapter_id: str, items: list[dict], cands: list[tuple[str, dict[str, Any]]]) -> str:
    rows_part = qscan.render_body(chapter_id, items, [])
    rows_part = rows_part.replace(
        "WRITTEN REFERENCES in this chapter's English: none\n\n", ""
    ).replace("\n\nReturn the JSON object for this chapter.", "")
    return (
        rows_part
        + "\n\n"
        + render_candidates(cands)
        + "\n\nReturn the JSON object for this chapter, one mapping per candidate id."
    )


_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "jobs": "one job per chapter with resolved quotations, epigraphs or allusions",
    "candidates": "candidates sent to the wave",
    "words": "English words across the jobs (the chapter is sent whole)",
    "manifest_path": "map.manifest.json",
    "effective": "what the wave will run as; quote it at the usage gate",
    "instructions": "next step",
}


def map_prepare(
    project_dir: Path,
    *,
    chapters: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render one mapping prompt per chapter plus the manifest (no spend)."""
    from src.harness.profile import resolve_profile, usage_log_for

    project_dir = Path(project_dir)
    resolved = load_resolved(project_dir)
    if resolved is None:
        return {"status": "error", "error": "no resolved.json; run `resolve` first", "_schema": _PREPARE_SCHEMA}

    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for cand in resolved.get("candidates") or []:
        if _mappable(cand):
            by_chapter.setdefault(cand["chapter_id"], []).append(cand)
    wanted = [c for c in qscan.chapter_ids(project_dir) if c in by_chapter]
    if chapters:
        unknown = sorted(set(chapters) - set(qscan.chapter_ids(project_dir)))
        if unknown:
            return {"status": "error", "error": f"chapters not found: {unknown}", "_schema": _PREPARE_SCHEMA}
        wanted = [c for c in wanted if c in set(chapters)]

    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli,
        worker_model=worker_model,
        usage_log=usage_log_for(project_dir, COMMAND),
    )
    qdir = quotations_dir(project_dir)
    qdir.mkdir(parents=True, exist_ok=True)
    preamble = load_template(TEMPLATE).rstrip("\n") + "\n\n"
    preamble_path = qdir / "preamble.map.txt"
    preamble_path.write_text(preamble, encoding="utf-8")

    jobs: list[dict[str, Any]] = []
    total_cands = total_words = 0
    for chapter_id in wanted:
        cands = [(f"c{i}", c) for i, c in enumerate(by_chapter[chapter_id], start=1)]
        items = qscan.interleave(
            qscan.load_rows(project_dir, chapter_id), qscan.chunk_sources(project_dir, chapter_id)
        )
        body = render_body(chapter_id, items, cands)
        body_path = qdir / f"{chapter_id}.map.body.txt"
        prompt_path = qdir / f"{chapter_id}.map.prompt.txt"
        draft_path = qdir / f"{chapter_id}.map.draft.json"
        body_path.write_text(body, encoding="utf-8")
        prompt_path.write_text(preamble + body, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)
        words = len(qscan.chapter_english(project_dir, chapter_id).split())
        total_cands += len(cands)
        total_words += words
        jobs.append({
            "id": chapter_id,
            "chapter_id": chapter_id,
            "candidates": {cid: c["key"] for cid, c in cands},
            "words": words,
            "body_path": str(body_path),
            "prompt_path": str(prompt_path),
            "draft_path": str(draft_path),
        })

    manifest = {
        "project": project_dir.name,
        "prepared_at": datetime.now().isoformat(),
        "prompt_version": prompt_version(TEMPLATE),
        "chapters": wanted,
        "preamble_path": str(preamble_path),
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "jobs": jobs,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "status": "ok",
        "jobs": len(jobs),
        "candidates": total_cands,
        "words": total_words,
        "chapters": wanted,
        "manifest_path": str(_manifest_path(project_dir)),
        "preamble_tokens_est": len(preamble) // 4,
        "effective": prof.to_payload(),
        "instructions": (
            "Quote jobs, candidates and `effective` at the usage gate (parallel and "
            "sequential minutes). Then `map-fanout`, or spawn one `quotation-map-worker` "
            "per job (prompt_path -> draft_path). Then `map-commit`."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


def _load_manifest(project_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    return load_manifest(
        _manifest_path(project_dir), missing="no quotation map manifest; run `map-prepare` first"
    )


def map_fanout(
    project_dir: Path,
    *,
    job_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless wave over the prepared mapping jobs."""
    project_dir = Path(project_dir)
    manifest, err = _load_manifest(project_dir)
    if err:
        return fanout_error(err)
    return run_wave(
        project_dir, manifest, command=COMMAND, job_ids=job_ids, concurrency=concurrency,
        cli=cli, cli_bin=cli_bin, effort=effort, runner=runner,
        root=quotations_dir(project_dir),
    )


# ---------------------------------------------------------------------------
# validate + build
# ---------------------------------------------------------------------------

class ChapterText:
    """A chapter's rows and the paragraphs of its chunks, for placing an edit."""

    def __init__(self, project_dir: Path, chapter_id: str):
        self.rows = {
            r["es_idx"]: r for r in qscan.load_rows(project_dir, chapter_id) if "es_idx" in r
        }
        self.paragraphs: dict[str, list[str]] = {}
        for path in sorted((Path(project_dir) / "chunks").glob(f"{chapter_id}_chunk_*.json")):
            try:
                chunk = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            text = chunk.get("translated_text") or ""
            self.paragraphs[chunk.get("id") or path.stem] = [
                p for p in re.split(r"\n\s*\n", text) if p.strip()
            ]

    def es(self, es_idx: int) -> Optional[str]:
        row = self.rows.get(es_idx)
        return None if row is None else row.get("es") or ""

    def en(self, es_idx: int) -> str:
        row = self.rows.get(es_idx) or {}
        return row.get("en") or ""

    def paragraph(self, es_idx: int, old: str) -> str:
        """The chunk paragraph holding ``old`` (from row ``es_idx``), else the row."""
        row = self.rows.get(es_idx) or {}
        paras = self.paragraphs.get(row.get("chunk_id") or "", [])
        es = row.get("es") or ""
        for para in paras:
            if es and es in para and old in para:
                return para
        for para in paras:
            if old in para:
                return para
        return es


def _span(row_text: str, old: str) -> Optional[tuple[int, int]]:
    at = row_text.find(old)
    return None if at < 0 else (at, at + len(old))


class _Claims:
    """Spans already taken in each row; a later overlapping edit is refused."""

    def __init__(self) -> None:
        self.spans: dict[int, list[tuple[int, int]]] = {}

    def take(self, es_idx: int, span: tuple[int, int]) -> bool:
        for start, end in self.spans.get(es_idx, []):
            if span[0] < end and start < span[1]:
                return False
        self.spans.setdefault(es_idx, []).append(span)
        return True


def _check_old(text: ChapterText, es_idx: Any, old: str) -> tuple[Optional[int], Optional[str]]:
    try:
        es_idx = int(es_idx)
    except (TypeError, ValueError):
        return None, REFUSE_ROW
    row_es = text.es(es_idx)
    if row_es is None:
        return es_idx, REFUSE_ROW
    if not old or old not in row_es:
        return es_idx, REFUSE_OLD
    if row_es.count(old) > 1:
        return es_idx, REFUSE_AMBIGUOUS
    return es_idx, None


def build_card(
    cand: dict[str, Any],
    mapping: Optional[dict[str, Any]],
    text: ChapterText,
    claims: _Claims,
    proper: frozenset[str],
    *,
    fix_caps: bool = True,
    allow_new: bool = False,
) -> dict[str, Any]:
    """One board card: the candidate, its validated edits, and why any were refused.

    ``allow_new`` is for a revision the user asked for (:mod:`src.quotations.revise`),
    never for the wave: a ``replace`` item may then carry ``new``, the Spanish to
    use as written. Its pieces must still be verbatim NBLA and must all be in
    ``new``; the card is marked adapted, since code no longer built the wording.
    """
    card = {
        k: cand.get(k)
        for k in (
            "key", "chapter_id", "kind", "ref", "ref_en", "ref_key", "en_span", "es_span",
            "es_idx", "adapted", "why", "confidence", "origin", "flags", "written_ref",
            "ref_edit", "ref_ok", "verse_keys", "unresolved_reason",
        )
    }
    card["quote_edits"] = []
    card["echo"] = []
    card["refusals"] = []
    card["drift"] = ""
    card["note"] = ""

    # The reference fix claims its span first: it is code-made and always valid.
    ref_edit = cand.get("ref_edit")
    if ref_edit:
        row_es = text.es(ref_edit["es_idx"]) or ""
        span = _span(row_es, ref_edit["old"])
        if span is None or not claims.take(ref_edit["es_idx"], span):
            card["refusals"].append({"what": "ref_edit", "reason": REFUSE_OVERLAP if span else REFUSE_OLD})
            card["ref_edit"] = None

    status = cand.get("status")
    if status == "not_scripture":
        card["status"] = CARD_NOT_SCRIPTURE
        return card
    if status != STATUS_RESOLVED:
        card["status"] = CARD_UNRESOLVED
        return card
    if cand.get("kind") not in MAPPED_KINDS:
        card["status"] = CARD_REFERENCE
        return card
    if mapping is None:
        card["status"] = CARD_UNMAPPED
        return card

    card["drift"] = str(mapping.get("drift") or "").strip()
    card["note"] = str(mapping.get("note") or "").strip()
    if mapping.get("adapted") and not card.get("adapted"):
        # The mapper reads the verse beside the author's words, which the scanner
        # never saw; an adaptation it spots counts the same way.
        card["adapted"] = True
        card["why"] = card.get("why") or card["note"] or "adapted (mapping wave)"
    verses = [(v["key"], v["text"]) for v in cand.get("verses") or []]

    replace = [r for r in mapping.get("replace") or [] if isinstance(r, dict)]
    # Every row of one quotation, for the block-vs-inline call.
    olds_by_para: dict[str, list[str]] = {}
    checked: list[tuple[int, str, list[Any], Optional[str]]] = []
    for item in replace:
        old = str(item.get("old") or "")
        pieces = [str(p) for p in item.get("pieces") or [] if str(p).strip()]
        free = str(item["new"]) if allow_new and item.get("new") is not None else None
        if not old or not pieces or free == "":
            card["refusals"].append({"what": "replace", "es_idx": item.get("es_idx"), "old": old, "reason": REFUSE_EMPTY})
            continue
        es_idx, reason = _check_old(text, item.get("es_idx"), old)
        if reason:
            card["refusals"].append({"what": "replace", "es_idx": es_idx, "old": old, "reason": reason})
            continue
        located = []
        for piece in pieces:
            loc = locate(verses, piece)
            if loc is None:
                card["refusals"].append({
                    "what": "replace", "es_idx": es_idx, "old": old, "piece": piece,
                    "reason": REFUSE_PIECE,
                })
                located = []
                break
            if free is not None and normalize_for_match(loc.text) not in normalize_for_match(free):
                card["refusals"].append({
                    "what": "replace", "es_idx": es_idx, "old": old, "piece": piece,
                    "reason": REFUSE_PIECE_NOT_IN_NEW,
                })
                located = []
                break
            located.append(loc)
        if not located:
            continue
        para = text.paragraph(es_idx, old)
        olds_by_para.setdefault(para, []).append(old)
        checked.append((es_idx, old, located, free))

    for es_idx, old, located, free in checked:
        span = _span(text.es(es_idx) or "", old)
        if not claims.take(es_idx, span):
            card["refusals"].append({"what": "replace", "es_idx": es_idx, "old": old, "reason": REFUSE_OVERLAP})
            continue
        para = text.paragraph(es_idx, old)
        block = fills_paragraph(para, olds_by_para.get(para, [old]))
        if free is not None:
            new = free
            card["adapted"] = True
        else:
            at = para.find(old)
            initial = is_sentence_initial(para[:at] if at >= 0 else "")
            rendered = render_quotation(
                located, inline=not block, sentence_initial=initial, proper=proper, old=old,
                fix_caps=fix_caps,
            )
            new = splice(old, rendered)
        keys = list(dict.fromkeys(k for loc in located for k in loc.verse_keys))
        card["quote_edits"].append({
            "es_idx": es_idx,
            "chunk_id": (text.rows.get(es_idx) or {}).get("chunk_id"),
            "old": old,
            "new": new,
            "pieces": [loc.text for loc in located],
            "verse_keys": keys,
            "inline": not block,
            "nbla_words": sum(len(loc.text.split()) for loc in located),
        })

    for item in mapping.get("echo") or []:
        if not isinstance(item, dict):
            continue
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        evidence = str(item.get("en_evidence") or "")
        if not old or not new or not evidence or old == new:
            card["refusals"].append({"what": "echo", "old": old, "reason": REFUSE_EMPTY})
            continue
        es_idx, reason = _check_old(text, item.get("es_idx"), old)
        if not reason and normalize_for_match(evidence) not in normalize_for_match(text.en(es_idx)):
            reason = REFUSE_EVIDENCE
        if not reason and not claims.take(es_idx, _span(text.es(es_idx) or "", old)):
            reason = REFUSE_OVERLAP
        if reason:
            card["refusals"].append({"what": "echo", "es_idx": es_idx, "old": old, "new": new, "reason": reason})
            continue
        card["echo"].append({
            "es_idx": es_idx,
            "chunk_id": (text.rows.get(es_idx) or {}).get("chunk_id"),
            "old": old,
            "new": new,
            "en_evidence": evidence,
        })

    if any(e["old"] != e["new"] for e in card["quote_edits"]) or card["echo"]:
        card["status"] = CARD_PROPOSED
    elif card["quote_edits"]:
        card["status"] = CARD_MATCHES
    elif card["refusals"]:
        card["status"] = CARD_REFUSED
    else:
        card["status"] = CARD_NO_MAPPING
    return card


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

def parse_map_draft(raw: str, *, chapter_id: str) -> dict[str, dict[str, Any]]:
    """``{candidate_id: mapping}`` from one worker draft."""
    data = parse_judge_json(raw, REQUIRED_FIELDS)
    echoed = str(data.get("chapter_id") or "").strip()
    if echoed and echoed != chapter_id:
        raise JudgeParseError(f"chapter mismatch: draft echoed {echoed!r}, expected {chapter_id!r}")
    mappings = data.get("mappings")
    if not isinstance(mappings, list):
        raise JudgeParseError("'mappings' must be a list")
    return {str(m.get("id")): m for m in mappings if isinstance(m, dict) and m.get("id")}


_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some chapters failed or missing) | 'error'",
    "counts": "{cards, by_status}",
    "edits": "{quote_edits, changing, echo, ref_edits}",
    "refusals": "{reason: count}: what the validator would not let through",
    "failed": "list of {id, problem}",
    "missing": "chapter ids with no draft",
    "mapped_path": "mapped.json, which the board reads",
    "instructions": "next step",
}


def _read_mapped(project_dir: Path) -> Optional[dict[str, Any]]:
    try:
        doc = json.loads(mapped_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def map_commit(project_dir: Path) -> dict[str, Any]:
    """Validate every mapping and write one card per candidate to ``mapped.json``."""
    project_dir = Path(project_dir)
    resolved = load_resolved(project_dir)
    if resolved is None:
        return {"status": "error", "error": "no resolved.json; run `resolve` first", "_schema": _COMMIT_SCHEMA}
    manifest, err = _load_manifest(project_dir)
    if err:
        return {"status": "error", "error": err, "_schema": _COMMIT_SCHEMA}
    try:
        index = load_index(resolved.get("version") or "nbla")
    except IndexError_ as exc:
        return {"status": "error", "error": str(exc), "_schema": _COMMIT_SCHEMA}
    qdir = quotations_dir(project_dir).resolve()

    mappings: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    for job in manifest["jobs"]:
        chapter_id = job.get("chapter_id") or job.get("id")
        draft = Path(job.get("draft_path") or "")
        if not draft.resolve().is_relative_to(qdir):
            failed.append({"id": chapter_id, "problem": f"draft_path escapes quotations/: {draft}"})
            continue
        if not draft.exists():
            missing.append(chapter_id)
            continue
        try:
            by_id = parse_map_draft(draft.read_text(encoding="utf-8"), chapter_id=chapter_id)
        except (OSError, UnicodeDecodeError, JudgeParseError) as exc:
            failed.append({"id": chapter_id, "problem": str(exc)[:500]})
            continue
        for cid, key in (job.get("candidates") or {}).items():
            if cid in by_id:
                mappings[key] = by_id[cid]

    from src.quotations.apply import applied_cards

    revisions = load_revisions(project_dir)
    # An applied card keeps the card it was applied from: its `old` text is gone
    # from the chapter, so rebuilding it would only refuse it.
    previous = {c["key"]: c for c in (_read_mapped(project_dir) or {}).get("cards") or []}
    applied = applied_cards(project_dir)

    cards: list[dict[str, Any]] = []
    texts: dict[str, ChapterText] = {}
    claims: dict[str, _Claims] = {}
    for cand in resolved.get("candidates") or []:
        key = cand["key"]
        if key in applied and key in previous:
            cards.append(previous[key])
            continue
        chapter_id = cand["chapter_id"]
        if chapter_id not in texts:
            texts[chapter_id] = ChapterText(project_dir, chapter_id)
            claims[chapter_id] = _Claims()
        revision = revisions.get(key)
        card = build_card(
            cand, revision["mapping"] if revision else mappings.get(key), texts[chapter_id],
            claims[chapter_id], index.proper,
            fix_caps=not index.doc.get("smallcaps_resolved"), allow_new=revision is not None,
        )
        cards.append(mark_revised(card, revision) if revision else card)

    by_status: dict[str, int] = {}
    refusals: dict[str, int] = {}
    for card in cards:
        by_status[card["status"]] = by_status.get(card["status"], 0) + 1
        for r in card["refusals"]:
            refusals[r["reason"]] = refusals.get(r["reason"], 0) + 1
    doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "version": resolved.get("version"),
        "prompt_version": manifest.get("prompt_version"),
        "worker_model": manifest.get("worker_model"),
        "range_separator": resolved.get("range_separator"),
        "cards": cards,
        "failed": failed,
        "missing": missing,
    }
    path = mapped_path(project_dir)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    quote_edits = [e for c in cards for e in c["quote_edits"]]
    return {
        "status": "partial" if failed or missing else "ok",
        "counts": {"cards": len(cards), "by_status": by_status},
        "edits": {
            "quote_edits": len(quote_edits),
            "changing": sum(1 for e in quote_edits if e["old"] != e["new"]),
            "echo": sum(len(c["echo"]) for c in cards),
            "ref_edits": sum(1 for c in cards if c.get("ref_edit")),
        },
        "refusals": refusals,
        "failed": failed,
        "missing": missing,
        "mapped_path": str(path),
        "instructions": (
            "Re-run `map-fanout` for anything in failed/missing, then commit again. "
            "Otherwise open the board."
        ),
        "_schema": _COMMIT_SCHEMA,
    }
