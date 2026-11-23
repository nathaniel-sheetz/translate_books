"""Hand edits from the harness, landed the way the web UI lands them.

Two verbs, so an agent that finds a slip mid-run can fix it without the web
server and without hand-editing ``translated_text``:

- :func:`sentence_edit` -- the primary verb. One edit is one alignment row
  (``es_idx``), as the reader's Save writes it, and the batch then goes through
  what the reader's *Apply corrections* does: the chunk is patched on the exact
  span, the row is archived in ``corrections_applied.jsonl``, the chapter is
  recombined and realigned, and reader notes are re-anchored.
- :func:`chunk_edit` -- the fallback, for what no single row can hold: a
  dropped heading, a paragraph split or join, an edit across two sentences. It
  mirrors the chunk editor: the same image-token and overlap guards, the chunk's
  pending corrections purged, the chapter realigned, the coded evaluators
  re-run.

Both take the CLI pipeline the other apply passes share (``run_judges apply``,
the quotation pass, the archaism pass): a ``.chunk_edits/`` snapshot first, then
:func:`~src.corrections_apply.apply_to_chunk` / save, the audit row, the stale
stamp, and the per-chapter recombine + realign. Nothing here imports
``web_ui.app``.

Provenance: every row carries ``source: "harness:chat"``. These are a model's
edits the user approved in chat, not reader Saves, so the KDP authorship facts
count them as model fixes and the ledger census and audit (which read rows with
no ``source``) leave them out. A finding an edit fixes gets the machine
``applied`` mark (:func:`web_ui.evaluations.mark_applied`), never ``resolved``:
the agent's judgement stays out of the precision corpus.

Every batch is all-or-nothing at planning time: if one edit cannot be placed,
nothing is written and the refusal says why.
"""

from __future__ import annotations

import contextlib
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SOURCE = "harness:chat"

#: Same pattern as the chunk editor's guard (``web_ui/app.py``).
_IMAGE_TOKEN_RE = re.compile(r"\[IMAGE:[^\]]+\]")
_CLIP = 160


def _clip(text: str, n: int = _CLIP) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


@contextlib.contextmanager
def _quiet_stdout():
    """The aligner and evaluators print progress; send it to stderr so stdout
    stays one JSON object and their warnings are still seen."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _langs(project_dir: Path) -> tuple[str, str]:
    from src.harness import state

    cfg = state.load_config(project_dir)
    return cfg.get("source_language_code") or "en", cfg.get("language_code") or "es"


def _reader_link(project_dir: Path, chapter_id: str, host: str, port: int) -> str:
    return f"http://{host}:{port}/read/{project_dir.name}/{chapter_id}"


def _alignment_rows(project_dir: Path, chapter_id: str) -> Optional[list[dict]]:
    import json

    path = project_dir / "alignments" / f"{chapter_id}.json"
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("alignments", [])
    except (OSError, ValueError):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _text_rows(rows: list[dict]) -> list[dict]:
    """Rows holding a Spanish sentence (image rows and empty rows are not editable)."""
    return [
        r for r in rows
        if r.get("type") != "image" and isinstance(r.get("es"), str) and r.get("es")
        and isinstance(r.get("es_idx"), int)
    ]


def _pending_rows(project_dir: Path) -> list[dict]:
    from src.corrections_apply import load_corrections

    return [r for r in load_corrections(project_dir) if isinstance(r, dict)]


def _pending_summary(rows: list[dict]) -> list[dict]:
    return [
        {
            "chapter_id": r.get("chapter_id"),
            "chunk_id": r.get("chunk_id"),
            "es_idx": r.get("es_idx"),
            "original_es": _clip(r.get("original_es") or ""),
            "corrected_es": _clip(r.get("corrected_es") or ""),
        }
        for r in rows
    ]


def _save_check_settings() -> tuple[bool, frozenset]:
    from src import save_check
    from src.app_config import get_save_check_config

    try:
        return save_check.read_config(get_save_check_config())
    except Exception:  # noqa: BLE001 - a broken config must not cost an edit
        return False, frozenset()


def _save_check_hits(project_dir: Path, en: str, before: str, after: str,
                     enabled: bool, disabled: frozenset) -> list[dict]:
    """The rule layer's hits on one write; ``[]`` when the check is off or fails."""
    if not enabled:
        return []
    from src import save_check

    try:
        return save_check.check_project_write(project_dir, en, before, after,
                                              disabled_rules=disabled)
    except Exception:  # noqa: BLE001 - a failed check must not cost an edit
        return []


def _log_save_check(project_dir: Path, chapter_id: str, es_idx, en: str, before: str,
                    after: str, hits: list[dict], disabled: frozenset) -> Optional[dict]:
    """Log a landed write's warning as the reader's Save does, under ``path: "harness"``."""
    from src import save_check
    from web_ui.evaluations import load_project_ignored_terms

    try:
        carried, carried_from = save_check.carried_hits(
            project_dir, chapter_id, before, after,
            ignored=load_project_ignored_terms(project_dir), disabled_rules=disabled,
        )
        found = {(h["rule"], h["text"]) for h in hits}
        carried = [h for h in carried if (h["rule"], h["text"]) not in found]
        hits = hits + carried
        if not hits:
            return None
        lineage = {"carried_from": carried_from} if carried else {}
        warning_id = save_check.append_warning(
            project_dir, chapter_id=chapter_id, es_idx=es_idx, path="harness",
            en=en, es_before=before, es_after=after, hits=hits, **lineage,
        )
        return {"id": warning_id, "hits": hits}
    except Exception:  # noqa: BLE001
        return None


def _resolve_finding(project_dir: Path, raw: Any) -> tuple[Optional[dict], Optional[str], bool]:
    """``(mark, problem, fatal)`` for an edit's ``finding``.

    ``mark`` is ``{chunk_id, eval_name, issue_index, issue}`` as
    :func:`web_ui.evaluations.mark_applied` takes it. A malformed value is
    fatal (the edit is refused). A well-formed one that names no stored finding
    is not: the edit lands and the problem is reported, since a finding the
    judge's own filter set aside is not in ``issues`` and the reader never
    shows it.
    """
    from web_ui.evaluations import resolve_issue

    if raw is None:
        return None, None, False
    if not isinstance(raw, dict):
        return None, "finding must be an object {eval_name, chunk_id, issue_index}", True
    eval_name, chunk_id, idx = raw.get("eval_name"), raw.get("chunk_id"), raw.get("issue_index")
    if not (isinstance(eval_name, str) and eval_name and isinstance(chunk_id, str) and chunk_id
            and isinstance(idx, int) and not isinstance(idx, bool)):
        return None, "finding needs eval_name (str), chunk_id (str) and issue_index (int)", True
    issue = resolve_issue(project_dir, chunk_id, eval_name, idx)
    if issue is None:
        return None, (f"no {eval_name} finding #{idx} in {chunk_id}'s issues, so nothing was "
                      "marked (a filtered finding is not shown by the reader and needs no "
                      "mark)"), False
    return ({"chunk_id": chunk_id, "eval_name": eval_name, "issue_index": idx, "issue": issue},
            None, False)


def _from_finding(mark: Optional[dict]) -> Optional[dict]:
    from web_ui.evaluations import clean_from_finding, issue_key

    if not mark:
        return None
    issue = mark["issue"]
    return clean_from_finding({
        "eval_name": mark["eval_name"],
        "chunk_id": mark["chunk_id"],
        "issue_key": issue_key(mark["eval_name"], issue),
        "category": issue.get("category") if isinstance(issue.get("category"), str) else None,
        "suggestion": issue.get("suggestion") if isinstance(issue.get("suggestion"), str) else None,
    })


def _realign(project_dir: Path, chapters: list[str]) -> tuple[list[str], list[dict]]:
    from src.corrections_apply import realign_chapter, recombine_chapter

    src_lang, tgt_lang = _langs(project_dir)
    done, errors = [], []
    with _quiet_stdout():
        for chapter_id in chapters:
            try:
                recombine_chapter(project_dir, chapter_id)
                realign_chapter(project_dir, chapter_id, src_lang, tgt_lang)
                done.append(chapter_id)
            except Exception as exc:  # noqa: BLE001 - report; the edit itself landed
                errors.append({"chapter_id": chapter_id, "error": str(exc)})
    return done, errors


# ── sentence-edit ────────────────────────────────────────────────────────────


def _plan_sentence_edits(project_dir: Path, edits: list[Any]) -> tuple[list[dict], list[dict]]:
    """Resolve every edit to one alignment row; ``(targets, refused)``.

    A target is ``{chapter_id, row, after, edits: [n], finding, reason}``; two
    edits of the same row fold into one target, applied in file order.
    """
    from src.quotations.apply import alignment_is_stale

    targets: dict[tuple[str, int], dict] = {}
    refused: list[dict] = []
    rows_cache: dict[str, Optional[list[dict]]] = {}
    stale_cache: dict[str, bool] = {}

    for n, edit in enumerate(edits):
        def refuse(error: str, **extra) -> None:
            refused.append({"n": n, "edit": edit, "error": error, **extra})

        if not isinstance(edit, dict):
            refuse("each edit must be an object")
            continue
        chunk_scope = edit.get("chunk_id")
        chapter_id = edit.get("chapter") or edit.get("chapter_id")
        if not chapter_id and isinstance(chunk_scope, str) and "_chunk_" in chunk_scope:
            chapter_id = chunk_scope.rsplit("_chunk_", 1)[0]
        if not isinstance(chapter_id, str) or not chapter_id:
            refuse("missing chapter (e.g. \"chapter_02\")")
            continue
        if chapter_id not in rows_cache:
            rows_cache[chapter_id] = _alignment_rows(project_dir, chapter_id)
        rows = rows_cache[chapter_id]
        if rows is None:
            refuse(f"no alignment for {chapter_id}; run `harness.py align --chapters ...` first")
            continue
        if chapter_id not in stale_cache:
            stale_cache[chapter_id] = alignment_is_stale(project_dir, chapter_id)
        if stale_cache[chapter_id]:
            # A chunk written after the last realign leaves offsets that no
            # longer slice back, and the fallback search can land on a twin.
            refuse(f"the alignment for {chapter_id} is older than its chunks; run "
                   f"`harness.py align --chapters {chapter_id}` first")
            continue
        rows = _text_rows(rows)
        if chunk_scope:
            rows = [r for r in rows if r.get("chunk_id") == chunk_scope]

        has_find = "find" in edit or "replace" in edit
        has_new = "new" in edit
        if has_find == has_new:
            refuse("give either {find, replace} (optionally with es_idx) or {es_idx, new}")
            continue
        es_idx = edit.get("es_idx")
        if es_idx is not None:
            if not isinstance(es_idx, int) or isinstance(es_idx, bool):
                refuse("es_idx must be an int")
                continue
            rows = [r for r in rows if r["es_idx"] == es_idx]
            if not rows:
                refuse(f"no sentence es_idx {es_idx} in {chapter_id}"
                       + (f" / {chunk_scope}" if chunk_scope else ""))
                continue

        if has_new:
            new = edit.get("new")
            if es_idx is None or not isinstance(new, str):
                refuse("{new} needs es_idx (int) and new (string)")
                continue
            row = rows[0]
            key = (chapter_id, es_idx)
            current = targets[key]["after"] if key in targets else row["es"]
            after = new.strip()
            op = None
        else:
            find, replace = edit.get("find"), edit.get("replace")
            if not isinstance(find, str) or not find or not isinstance(replace, str):
                refuse("find must be a non-empty string and replace a string")
                continue
            # A row already edited earlier in this batch is searched as it now reads.
            def text_of(r: dict) -> str:
                return targets.get((chapter_id, r["es_idx"]), {}).get("after", r["es"])

            matches = [r for r in rows if find in text_of(r)]
            if not matches:
                joined = " ".join(text_of(r) for r in rows)
                # An es_idx scope names one sentence, so a miss there is a miss,
                # not a find that runs across sentences elsewhere in the chapter.
                if es_idx is None and (find in joined or any(
                    find in (c or "") for c in _chunk_texts(project_dir, chapter_id, chunk_scope)
                )):
                    refuse("find spans more than one sentence; split it into one edit per "
                           "sentence, or use `chunk-edit` for an edit across sentences")
                else:
                    refuse(f"find not in any sentence of {chapter_id}"
                           + (f" / {chunk_scope}" if chunk_scope else "")
                           + (f" / es_idx {es_idx}" if es_idx is not None else ""))
                continue
            if len(matches) > 1:
                refuse("find matches more than one sentence; quote more of it, or add "
                       "chunk_id, or name the sentence by es_idx",
                       candidates=[{"es_idx": r["es_idx"], "chunk_id": r.get("chunk_id"),
                                    "es": _clip(text_of(r))} for r in matches[:8]])
                continue
            row = matches[0]
            key = (chapter_id, row["es_idx"])
            current = text_of(row)
            if current.count(find) > 1:
                refuse("find occurs more than once in its sentence; quote more of it",
                       es_idx=row["es_idx"], es=_clip(current))
                continue
            after = current.replace(find, replace, 1)
            op = (find, replace)

        expect = edit.get("expect")
        if expect is not None:
            # Guards an es_idx read before a realign renumbered the rows.
            if not isinstance(expect, str) or not expect:
                refuse("expect must be a non-empty string")
                continue
            if expect not in current:
                refuse(f"expect not in sentence es_idx {row['es_idx']}; the numbering may have "
                       "moved after a realign, so re-read the alignment",
                       es_idx=row["es_idx"], es=_clip(current))
                continue

        if not after.strip():
            refuse("the edit would empty the sentence; use chunk-edit to delete a sentence")
            continue
        if after == current:
            refuse("the edit changes nothing", es_idx=row["es_idx"])
            continue
        mark, why, fatal = _resolve_finding(project_dir, edit.get("finding"))
        if fatal:
            refuse(why)
            continue
        target = targets.setdefault(key, {
            "chapter_id": chapter_id, "row": row, "after": current,
            "edits": [], "findings": [], "finding_problems": [], "reasons": [], "ops": [],
        })
        target["after"] = after
        target["edits"].append(n)
        target["ops"].append(op)
        if mark:
            target["findings"].append(mark)
        elif why:
            target["finding_problems"].append(why)
        if isinstance(edit.get("reason"), str) and edit["reason"].strip():
            target["reasons"].append(edit["reason"].strip()[:500])

    return list(targets.values()), refused


def _replay_in_chunk(matched: str, before: str, ops: list) -> Optional[tuple[str, str]]:
    """``(matched, edited)`` when a merged row's finds can be replayed on the chunk.

    The whitespace-tolerant span of a merged dialogue row holds the chunk's own
    line breaks, which the space-joined row does not. Rebuilding the sentence
    from the row (``restore_whitespace``) gives up once an edit changes the
    number of whitespace runs, and the line break becomes a space. When every
    edit on the row is a find/replace whose find occurs exactly once in the
    chunk's slice, replaying them there keeps every break. Returns None when
    the slice already equals the row, or when any edit cannot be replayed (an
    ``{es_idx, new}`` rewrite, or a find that crosses the line break).
    """
    if matched == before or not ops or any(op is None for op in ops):
        return None
    edited = matched
    for find, replace in ops:
        if edited.count(find) != 1:
            return None
        edited = edited.replace(find, replace, 1)
    return matched, edited


def _chunk_texts(project_dir: Path, chapter_id: str, chunk_id: Optional[str]) -> list[str]:
    from src.utils.file_io import load_chunk

    paths = ([project_dir / "chunks" / f"{chunk_id}.json"] if chunk_id
             else sorted((project_dir / "chunks").glob(f"{chapter_id}_chunk_*.json")))
    out = []
    for p in paths:
        try:
            out.append(load_chunk(p).translated_text or "")
        except Exception:  # noqa: BLE001
            continue
    return out


def sentence_edit(
    project_dir: Path,
    edits: list[Any],
    *,
    dry_run: bool = False,
    include_pending: bool = False,
    reader_host: str = "localhost",
    reader_port: int = 5000,
) -> dict[str, Any]:
    """Edit sentences the way the reader's Save + Apply corrections does."""
    from src.corrections_apply import (
        _resolve_correction_span,
        apply_to_chunk,
        archive_applied_records,
        drain_corrections,
        safe_chunk_id,
    )
    from src.quotations.apply import _write_chunk_snapshot
    from src.utils.file_io import load_chunk, save_chunk
    from web_ui.evaluations import mark_applied, mark_evaluation_stale

    project_dir = Path(project_dir)
    if not isinstance(edits, list) or not edits:
        return {"status": "error", "error": "no edits given", "dry_run": dry_run}

    targets, refused = _plan_sentence_edits(project_dir, edits)
    chapters = sorted({t["chapter_id"] for t in targets} | {
        r["edit"].get("chapter") for r in refused
        if isinstance(r.get("edit"), dict) and isinstance(r["edit"].get("chapter"), str)
    })

    # Queued reader Saves in a chapter about to be realigned. The Save patched
    # the alignment in place and left the chunk alone, so a realign would hide
    # them from the reader (the hazard the web UI's Realign route guards).
    queue = _pending_rows(project_dir)
    pending = [r for r in queue if r.get("chapter_id") in chapters]
    if pending and not include_pending:
        return {
            "status": "error",
            "error": "the reader has unapplied Saves in these chapters; ask the user to press "
                     "Apply corrections in the reader, or re-run with --include-pending to "
                     "apply them in this batch (as that button would)",
            "dry_run": dry_run,
            "pending": _pending_summary(pending),
            "refused": refused,
            "write": False,
        }

    if refused:
        return {
            "status": "error",
            "error": f"{len(refused)} of {len(edits)} edit(s) could not be placed; nothing "
                     "was written. Fix them and re-run the whole batch.",
            "dry_run": dry_run,
            "refused": refused,
            "write": False,
        }

    enabled, disabled = _save_check_settings()
    stamp = datetime.now().isoformat()
    ts = time.strftime("%Y%m%dT%H%M%S")

    # Group by chunk. Pending reader Saves go first, then this batch's edits are
    # placed on the text those Saves leave, exactly as Apply would order them.
    by_chunk: dict[str, dict] = defaultdict(lambda: {"targets": [], "pending": []})
    for t in targets:
        by_chunk[t["row"].get("chunk_id") or ""]["targets"].append(t)
    for r in pending:
        by_chunk[r.get("chunk_id") or ""]["pending"].append(r)

    planned: list[dict] = []
    problems: list[dict] = []
    for chunk_id, group in sorted(by_chunk.items()):
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not safe_chunk_id(chunk_id) or not chunk_path.exists():
            for t in group["targets"]:
                problems.append({"es_idx": t["row"]["es_idx"], "chapter_id": t["chapter_id"],
                                 "error": f"the sentence's chunk {chunk_id!r} is missing"})
            continue
        chunk = load_chunk(chunk_path)
        after_pending, n_pending, pending_idx = apply_to_chunk(chunk, group["pending"])
        if n_pending == 0:
            after_pending = chunk
        text = after_pending.translated_text or ""
        records = []
        for t in group["targets"]:
            row = t["row"]
            before = row["es"]
            span = _resolve_correction_span(
                text, before, row.get("chunk_offset_start"), row.get("chunk_offset_end"),
            )
            if span is None:
                problems.append({
                    "es_idx": row["es_idx"], "chapter_id": t["chapter_id"],
                    "error": "the sentence's text is not in its chunk (the alignment is "
                             "older than the chunk); run `harness.py align` and re-run",
                    "es": _clip(before),
                })
                continue
            record = {
                "project_id": project_dir.name,
                "chapter_id": t["chapter_id"],
                "es_idx": row["es_idx"],
                "original_es": before,
                "corrected_es": t["after"],
                "en_reference": row.get("en") or "",
                "timestamp": stamp,
                "source": SOURCE,
                "chunk_id": chunk_id,
            }
            # The archived row carries offsets only when they slice back to
            # original_es, as /api/correction stores them. The apply call always
            # gets the located span as its hint, so the whitespace-tolerant tier
            # lands on this occurrence and not an earlier twin.
            if text[span[0]:span[1]] == before:
                record["chunk_offset_start"], record["chunk_offset_end"] = span
            from_finding = _from_finding(t["findings"][0] if t["findings"] else None)
            if from_finding:
                record["from_finding"] = from_finding
            if t["reasons"]:
                record["reason"] = " / ".join(t["reasons"])
            hits = _save_check_hits(project_dir, record["en_reference"], before, t["after"],
                                    enabled, disabled)
            hint = {**record, "chunk_offset_start": span[0], "chunk_offset_end": span[1]}
            in_chunk = _replay_in_chunk(text[span[0]:span[1]], before, t["ops"])
            if in_chunk is not None:
                # Write the chunk's own slice, line breaks and all, so the exact
                # tier lands and nothing is rebuilt from the space-joined row.
                hint["original_es"], hint["corrected_es"] = in_chunk
            records.append((t, record, hits, hint))
        planned.append({"chunk_id": chunk_id, "chunk_path": chunk_path, "chunk": chunk,
                        "after_pending": after_pending, "pending": group["pending"],
                        "pending_idx": pending_idx, "records": records})

    if problems:
        return {"status": "error", "dry_run": dry_run, "write": False,
                "error": "some sentences could not be located in their chunk; nothing was written",
                "refused": problems}

    def describe(t: dict, record: dict, hits: list[dict]) -> dict:
        out = {
            "chapter_id": record["chapter_id"], "chunk_id": record["chunk_id"],
            "es_idx": record["es_idx"], "en": record["en_reference"],
            "before": record["original_es"], "after": record["corrected_es"],
            "edits": t["edits"],
        }
        if hits:
            out["save_check"] = hits
        if t["findings"]:
            out["findings"] = [f"{m['eval_name']}:{m['chunk_id']}#{m['issue_index']}"
                               for m in t["findings"]]
        if t["finding_problems"]:
            out["finding_problems"] = t["finding_problems"]
        return out

    pending_planned = [r for p in planned for i, r in enumerate(p["pending"])
                       if i in set(p["pending_idx"])]
    if dry_run:
        return {
            "status": "ok", "dry_run": True, "write": False,
            "planned": [describe(t, rec, h) for p in planned for t, rec, h, _x in p["records"]],
            "pending_included": _pending_summary(pending_planned),
            "pending_unplaced": _pending_summary(
                [r for r in pending if not any(r is x for x in pending_planned)]),
            "chapters": chapters,
            "instructions": "Show the user each before/after pair (with its English) and "
                            "re-run without --dry-run once they approve.",
        }

    applied, failed, backups, stale_marked, checks = [], [], [], [], []
    findings_marked = 0
    applied_pending: list[dict] = []
    edited_chapters: list[str] = []
    for p in planned:
        chunk_id, chunk_path = p["chunk_id"], p["chunk_path"]
        recs = [hint for _t, _rec, _h, hint in p["records"]]
        updated, n, idx = apply_to_chunk(p["after_pending"], recs) if recs else (p["after_pending"], 0, [])
        landed = [p["records"][i] for i in sorted(idx)]
        for i, (t, rec, _h, _x) in enumerate(p["records"]):
            if i not in idx:
                failed.append({"es_idx": rec["es_idx"], "chapter_id": rec["chapter_id"],
                               "error": "did not land"})
        pend_landed = [p["pending"][i] for i in sorted(p["pending_idx"])]
        if not landed and not pend_landed:
            continue
        backups.append(str(_write_chunk_snapshot(project_dir, p["chunk"].chapter_id, chunk_id,
                                                 chunk_path, ts)))
        save_chunk(updated if landed else p["after_pending"], chunk_path)
        if pend_landed:
            archive_applied_records(project_dir, [{**r, "status": "applied"} for r in pend_landed])
            applied_pending.extend(pend_landed)
        if landed:
            archive_applied_records(project_dir, [{**rec, "status": "applied"}
                                                  for _t, rec, _h, _x in landed])
        if mark_evaluation_stale(project_dir, chunk_id,
                                 "translated_text edited by harness sentence-edit") is not None:
            stale_marked.append(chunk_id)
        marks = [m for t, _rec, _h, _x in landed for m in t["findings"]]
        if marks:
            findings_marked += mark_applied(project_dir, marks,
                                            note=f"harness sentence-edit {stamp}")
        for t, rec, hits, _x in landed:
            check = _log_save_check(project_dir, rec["chapter_id"], rec["es_idx"],
                                    rec["en_reference"], rec["original_es"],
                                    rec["corrected_es"], hits, disabled) if enabled else None
            if check:
                checks.append({"es_idx": rec["es_idx"], "chapter_id": rec["chapter_id"], **check})
            applied.append(describe(t, rec, hits))
        chapter_id = p["chunk"].chapter_id
        if chapter_id not in edited_chapters:
            edited_chapters.append(chapter_id)

    if applied_pending:
        remaining = [r for r in queue if not any(r is x for x in applied_pending)]
        drain_corrections(project_dir, remaining)

    realigned, realign_errors = _realign(project_dir, edited_chapters)
    status = "ok" if not failed and not realign_errors else "partial"
    if not applied and not applied_pending:
        status = "error"
    instructions = "Done. The reader shows the new text after a reload."
    if realign_errors:
        instructions = ("The edits are in the chunks but the realign failed; run "
                        "`harness.py align --chapters <set>` for chapters_pending_realign.")
    if checks:
        instructions += (" The save-time check flagged some writes (save_check_warnings): "
                         "read each one and fix the slip, or tell the user why it stands.")
    return {
        "status": status,
        "dry_run": False,
        "write": True,
        "applied": applied,
        "failed": failed,
        "pending_applied": _pending_summary(applied_pending),
        "pending_unplaced": _pending_summary(
            [r for r in pending if not any(r is x for x in applied_pending)]),
        "save_check_warnings": checks,
        "backups": backups,
        "stale_marked": stale_marked,
        "findings_marked": findings_marked,
        "chapters_realigned": realigned,
        "chapters_pending_realign": [e["chapter_id"] for e in realign_errors],
        "realign_errors": realign_errors,
        "reader_links": [_reader_link(project_dir, c, reader_host, reader_port)
                         for c in edited_chapters],
        "instructions": instructions,
    }


# ── chunk-edit ───────────────────────────────────────────────────────────────


def chunk_edit(
    project_dir: Path,
    chunk_id: str,
    spans: list[Any],
    *,
    dry_run: bool = False,
    purge_pending: bool = False,
    finding: Any = None,
    reason: Optional[str] = None,
    reader_host: str = "localhost",
    reader_port: int = 5000,
) -> dict[str, Any]:
    """Edit spans of one chunk's text the way the chunk editor saves it."""
    from src.corrections_apply import (
        archive_applied_records,
        purge_chunk_corrections,
        safe_chunk_id,
    )
    from src.quotations.apply import _write_chunk_snapshot
    from src.utils.file_io import load_chunk, save_chunk
    from web_ui.evaluations import (
        evaluate_and_persist_chunk,
        mark_applied,
        mark_evaluation_stale,
    )

    project_dir = Path(project_dir)

    def error(msg: str, **extra) -> dict:
        return {"status": "error", "error": msg, "dry_run": dry_run, "write": False,
                "chunk_id": chunk_id, **extra}

    if not safe_chunk_id(chunk_id) or "_chunk_" not in chunk_id:
        return error("chunk_id must look like chapter_02_chunk_005")
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return error(f"no chunk {chunk_id}")
    if not isinstance(spans, list) or not spans:
        return error("no spans given")
    chunk = load_chunk(chunk_path)
    chapter_id = chunk.chapter_id or chunk_id.rsplit("_chunk_", 1)[0]
    old_text = chunk.translated_text or ""
    if not old_text:
        return error("the chunk has no translation yet; translate it first")

    queue = _pending_rows(project_dir)
    on_chunk = [r for r in queue if r.get("chunk_id") == chunk_id]
    on_chapter = [r for r in queue
                  if r.get("chapter_id") == chapter_id and r.get("chunk_id") != chunk_id]
    if on_chapter:
        return error("the reader has unapplied Saves elsewhere in this chapter, which the "
                     "realign would hide; ask the user to press Apply corrections in the "
                     "reader first, or add --include-pending to a sentence-edit you are "
                     "making in this chapter anyway",
                     pending=_pending_summary(on_chapter))
    if on_chunk and not purge_pending:
        return error("the reader has unapplied Saves on this chunk, which a chunk edit "
                     "discards (as the chunk editor does); ask the user, then re-run with "
                     "--purge-pending, or apply them first", pending=_pending_summary(on_chunk))

    text = old_text
    rows: list[dict] = []
    stamp = datetime.now().isoformat()
    for n, span in enumerate(spans):
        if not isinstance(span, dict) or not isinstance(span.get("find"), str) \
                or not span["find"] or not isinstance(span.get("replace"), str):
            return error(f"span {n}: needs find (non-empty string) and replace (string)")
        find, replace = span["find"], span["replace"]
        count = text.count(find)
        if count != 1:
            return error(f"span {n}: find occurs {count} times in the chunk (as edited by the "
                         "spans before it); it must occur exactly once",
                         find=_clip(find))
        start = text.index(find)
        text = text[:start] + replace + text[start + len(find):]
        rows.append({
            "project_id": project_dir.name, "chapter_id": chapter_id, "chunk_id": chunk_id,
            "es_idx": None, "original_es": find, "corrected_es": replace,
            "chunk_offset_start": start, "chunk_offset_end": start + len(find),
            "en_reference": "", "timestamp": stamp, "source": SOURCE, "kind": "chunk",
        })

    # The chunk editor's guards (web_ui/app.py save_chunk_edit).
    if not text.strip():
        return error("the edit would empty the chunk")
    if text == old_text:
        return error("the spans change nothing")
    old_tokens, new_tokens = _IMAGE_TOKEN_RE.findall(old_text), _IMAGE_TOKEN_RE.findall(text)
    if old_tokens != new_tokens:
        return error("[IMAGE:...] placeholders must not be added, removed, or reordered",
                     expected=old_tokens, got=new_tokens)
    ov_start, ov_end = chunk.metadata.overlap_start, chunk.metadata.overlap_end
    if ov_start > 0 and text[:ov_start] != old_text[:ov_start]:
        return error(f"the first {ov_start} characters overlap the previous chunk and cannot "
                     f"be edited here; edit them in the previous chunk")
    if ov_end > 0 and text[-ov_end:] != old_text[-ov_end:]:
        return error(f"the last {ov_end} characters overlap the next chunk and cannot be "
                     f"edited here; edit them in the next chunk")

    mark, why, fatal = _resolve_finding(project_dir, finding)
    if fatal:
        return error(why)
    if mark:
        for row in rows:
            row["from_finding"] = _from_finding(mark)
    if isinstance(reason, str) and reason.strip():
        for row in rows:
            row["reason"] = reason.strip()[:500]

    enabled, disabled = _save_check_settings()
    hits = []
    for row in rows:
        h = _save_check_hits(project_dir, "", row["original_es"], row["corrected_es"],
                             enabled, disabled)
        if h:
            hits.append({"find": _clip(row["original_es"]), "hits": h})

    summary = [{"before": r["original_es"], "after": r["corrected_es"],
                "chunk_offset_start": r["chunk_offset_start"]} for r in rows]
    if dry_run:
        return {
            "status": "ok", "dry_run": True, "write": False, "chunk_id": chunk_id,
            "chapter_id": chapter_id, "spans": summary, "save_check": hits,
            "pending_to_purge": _pending_summary(on_chunk),
            "finding_problem": why,
            "instructions": "Show the user each span's before/after and re-run without "
                            "--dry-run once they approve.",
        }

    ts = time.strftime("%Y%m%dT%H%M%S")
    backup = _write_chunk_snapshot(project_dir, chapter_id, chunk_id, chunk_path, ts)
    chunk.translated_text = text
    save_chunk(chunk, chunk_path)
    purged = purge_chunk_corrections(project_dir, chunk_id) if purge_pending else 0
    archive_applied_records(project_dir, [{**r, "status": "applied"} for r in rows])
    findings_marked = mark_applied(project_dir, [mark], note=f"harness chunk-edit {stamp}") \
        if mark else 0
    # Stamp first: re-running the coded evaluators clears the flag only for
    # what they cover, so judge findings on the old text stay marked stale.
    mark_evaluation_stale(project_dir, chunk_id, "translated_text edited by harness chunk-edit")
    realigned, realign_errors = _realign(project_dir, [chapter_id])
    evaluation_error = None
    try:
        with _quiet_stdout():
            evaluate_and_persist_chunk(project_dir, load_chunk(chunk_path))
    except Exception as exc:  # noqa: BLE001 - the edit landed; report the evaluator
        evaluation_error = str(exc)

    instructions = "Done. The reader shows the new text after a reload."
    if realign_errors:
        instructions = ("The edit is in the chunk but the realign failed; run "
                        f"`harness.py align --chapters {chapter_id.rsplit('_', 1)[-1]}`.")
    return {
        "status": "ok" if not realign_errors else "partial",
        "dry_run": False, "write": True, "chunk_id": chunk_id, "chapter_id": chapter_id,
        "spans": summary, "save_check": hits,
        "backup": str(backup), "corrections_purged": purged,
        "findings_marked": findings_marked, "finding_problem": why,
        "chapters_realigned": realigned, "realign_errors": realign_errors,
        "evaluation_error": evaluation_error,
        "reader_link": _reader_link(project_dir, chapter_id, reader_host, reader_port),
        "instructions": instructions,
    }
