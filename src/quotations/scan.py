"""
The detection wave: one job per aligned chapter, every quotation it carries.

Prepare / fanout / commit on the manifest pattern of :mod:`src.decisions.wave`
(``{preamble_path, worker_model, cli, effort, jobs}``; a job whose draft exists
is skipped on a re-run).

**The body is the aligned chapter, both languages.** Numbered ``es_idx | EN | ES``
rows, as in :func:`src.footnote_pass.scan.render_body`, because a candidate has
to name the Spanish row that renders the English quotation. Unlike footnote-pass
the English is always shown: detection is done on the English, where the KJV
wording is recognisable. English the alignment left out is shown too, as
``[-] EN-only`` lines found by walking each chunk's source text between aligned
rows. On short-sermons one of those is a sermon's epigraph.

**Written references have a deterministic floor.** :func:`src.quotations.refs.find_refs`
finds every written reference in the chapter's English before the wave runs; they
are listed in the body, and ``scan_commit`` turns any the wave did not attach to
a candidate into a ``reference_only`` card of its own (``origin: "coded"``).

**Candidates are keyed on ``(chapter_id, ref, hash of en_span)``**, never on
``es_idx``, which moves every time a chapter is realigned.
"""

from __future__ import annotations

import hashlib
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
from src.quotations.refs import FoundRef, find_es_refs, find_refs, parse_ref

logger = logging.getLogger(__name__)

TEMPLATE = "quotation_scan.txt"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_quotations_scan``.
COMMAND = "quotations_scan"

KINDS = ("quotation", "epigraph", "allusion", "reference_only", "not_scripture")
CONFIDENCE = ("high", "medium", "low")

REQUIRED_FIELDS = ("chapter_id", "candidates")

UNUSABLE_FIELDS = "missing_fields"
UNUSABLE_KIND = "unknown_kind"
UNUSABLE_EN = "en_span_not_in_chapter"
UNUSABLE_ROW = "no_aligned_row"
UNUSABLE_DUPLICATE = "duplicate"

#: How far (in characters of the chapter's English) a candidate may sit from a
#: written reference and still be the quotation that reference cites.
REF_REACH = 600


def _manifest_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "scan.manifest.json"


def candidates_path(project_dir: Path) -> Path:
    return quotations_dir(project_dir) / "candidates.json"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def chapter_ids(project_dir: Path) -> list[str]:
    """Every chapter with an alignment file, in book order."""
    align_dir = Path(project_dir) / "alignments"
    if not align_dir.is_dir():
        return []
    return sorted(p.stem for p in align_dir.glob("*.json"))


def load_rows(project_dir: Path, chapter_id: str) -> list[dict]:
    from src.footnote_pass.corpus import load_alignment_rows

    return load_alignment_rows(project_dir, chapter_id)


def chunk_sources(project_dir: Path, chapter_id: str) -> list[tuple[str, str]]:
    """``[(chunk_id, source_text)]`` for the chapter, in order."""
    out = []
    for path in sorted((Path(project_dir) / "chunks").glob(f"{chapter_id}_chunk_*.json")):
        try:
            chunk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("unreadable chunk %s: %s", path, exc)
            continue
        out.append((chunk.get("id") or path.stem, chunk.get("source_text") or ""))
    return out


def chapter_english(project_dir: Path, chapter_id: str) -> str:
    """The chapter's English as the chunks hold it, chunks joined by a blank line."""
    return "\n\n".join(src for _cid, src in chunk_sources(project_dir, chapter_id))


def _ws_pattern(text: str) -> Optional[re.Pattern[str]]:
    tokens = text.split()
    if not tokens:
        return None
    return re.compile(r"\s+".join(re.escape(t) for t in tokens))


def interleave(rows: list[dict], sources: list[tuple[str, str]]) -> list[dict]:
    """The alignment rows with the English they never covered put back in place.

    Each item is a row (``{"es_idx", "en", "es"}``) or ``{"en_only": text}``. The
    walk is by position in each chunk's source text, not by sentence index, so a
    splitter change since the alignment was built cannot shift it.
    """
    by_chunk: dict[str, list[dict]] = {}
    for row in rows:
        by_chunk.setdefault(row.get("chunk_id") or "", []).append(row)
    out: list[dict] = []
    seen_chunks: set[str] = set()

    def gap(text: str) -> None:
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if re.search(r"[A-Za-z]", para):
                out.append({"en_only": para})

    for chunk_id, source in sources:
        seen_chunks.add(chunk_id)
        cursor = 0
        last_en = None
        for row in by_chunk.get(chunk_id, []):
            en = row.get("en") or ""
            if en and en != last_en:
                pattern = _ws_pattern(en)
                match = pattern.search(source, cursor) if pattern else None
                if match:
                    gap(source[cursor : match.start()])
                    cursor = match.end()
            last_en = en
            out.append(row)
        gap(source[cursor:])
    for chunk_id, chunk_rows in by_chunk.items():
        if chunk_id not in seen_chunks:
            out.extend(chunk_rows)
    return out


def render_body(chapter_id: str, items: list[dict], written: list[FoundRef]) -> str:
    """Numbered ``[es_idx] EN / ES`` rows; an EN line only when it changes."""
    lines = [f"CHAPTER: {chapter_id}", ""]
    if written:
        refs = "; ".join(dict.fromkeys(f.ref.text for f in written))
        lines += [
            f"WRITTEN REFERENCES in this chapter's English ({len(written)}): {refs}",
            "Every one of these must be attached to a candidate.",
            "",
        ]
    else:
        lines += ["WRITTEN REFERENCES in this chapter's English: none", ""]
    lines.append("ROWS:")
    last_en = None
    for item in items:
        if "en_only" in item:
            lines.append(f"[-] EN-only (no Spanish row of its own): {item['en_only']}")
            last_en = None
            continue
        en = " ".join((item.get("en") or "").split())
        es = " ".join((item.get("es") or "").split())
        lines.append(f"[{item.get('es_idx')}]")
        if en and en != last_en:
            lines.append(f"  EN: {en}")
        lines.append(f"  ES: {es}")
        last_en = en
    lines += ["", "Return the JSON object for this chapter."]
    return "\n".join(lines)


def build_preamble() -> str:
    return load_template(TEMPLATE).rstrip("\n") + "\n\n"


# ---------------------------------------------------------------------------
# prepare / fanout
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "jobs": "one job per chapter with an alignment",
    "rows": "aligned Spanish rows across the jobs",
    "en_only": "English paragraphs the alignment left out, shown to the scanner as [-] lines",
    "written_refs": "written Bible references found in the English by code (the recall floor)",
    "words": "English words across the jobs",
    "manifest_path": "scan.manifest.json (scan-fanout and scan-commit read it)",
    "effective": "what the wave will run as, with provenance; quote it at the usage gate",
    "instructions": "next step",
}


def scan_prepare(
    project_dir: Path,
    *,
    chapters: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render one scan prompt per chapter plus the manifest (no spend)."""
    from src.harness.profile import resolve_profile, usage_log_for

    project_dir = Path(project_dir)

    def error(message: str) -> dict[str, Any]:
        return {"status": "error", "error": message, "_schema": _PREPARE_SCHEMA}

    available = chapter_ids(project_dir)
    if not available:
        return error(f"no alignment files in {project_dir / 'alignments'}; align the book first")
    if chapters:
        unknown = sorted(set(chapters) - set(available))
        if unknown:
            return error(f"chapters not found in alignments/: {unknown}")
        wanted = [c for c in available if c in set(chapters)]
    else:
        wanted = available

    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli,
        worker_model=worker_model,
        usage_log=usage_log_for(project_dir, COMMAND),
    )

    qdir = quotations_dir(project_dir)
    qdir.mkdir(parents=True, exist_ok=True)
    preamble = build_preamble()
    preamble_path = qdir / "preamble.scan.txt"
    preamble_path.write_text(preamble, encoding="utf-8")

    jobs: list[dict[str, Any]] = []
    totals = {"rows": 0, "en_only": 0, "written_refs": 0, "words": 0}
    for chapter_id in wanted:
        rows = load_rows(project_dir, chapter_id)
        sources = chunk_sources(project_dir, chapter_id)
        items = interleave(rows, sources)
        english = "\n\n".join(s for _c, s in sources)
        written = find_refs(english)
        body = render_body(chapter_id, items, written)
        body_path = qdir / f"{chapter_id}.scan.body.txt"
        prompt_path = qdir / f"{chapter_id}.scan.prompt.txt"
        draft_path = qdir / f"{chapter_id}.scan.draft.json"
        body_path.write_text(body, encoding="utf-8")
        prompt_path.write_text(preamble + body, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)
        words = len(english.split())
        en_only = sum(1 for i in items if "en_only" in i)
        totals["rows"] += len(rows)
        totals["en_only"] += en_only
        totals["written_refs"] += len(written)
        totals["words"] += words
        jobs.append(
            {
                "id": chapter_id,
                "chapter_id": chapter_id,
                "rows": len(rows),
                "en_only": en_only,
                "written_refs": len(written),
                "words": words,
                "body_path": str(body_path),
                "prompt_path": str(prompt_path),
                "draft_path": str(draft_path),
            }
        )

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
        **totals,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "status": "ok",
        "jobs": len(jobs),
        **totals,
        "chapters": wanted,
        "manifest_path": str(_manifest_path(project_dir)),
        "preamble_tokens_est": len(preamble) // 4,
        "effective": prof.to_payload(),
        "instructions": (
            "Quote jobs, words and `effective` at the usage gate (parallel and "
            "sequential minutes). Then run `scan-fanout` for a headless wave, or spawn "
            "one `quotation-scan-worker` per job (prompt_path -> draft_path). Then "
            "`scan-commit`."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


def _load_manifest(project_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    return load_manifest(
        _manifest_path(project_dir), missing="no quotation scan manifest; run `scan-prepare` first"
    )


def scan_fanout(
    project_dir: Path,
    *,
    job_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless wave over the prepared chapter jobs."""
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
# commit
# ---------------------------------------------------------------------------

def span_hash(en_span: str) -> str:
    return hashlib.sha1(normalize_for_match(en_span).encode("utf-8")).hexdigest()[:10]


def candidate_key(chapter_id: str, ref_key: Optional[str], en_span: str) -> str:
    """``chapter|ref|hash``: stable across realigns, distinct per quotation."""
    return f"{chapter_id}|{ref_key or '-'}|{span_hash(en_span)}"


def parse_scan_draft(raw: str, *, chapter_id: str) -> list[dict[str, Any]]:
    data = parse_judge_json(raw, REQUIRED_FIELDS)
    echoed = str(data.get("chapter_id") or "").strip()
    if echoed and echoed != chapter_id:
        raise JudgeParseError(f"chapter mismatch: draft echoed {echoed!r}, expected {chapter_id!r}")
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        raise JudgeParseError(f"'candidates' must be a list, got {type(candidates).__name__}")
    return [c for c in candidates if isinstance(c, dict)]


def _es_idx_list(value: Any) -> list[int]:
    if isinstance(value, (int, str)):
        value = [value]
    out = []
    for item in value or []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def validate_candidate(
    raw: dict[str, Any],
    *,
    chapter_id: str,
    english_norm: str,
    es_map: dict[int, str],
) -> tuple[dict[str, Any], Optional[str]]:
    """``(row, None)`` for a usable candidate, ``(row, reason)`` otherwise.

    A wrong ``es_span`` is not fatal: the mapping wave finds the Spanish again.
    It is dropped and flagged, and the candidate stays.
    """
    kind = str(raw.get("kind") or "").strip().lower()
    confidence = str(raw.get("confidence") or "").strip().lower()
    ref_text = raw.get("ref_en")
    ref_text = str(ref_text).strip() if ref_text not in (None, "") else ""
    row: dict[str, Any] = {
        "chapter_id": chapter_id,
        "kind": kind,
        "en_span": str(raw.get("en_span") or "").strip(),
        "es_span": str(raw.get("es_span") or "").strip(),
        "es_idx": _es_idx_list(raw.get("es_idx")),
        "ref_en": ref_text,
        "adapted": bool(raw.get("adapted")),
        "why": str(raw.get("why") or "").strip(),
        "confidence": confidence if confidence in CONFIDENCE else "medium",
        "origin": "wave",
        "flags": [],
    }
    if not row["en_span"] or not kind:
        return row, UNUSABLE_FIELDS
    if kind not in KINDS:
        return row, UNUSABLE_KIND
    if normalize_for_match(row["en_span"]) not in english_norm:
        return row, UNUSABLE_EN
    good_idx = [i for i in row["es_idx"] if i in es_map]
    if row["es_idx"] and not good_idx:
        return row, UNUSABLE_ROW
    if len(good_idx) < len(row["es_idx"]):
        row["flags"].append("es_idx_partly_unknown")
    row["es_idx"] = good_idx
    if row["es_span"]:
        joined = normalize_for_match(" ".join(es_map[i] for i in good_idx))
        if normalize_for_match(row["es_span"]) not in joined:
            row["flags"].append("es_span_not_found")
            row["es_span_proposed"] = row["es_span"]
            row["es_span"] = ""
    ref = parse_ref(ref_text) if ref_text and kind != "not_scripture" else None
    row["ref"] = ref.english if ref else None
    row["ref_key"] = ref.key if ref else None
    row["verse_keys"] = ref.verse_keys() if ref else []
    if kind != "not_scripture" and ref is None:
        row["flags"].append("unresolved_ref")
    row["key"] = candidate_key(chapter_id, row["ref_key"], row["en_span"])
    return row, None


def _overlaps(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


def attach_written_refs(
    chapter_id: str,
    english: str,
    written: list[FoundRef],
    candidates: list[dict[str, Any]],
    rows: list[dict],
) -> list[dict[str, Any]]:
    """Cards for written references no candidate covers (the deterministic floor).

    A reference is covered by a candidate that names an overlapping verse and
    sits within :data:`REF_REACH` characters of it in the chapter's English. Each
    candidate covers at most one written occurrence, so a reference written
    twice needs two candidates.
    """
    norm_english = english
    positions: list[Optional[int]] = []
    for cand in candidates:
        pattern = _ws_pattern(cand["en_span"])
        match = pattern.search(norm_english) if pattern else None
        positions.append(match.start() if match else None)
    used: set[int] = set()
    added: list[dict[str, Any]] = []
    for found in written:
        keys = found.ref.verse_keys()
        best: Optional[int] = None
        best_dist = None
        for i, cand in enumerate(candidates):
            if i in used or not _overlaps(keys, cand.get("verse_keys") or []):
                continue
            pos = positions[i]
            dist = abs(pos - found.start) if pos is not None else REF_REACH
            if dist <= REF_REACH and (best_dist is None or dist < best_dist):
                best, best_dist = i, dist
        if best is not None:
            used.add(best)
            candidates[best].setdefault("written_ref", found.ref.text)
            continue
        added.append(_reference_card(chapter_id, english, found, rows))
    return added


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence (or line) of ``text`` that holds ``[start, end)``."""
    left = max(text.rfind("\n", 0, start), max(text.rfind(m, 0, start) for m in (". ", "? ", "! ")))
    left = left + 1 if left >= 0 else 0
    right_nl = text.find("\n", end)
    right = right_nl if right_nl >= 0 else len(text)
    return " ".join(text[left:right].split())


def _reference_card(chapter_id: str, english: str, found: FoundRef, rows: list[dict]) -> dict[str, Any]:
    ref = found.ref
    es_idx: list[int] = []
    es_span = ""
    for row in rows:
        if ref.text.split()[0] in (row.get("en") or "") and _ws_pattern(ref.text).search(row.get("en") or ""):
            es_idx = [row["es_idx"]]
            for es_found in find_es_refs(row.get("es") or ""):
                if es_found.ref.book == ref.book:
                    es_span = es_found.ref.text
                    break
            break
    card = {
        "chapter_id": chapter_id,
        "kind": "reference_only",
        "en_span": ref.text,
        "en_context": _sentence_around(english, found.start, found.end),
        "es_span": es_span,
        "es_idx": es_idx,
        "ref_en": ref.text,
        "ref": ref.english,
        "ref_key": ref.key,
        "verse_keys": ref.verse_keys(),
        "written_ref": ref.text,
        "adapted": False,
        "why": "written reference the wave did not attach to a quotation",
        "confidence": "high",
        "origin": "coded",
        "flags": [] if es_idx else ["no_aligned_row"],
    }
    card["key"] = candidate_key(chapter_id, ref.key, ref.text + f"@{found.start}")
    return card


_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some jobs failed or missing) | 'error' (nothing parsed)",
    "counts": "{jobs, usable, coded, unusable, failed, missing}",
    "by_kind": "{kind: usable candidates, coded reference cards included}",
    "written_refs": "{found, covered_by_wave, coded_cards}: the deterministic floor",
    "flags": "{flag: count}: es_span_not_found, unresolved_ref, no_aligned_row ...",
    "failed": "list of {id, problem}: re-run scan-fanout for these",
    "missing": "job ids with no draft",
    "unusable": "candidates refused: {chapter_id, en_span, reason}",
    "candidates_path": "candidates.json",
    "instructions": "next step",
}


def scan_commit(project_dir: Path) -> dict[str, Any]:
    """Parse drafts, validate every candidate, add the coded reference cards."""
    project_dir = Path(project_dir)
    manifest, err = _load_manifest(project_dir)
    if err:
        return {"status": "error", "error": err, "_schema": _COMMIT_SCHEMA}
    qdir = quotations_dir(project_dir).resolve()

    usable: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    parsed = 0
    written_total = covered = coded_total = 0

    for job in manifest["jobs"]:
        chapter_id = job.get("chapter_id") or job.get("id")
        draft = Path(job.get("draft_path") or "")
        try:
            if not draft.resolve().is_relative_to(qdir):
                failed.append({"id": chapter_id, "problem": f"draft_path escapes quotations/: {draft}"})
                continue
        except (OSError, ValueError) as exc:
            failed.append({"id": chapter_id, "problem": f"unresolvable draft_path: {exc}"})
            continue
        if not draft.exists():
            missing.append(chapter_id)
            continue
        try:
            raw_cands = parse_scan_draft(draft.read_text(encoding="utf-8"), chapter_id=chapter_id)
        except (OSError, UnicodeDecodeError, JudgeParseError) as exc:
            failed.append({"id": chapter_id, "problem": str(exc)[:500]})
            continue
        parsed += 1

        rows = load_rows(project_dir, chapter_id)
        es_map = {r["es_idx"]: r.get("es") or "" for r in rows if "es_idx" in r}
        english = chapter_english(project_dir, chapter_id)
        english_norm = normalize_for_match(english)

        chapter_usable: list[dict[str, Any]] = []
        seen: dict[str, list[set[int]]] = {}
        for raw in raw_cands:
            row, reason = validate_candidate(
                raw, chapter_id=chapter_id, english_norm=english_norm, es_map=es_map
            )
            if reason is None:
                # The same words quoted twice in a chapter (an epigraph and again
                # in the text) are two quotations: the nth occurrence gets "#n".
                # The same words on the same rows are one quotation reported twice.
                earlier = seen.setdefault(row["key"], [])
                rows_here = set(row["es_idx"])
                if any(rows_here & prev or not (rows_here or prev) for prev in earlier):
                    reason = UNUSABLE_DUPLICATE
                else:
                    if earlier:
                        row["key"] = f"{row['key']}#{len(earlier) + 1}"
                    earlier.append(rows_here)
            if reason:
                unusable.append({**row, "reason": reason})
                continue
            chapter_usable.append(row)

        written = find_refs(english)
        coded = attach_written_refs(chapter_id, english, written, chapter_usable, rows)
        written_total += len(written)
        covered += len(written) - len(coded)
        coded_total += len(coded)
        usable.extend(chapter_usable)
        usable.extend(coded)

    counts = {
        "jobs": len(manifest["jobs"]),
        "usable": len(usable),
        "coded": coded_total,
        "unusable": len(unusable),
        "failed": len(failed),
        "missing": len(missing),
    }
    if not parsed:
        return {
            "status": "error",
            "error": "no scan draft parsed; candidates.json left as it was",
            "counts": counts,
            "failed": failed,
            "missing": missing,
            "_schema": _COMMIT_SCHEMA,
        }

    by_kind: dict[str, int] = {}
    flags: dict[str, int] = {}
    for row in usable:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        for flag in row.get("flags") or []:
            flags[flag] = flags.get(flag, 0) + 1

    doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "prompt_version": manifest.get("prompt_version"),
        "worker_model": manifest.get("worker_model"),
        "chapters": manifest.get("chapters"),
        "candidates": usable,
        "unusable": unusable,
        "failed": failed,
        "missing": missing,
    }
    path = candidates_path(project_dir)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "partial" if failed or missing else "ok",
        "counts": counts,
        "by_kind": by_kind,
        "written_refs": {"found": written_total, "covered_by_wave": covered, "coded_cards": coded_total},
        "flags": flags,
        "failed": failed,
        "missing": missing,
        "unusable": [
            {"chapter_id": r["chapter_id"], "en_span": r["en_span"][:80], "reason": r["reason"]}
            for r in unusable
        ],
        "candidates_path": str(path),
        "instructions": (
            "Re-run `scan-fanout` for anything in failed/missing, then commit again. "
            "Otherwise run `resolve`."
        ),
        "_schema": _COMMIT_SCHEMA,
    }
