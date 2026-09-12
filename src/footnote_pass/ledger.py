"""
The decision ledger: what was proposed, what was cut, and what actually landed.

Everything else in this package records one *end* of the pass. ``scan_commit``
writes every candidate the wave proposed; ``annotations.jsonl`` holds the notes
that reached the book. Between those two sat the whole editorial argument —
which candidates the human cut at Gate 2, which the agent killed during
research, what a gloss said before the operator rewrote it — and none of it was
written down. On the 2026-09-11 ``fabre2`` run that history existed only in a
chat transcript, and ``candidates.json`` (which is replaced wholesale by the
next ``scan-commit``) took the proposals with it.

This module is the join. It reads a decisions document, resolves each row back
to the candidate that proposed it, and appends one line per decision to
``.harness/footnotes/decisions.jsonl``.

**Append-only, deliberately.** ``src/annotations/review.py``'s ``results.json``
is replace-in-place because it is a *plan*: ``apply`` executes it, so a second
commit must merge rather than clobber work still owed. Nothing reads a footnote
ledger as a plan — ``add`` is driven by its ``--json-file`` — so merge logic here
would be liability without a payer. ``store.append_record`` already defines the
superseding rule this inherits: a later row at the same key wins and the earlier
one stays as history, which is exactly what makes a refusal → fix → land
sequence readable afterwards.

**Snapshots, not references.** Each row embeds the candidate's category, claim
and span, and the model that proposed it. A row that merely pointed at
``candidates.json`` would be worthless the moment the next commit rewrote it,
which is the failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.footnote_pass.corpus import footnotes_dir

# ── vocabulary ──────────────────────────────────────────────────────────────
# Named rather than free-text for the reason ``write.py``'s failure codes are:
# the skill, the tests and the report all have to say the same thing about the
# same decision.

VERDICT_KEEP = "keep"
VERDICT_DROP = "drop"
VERDICT_PROPOSED = "proposed"
VERDICT_UNDECIDED = "undecided"
VERDICT_INVALID = "invalid"

OUTCOME_ADDED = "added"
OUTCOME_REFUSED = "refused"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_PLANNED = "planned"
OUTCOME_DROPPED = "dropped"
OUTCOME_OMITTED = "omitted"
OUTCOME_INVALID = "invalid"

# Where a decision was taken. The only field that separates "the scanner
# over-proposes" (many ``gate2``) from "the profile is too loose" (many
# ``research``) — the cross-run question this ledger exists to make answerable.
# Never inferred: an agent that does not say gets ``unspecified``.
STAGES = frozenset({"scan", "gate2", "research", "gate3", "add", "unspecified"})
STAGE_DEFAULT = "unspecified"

# How well a decision row resolved back to the candidate that proposed it.
JOIN_EXACT = "exact"        # the row carried a candidate_key and it matched
JOIN_SENTENCE = "sentence"  # matched (chapter_id, es_idx), and it was unambiguous
JOIN_NONE = "none"          # no candidates, no match, or an ambiguous sentence

SCHEMA = 1

_KEY_SAFE = re.compile(r"^[A-Za-z0-9_\-]+$")


def decisions_path(project_dir: Path | str) -> Path:
    return footnotes_dir(Path(project_dir)) / "decisions.jsonl"


def run_id(when: Optional[datetime] = None) -> str:
    """The stamp shared by a run's ledger rows and its report filename."""
    return (when or datetime.now()).strftime("%Y%m%d_%H%M%S")


def candidate_key(chapter_id: str, es_idx: Any, quoted_span: str) -> Optional[str]:
    """``<chapter_id>__<es_idx>__<span hash>``, or ``None`` if unkeyable.

    ``(chapter_id, es_idx)`` alone is **not** an identity for a candidate, in
    either direction. It is not unique — ``scan_commit`` never dedupes on it, and
    ``write.py`` deliberately allows several notes on one sentence
    (``test_two_notes_on_one_sentence_are_allowed_when_they_differ``). And it is
    not stable — ``es_idx`` is a position in the alignment, which is the whole
    reason ``sentence_drifted`` exists. Hashing the span the scanner quoted pins
    the third axis.

    ``__`` and the ``^[A-Za-z0-9_\\-]+$`` guard are lifted from
    ``store.target_key`` so a footnote key and an annotation key read alike.
    """
    span = (quoted_span or "").strip()
    if not chapter_id or es_idx is None or not span:
        return None
    digest = hashlib.sha1(span.encode("utf-8")).hexdigest()[:8]
    key = f"{chapter_id}__{es_idx}__{digest}"
    return key if _KEY_SAFE.match(key) else None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def split_decisions(
    rows: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """``(keeps, drops, invalid)`` from a decisions document.

    **A row with no ``verdict`` is a keep.** That is exactly the shape
    ``approved.json`` has always had — ``[{chapter_id, es_idx, anchor, note}]`` —
    so every existing batch file keeps working untouched. The split lives here
    rather than in the CLI so any caller of ``add`` gets the same rule from one
    implementation.

    An unrecognised verdict is never coerced to either side: a malformed
    instruction is a real error, and guessing which way the operator meant it is
    how an unapproved gloss lands in a published book.
    """
    keeps: list[dict] = []
    drops: list[dict] = []
    invalid: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            invalid.append({"row": repr(raw)[:200], "problem": "not a JSON object"})
            continue
        verdict = str(raw.get("verdict") or VERDICT_KEEP).strip().lower()
        if verdict == VERDICT_KEEP:
            keeps.append(raw)
        elif verdict == VERDICT_DROP:
            drops.append(raw)
        else:
            invalid.append({**raw, "problem": f"unknown verdict {verdict!r}"})
    return keeps, drops, invalid


def load_candidates(project_dir: Path | str) -> dict[str, Any]:
    """``candidates.json``, indexed for the join. Never raises.

    A missing or unreadable candidates file is normal — ``add`` is also used for
    one-off hand-authored notes with no scan behind them — so it degrades to
    empty indexes rather than failing a write that is otherwise fine.
    """
    from src.footnote_pass.scan import _candidates_path

    path = _candidates_path(Path(project_dir))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"doc": None, "by_key": {}, "by_sentence": {}, "rows": []}
    if not isinstance(doc, dict):
        return {"doc": None, "by_key": {}, "by_sentence": {}, "rows": []}

    rows = [r for r in (doc.get("candidates") or []) if isinstance(r, dict)]
    by_key: dict[str, dict] = {}
    by_sentence: dict[tuple[str, Optional[int]], list[dict]] = {}
    for row in rows:
        chapter_id = str(row.get("chapter_id") or "")
        es_idx = _as_int(row.get("es_idx"))
        key = candidate_key(chapter_id, es_idx, row.get("quoted_span") or "")
        if key:
            by_key[key] = row
        by_sentence.setdefault((chapter_id, es_idx), []).append(row)
    return {"doc": doc, "by_key": by_key, "by_sentence": by_sentence, "rows": rows}


def _join_candidate(
    candidates: dict[str, Any], chapter_id: str, es_idx: Optional[int], key: Any
) -> tuple[Optional[dict], str]:
    """Resolve one decision row back to its candidate, honestly.

    Two tiers, and the *quality* of the match is reported rather than hidden. An
    ambiguous sentence — two candidates on it, and no key to tell them apart —
    degrades to ``none``. A ledger that claims a claim and a category it cannot
    prove belong to this note is worse than one that says it does not know.
    """
    if key and key in candidates["by_key"]:
        return candidates["by_key"][key], JOIN_EXACT
    hits = candidates["by_sentence"].get((chapter_id, es_idx)) or []
    if len(hits) == 1:
        return hits[0], JOIN_SENTENCE
    return None, JOIN_NONE


def _candidate_snapshot(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "category": row.get("category"),
        "claim": row.get("claim"),
        "why": row.get("why"),
        "quoted_span": row.get("quoted_span"),
        "en_sentence": row.get("en_sentence"),
    }


def _provenance(candidates: dict[str, Any]) -> Optional[dict]:
    doc = candidates.get("doc")
    if not doc:
        return None
    return {
        "worker_model": doc.get("worker_model"),
        "prompt_version": doc.get("prompt_version"),
        "profile_path": doc.get("profile_path"),
        "committed_at": doc.get("committed_at"),
    }


def _stage(raw: dict[str, Any], default: str) -> str:
    stage = str(raw.get("stage") or default).strip().lower()
    return stage if stage in STAGES else STAGE_DEFAULT


def _sources(raw: dict[str, Any]) -> list[str]:
    value = raw.get("sources")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def build_run_doc(
    project_dir: Path | str,
    *,
    stamp: str,
    dry_run: bool,
    result: dict[str, Any],
    keeps: list[dict],
    drops: list[dict],
    invalid: list[dict],
    candidates: dict[str, Any],
    decided: bool,
) -> dict[str, Any]:
    """The whole run, joined — the document both the ledger and the report read.

    ``result`` is what ``write.add`` produced (``added`` / ``planned`` /
    ``refused`` rows). ``decided`` says whether the caller passed a decisions
    document at all; an inline single-note ``add`` has no decision set to be
    complete against, so the undecided sweep below must not run for it.
    """
    project_dir = Path(project_dir)
    written_at = datetime.now().isoformat()
    rows: list[dict[str, Any]] = []

    def _base(raw: dict[str, Any], *, verdict: str, stage_default: str) -> dict:
        chapter_id = str(raw.get("chapter_id") or raw.get("chapter") or "")
        es_idx = _as_int(raw.get("es_idx"))
        key = raw.get("candidate_key") or candidate_key(
            chapter_id, es_idx, raw.get("quoted_span") or ""
        )
        cand, join = _join_candidate(candidates, chapter_id, es_idx, key)
        if cand is not None and not key:
            key = candidate_key(chapter_id, es_idx, cand.get("quoted_span") or "")
        return {
            "schema": SCHEMA,
            "run_id": stamp,
            "written_at": written_at,
            "project_id": project_dir.name,
            "dry_run": dry_run,
            "stage": _stage(raw, stage_default),
            "verdict": verdict,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "candidate_key": key,
            "join": join,
            "reason": raw.get("reason"),
            "sources": _sources(raw),
            "candidate": _candidate_snapshot(cand),
            "proposed_by": _provenance(candidates),
        }

    # ── keeps, in the order `add` reported them ─────────────────────────────
    # `added` / `planned` / `refused` are disjoint and together cover every keep
    # that reached the validator, so pairing them back up by position within each
    # bucket is exact — `add` preserves input order inside a bucket.
    by_sentence: dict[tuple[str, Optional[int], str], dict] = {}
    for raw in keeps:
        chapter_id = str(raw.get("chapter_id") or raw.get("chapter") or "")
        note = str(raw.get("note") or raw.get("content") or "")
        by_sentence[(chapter_id, _as_int(raw.get("es_idx")), note)] = raw

    for bucket, outcome in (
        ("added", OUTCOME_ADDED),
        ("planned", OUTCOME_PLANNED),
        ("refused", OUTCOME_REFUSED),
    ):
        for out in result.get(bucket) or []:
            problems = out.get("problems") or []
            codes = [p.get("code") for p in problems]
            # The validator's own `duplicate` refusal is not a loss — the note is
            # already on the book under an earlier sub_id. Saying "refused" for
            # it would read as work destroyed.
            resolved = (
                OUTCOME_DUPLICATE
                if outcome == OUTCOME_REFUSED and "duplicate" in codes
                else outcome
            )
            raw = _match_keep(by_sentence, out)
            row = _base(raw, verdict=VERDICT_KEEP, stage_default="gate3")
            row.update(
                {
                    "outcome": resolved,
                    "anchor": out.get("anchor"),
                    "suggested_anchor": out.get("suggested_anchor"),
                    # The full gloss, always — a refusal means the drafted prose
                    # exists nowhere else on disk, and it cost research to write.
                    "content": out.get("content"),
                    "es_text": out.get("es_text"),
                    # Where the marker actually falls, computed through
                    # `endnotes._injection_point` itself. This is the half of the
                    # review page a one-line summary can never carry.
                    "injection_preview": out.get("injection_preview"),
                    "problems": problems,
                    "warning_codes": [
                        p.get("code") for p in problems if p.get("warning")
                    ],
                    "sub_id": out.get("sub_id"),
                    "annotation_key": _annotation_key(out),
                    "existing_sub_id": out.get("existing_sub_id"),
                }
            )
            rows.append(row)

    # ── drops: never validated, but never lost either ───────────────────────
    for raw in drops:
        row = _base(raw, verdict=VERDICT_DROP, stage_default=STAGE_DEFAULT)
        row.update(
            {
                "outcome": OUTCOME_DROPPED,
                "anchor": raw.get("anchor"),
                "content": raw.get("note") or raw.get("content"),
                "problems": [],
                "warning_codes": [],
                "sub_id": None,
            }
        )
        rows.append(row)

    for raw in invalid:
        row = _base(raw, verdict=VERDICT_INVALID, stage_default=STAGE_DEFAULT)
        row.update(
            {
                "outcome": OUTCOME_INVALID,
                "problem": raw.get("problem"),
                "content": raw.get("note") or raw.get("content"),
                "problems": [],
                "warning_codes": [],
                "sub_id": None,
            }
        )
        rows.append(row)

    # ── candidates this run neither kept nor dropped ────────────────────────
    undecided = _undecided(candidates, keeps, drops) if decided else []
    for cand in undecided:
        chapter_id = str(cand.get("chapter_id") or "")
        es_idx = _as_int(cand.get("es_idx"))
        row = _base(
            {"chapter_id": chapter_id, "es_idx": es_idx,
             "quoted_span": cand.get("quoted_span")},
            verdict=VERDICT_UNDECIDED,
            stage_default="gate2",
        )
        row.update(
            {
                "outcome": OUTCOME_OMITTED,
                "content": None,
                "problems": [],
                "warning_codes": [],
                "sub_id": None,
            }
        )
        rows.append(row)

    return {
        "project": project_dir.name,
        "run_id": stamp,
        "written_at": written_at,
        "dry_run": dry_run,
        "rows": rows,
        "undecided": undecided,
        "candidates_committed_at": (candidates.get("doc") or {}).get("committed_at"),
        "worker_model": (candidates.get("doc") or {}).get("worker_model"),
        "annotations_path": str(project_dir / "annotations.jsonl"),
        "counts": {
            "added": len(result.get("added") or []),
            "planned": len(result.get("planned") or []),
            "refused": len(result.get("refused") or []),
            "dropped": len(drops),
            "invalid": len(invalid),
            "undecided": len(undecided),
        },
    }


def _match_keep(
    by_sentence: dict[tuple[str, Optional[int], str], dict], out: dict
) -> dict:
    """The decision row a result row came from, or a stand-in.

    ``add`` composes ``content`` as ``[anchor] note``, so the note text is not
    recoverable from the result alone; match on the bare note first and fall back
    to the result itself, which carries everything the ledger strictly needs.
    """
    chapter_id = out.get("chapter_id")
    es_idx = out.get("es_idx")
    for (ch, idx, note), raw in by_sentence.items():
        if ch == chapter_id and idx == es_idx and note and note in (out.get("content") or ""):
            return raw
    return dict(out)


def _annotation_key(out: dict) -> Optional[str]:
    """``store.target_key`` for a landed note — the cross-reference to reviews.

    A convenience, not the identity: ``target_key`` embeds ``es_idx``, which a
    re-align moves. ``sub_id`` is the join that survives.
    """
    sub_id = out.get("sub_id")
    if not sub_id:
        return None
    return f"{out.get('chapter_id')}__{out.get('es_idx')}__{sub_id}"


def _undecided(
    candidates: dict[str, Any], keeps: list[dict], drops: list[dict]
) -> list[dict]:
    """Usable candidates that appear in neither a keep nor a drop row.

    Scoped to the chapters this run actually touched. ``candidates.json``
    routinely covers a whole range (21–40) while an ``add`` lands one note in
    ch. 4, and reporting the other nineteen chapters as unfinished business every
    time is how a warning becomes noise everyone learns to ignore.
    """
    decided_rows = list(keeps) + list(drops)
    touched = {
        str(r.get("chapter_id") or r.get("chapter") or "") for r in decided_rows
    }
    seen_keys = set()
    seen_sentences = set()
    for raw in decided_rows:
        chapter_id = str(raw.get("chapter_id") or raw.get("chapter") or "")
        es_idx = _as_int(raw.get("es_idx"))
        if raw.get("candidate_key"):
            seen_keys.add(raw["candidate_key"])
        seen_sentences.add((chapter_id, es_idx))

    out: list[dict] = []
    for row in candidates["rows"]:
        chapter_id = str(row.get("chapter_id") or "")
        if chapter_id not in touched:
            continue
        es_idx = _as_int(row.get("es_idx"))
        key = candidate_key(chapter_id, es_idx, row.get("quoted_span") or "")
        if key and key in seen_keys:
            continue
        if (chapter_id, es_idx) in seen_sentences:
            continue
        out.append(row)
    return out


def proposed_row(
    project_dir: Path | str,
    candidate: dict[str, Any],
    doc: dict[str, Any],
    *,
    stamp: str,
) -> dict[str, Any]:
    """One ``proposed`` row, written by ``scan_commit`` for each usable candidate.

    Without these the ledger can only describe candidates that someone later
    decided on: ``candidates.json`` is replaced by the next commit, so a proposal
    nobody ever ruled on would vanish with it. With them the whole arc —
    proposed, then kept or dropped or never decided — is in one append-only file.
    """
    project_dir = Path(project_dir)
    chapter_id = str(candidate.get("chapter_id") or "")
    es_idx = _as_int(candidate.get("es_idx"))
    return {
        "schema": SCHEMA,
        "run_id": stamp,
        "written_at": datetime.now().isoformat(),
        "project_id": project_dir.name,
        "dry_run": False,
        "stage": "scan",
        "verdict": VERDICT_PROPOSED,
        "outcome": VERDICT_PROPOSED,
        "chapter_id": chapter_id,
        "es_idx": es_idx,
        "candidate_key": candidate.get("candidate_key")
        or candidate_key(chapter_id, es_idx, candidate.get("quoted_span") or ""),
        "join": JOIN_EXACT,
        "es_text": candidate.get("es_sentence"),
        "candidate": _candidate_snapshot(candidate),
        "proposed_by": {
            "worker_model": doc.get("worker_model"),
            "prompt_version": doc.get("prompt_version"),
            "profile_path": doc.get("profile_path"),
            "committed_at": doc.get("committed_at"),
        },
        "problems": [],
        "warning_codes": [],
        "sub_id": None,
    }


def append_decisions(project_dir: Path | str, rows: list[dict[str, Any]]) -> Path:
    """Append one line per decision. The single writer of ``decisions.jsonl``.

    Append-only, for the reason ``store.append_record`` is: nothing here ever
    rewrites a line, so a run is always recoverable from the log, and a second
    decision on the same candidate supersedes rather than erases the first.
    """
    path = decisions_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_decisions(project_dir: Path | str) -> list[dict[str, Any]]:
    """Every ledger row, oldest first. Skips an undecodable line, as the
    reader's own ``load_annotations`` does — one bad line must not hide the rest."""
    path = decisions_path(project_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def latest_by_candidate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Current state per candidate: the last row wins, earlier ones are history."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("candidate_key")
        if key:
            out[key] = row
    return out
