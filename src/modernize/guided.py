"""The model-applied pass for a modernize book's guided archaism entries.

Literal entries are swapped by code at commit (:func:`archaisms.apply_literal`).
The rest need a reader: a replacement to fit into its sentence (agreement,
inflection), or a ``scope: only`` entry whose sense must be checked occurrence
by occurrence (*barba* is the Nutcracker's jaw in some sentences and a beard
in others). This pass runs after chunks are committed, over their text:

1. :func:`prepare` finds every pending occurrence (:func:`archaisms.guided_hits`)
   in the committed chunks and writes one job per chunk into
   ``.harness/archaisms/``: a shared preamble (``prompts/archaism_guided.txt``)
   and a body with each occurrence marked ⟦ ⟧ in its paragraph.
2. :func:`fanout` runs the jobs as one headless wave (:func:`src.decisions.wave.run_wave`).
3. :func:`apply` reads each answer back, checks it (:func:`check_answer`), and
   applies the replacements through the judge-apply path: snapshot to
   ``.chunk_edits/``, :func:`~src.corrections_apply.apply_to_chunk` at the
   exact offset, save, mark the evaluation stale, then recombine and realign
   each chapter it touched.

Every decision, ``replace`` or ``keep``, goes to ``archaism_fixes.jsonl`` with
a ``key`` (chunk, entry, and the text around the occurrence), so a re-run asks
only about occurrences not yet decided. An answer that fails its check is
reported and not recorded, so the next ``prepare`` asks again.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.modernize import archaisms

#: Wave type: ``state.COMMAND_EFFORT_DEFAULTS``, ``profile.USAGE_LOG_RELPATH``,
#: and the per-book ``headless_effort_archaisms_guided``.
COMMAND = "archaisms_guided"
PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "archaism_guided.txt"
OPEN, CLOSE = "⟦", "⟧"
#: Characters on each side of an occurrence that identify it in the ledger.
KEY_WINDOW = 40
#: An answer's ``before`` may not run longer than this many words.
MAX_SPAN_WORDS = 40
#: Words an answer may change beyond the entry's own old and new forms.
SLACK_WORDS = 4

_WORD_RE = re.compile(r"[^\W_]+")


def pass_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / "archaisms"


def occurrence_key(chunk_id: str, entry_id: str, text: str, start: int, end: int) -> str:
    window = text[max(0, start - KEY_WINDOW): end + KEY_WINDOW]
    window = " ".join(window.split())
    return hashlib.sha1(f"{chunk_id}|{entry_id}|{window}".encode("utf-8")).hexdigest()[:16]


def decided_keys(project_dir: Path) -> set[str]:
    path = Path(project_dir) / archaisms.LOG_NAME
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("key"):
            keys.add(row["key"])
    return keys


def _committed_chunks(project_dir: Path, chapters: Optional[set[int]] = None):
    from src.utils.file_io import load_chunk

    for path in sorted((Path(project_dir) / "chunks").glob("chapter_*_chunk_*.json")):
        num = int(re.search(r"chapter_(\d+)", path.name).group(1))
        if chapters and num not in chapters:
            continue
        chunk = load_chunk(path)
        if chunk.translated_text:
            yield chunk


# ── collect and prepare ──────────────────────────────────────────────────────

def collect(project_dir: Path, *, chapters: Optional[set[int]] = None) -> list[dict[str, Any]]:
    """Every occurrence of a guided entry in the committed chunks not yet decided."""
    project_dir = Path(project_dir)
    entries = {e.id: e for e in archaisms.load_list(project_dir)}
    done = decided_keys(project_dir)
    items = []
    for chunk in _committed_chunks(project_dir, chapters):
        text = chunk.translated_text
        for hit in archaisms.guided_hits(text, entries.values()):
            start, end = hit["offset"], hit["offset"] + len(hit["old"])
            key = occurrence_key(chunk.id, hit["id"], text, start, end)
            if key in done:
                continue
            items.append({
                "key": key, "chunk_id": chunk.id, "chapter_id": chunk.chapter_id,
                "id": hit["id"], "old": hit["old"], "offset": start,
                "paragraph": hit["paragraph"], "paragraph_start": hit["paragraph_start"],
            })
    return items


def render_item(n: int, item: dict[str, Any], entry: archaisms.Entry, contexts: list[str]) -> str:
    para, rel = item["paragraph"], item["offset"] - item["paragraph_start"]
    end = rel + len(item["old"])
    marked = para[:rel] + OPEN + para[rel:end] + CLOSE + para[end:]
    lines = [f"ITEM {n}", f"Entry {entry.id}: «{entry.old}» → «{entry.replacement}»"]
    if entry.scope == "only" and entry.only_when:
        lines.append(f"Replace only when: {entry.only_when}")
    if entry.why:
        lines.append(f"Why: {entry.why}")
    if entry.scope == "only" and contexts:
        lines.append("The sentences the note numbers (original spelling, before modernizing):")
        lines.extend(f"  [{i}] {c}" for i, c in enumerate(contexts, 1))
    lines += ["Paragraph:", marked]
    return "\n".join(lines)


def _raw_entries(project_dir: Path) -> dict[str, dict]:
    path = Path(project_dir) / archaisms.LIST_NAME
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = doc.get("entries", []) if isinstance(doc, dict) else doc
    return {str(r.get("id")): r for r in rows}


def prepare(project_dir: Path, *, chapters: Optional[set[int]] = None,
            worker_model: Optional[str] = None, cli: Optional[str] = None) -> dict[str, Any]:
    """Write one job per chunk with pending occurrences, and the manifest (no spend)."""
    from src.harness.profile import resolve_profile, usage_log_for

    project_dir = Path(project_dir)
    root = pass_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    # A fresh prepare owns the directory: an old draft must never be read
    # against new items.
    for old in list(root.glob("*.body.txt")) + list(root.glob("*.draft.json")):
        old.unlink()
    entries = {e.id: e for e in archaisms.load_list(project_dir)}
    raw = _raw_entries(project_dir)
    items = collect(project_dir, chapters=chapters)
    from src.harness import state as hstate
    cfg = hstate.load_config(project_dir)
    # The book's pinned worker (the one its chunks were modernized with) unless
    # the run names another.
    prof = resolve_profile(
        project_dir, command=COMMAND, cli=cli,
        worker_model=worker_model or cfg.get("worker_model") or None,
        worker_model_source="cli" if worker_model else "config",
        usage_log=usage_log_for(project_dir, COMMAND),
    )
    preamble_path = root / "preamble.txt"
    preamble_path.write_text(PROMPT.read_text(encoding="utf-8"), encoding="utf-8")
    title = cfg.get("title") or project_dir.name
    by_chunk: dict[str, list[dict]] = {}
    for item in items:
        by_chunk.setdefault(item["chunk_id"], []).append(item)
    jobs = []
    for chunk_id, batch in by_chunk.items():
        for n, item in enumerate(batch, 1):
            item["n"] = n
        body = [f"Book: «{title}»", ""]
        for item in batch:
            entry = entries[item["id"]]
            body += [render_item(item["n"], item, entry, raw.get(entry.id, {}).get("contexts") or []), ""]
        body_path, draft_path = root / f"{chunk_id}.body.txt", root / f"{chunk_id}.draft.json"
        body_path.write_text("\n".join(body), encoding="utf-8")
        jobs.append({"id": chunk_id, "keys": [i["key"] for i in batch],
                     "body_path": str(body_path), "draft_path": str(draft_path)})
    (root / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {
        "project": project_dir.name, "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "preamble_path": str(preamble_path), "worker_model": prof.worker_model,
        "cli": prof.cli, "effort": prof.effort, "jobs": jobs,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    for item in items:
        counts[item["id"]] = counts.get(item["id"], 0) + 1
    return {
        "status": "ok", "jobs": len(jobs), "occurrences": len(items),
        "by_entry": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "manifest_path": str(root / "manifest.json"), "effective": prof.to_payload(),
        "instructions": ("Nothing pending." if not jobs else
                         f"Run `archaisms fanout` ({len(jobs)} headless jobs), then `archaisms apply`."),
    }


def fanout(project_dir: Path, **kwargs: Any) -> dict[str, Any]:
    from src.decisions.wave import fanout_error, load_manifest, run_wave

    root = pass_dir(project_dir)
    manifest, err = load_manifest(root / "manifest.json",
                                  missing="no archaisms manifest; run `archaisms prepare` first")
    if err:
        return fanout_error(err)
    out = run_wave(Path(project_dir), manifest, command=COMMAND, root=root, **kwargs)
    if out.get("instructions") == "Run commit.":
        out["instructions"] = "Run `archaisms apply`."
    elif "instructions" in out:
        out["instructions"] = out["instructions"].replace("then commit", "then `archaisms apply`")
    return out


# ── check and apply ──────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def check_answer(item: dict[str, Any], entry: archaisms.Entry, answer: dict[str, Any]
                 ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(edit, None)``, ``(None, None)`` for a keep, or ``(None, problem)``.

    An edit is ``{start, end, before, after}`` in chunk offsets. A replace must
    copy ``before`` from the paragraph around the marked occurrence, take the
    old form out, and change no more than the swap and its agreement need.
    """
    decision = str(answer.get("decision", "")).strip().lower()
    if decision == "keep":
        return None, None
    if decision != "replace":
        return None, f"decision {decision!r} is neither replace nor keep"
    # The marks are the prompt's, not the text's; a model that copies them along
    # with the occurrence has still copied the right span.
    before, after = (str(answer.get(k) or "").replace(OPEN, "").replace(CLOSE, "")
                     for k in ("before", "after"))
    if not before or before.strip() == after.strip():
        return None, "replace with an empty or unchanged span"
    if len(_words(before)) > MAX_SPAN_WORDS:
        return None, f"«before» runs {len(_words(before))} words (max {MAX_SPAN_WORDS})"
    para, rel = item["paragraph"], item["offset"] - item["paragraph_start"]
    end = rel + len(item["old"])
    at = None
    for m in re.finditer(re.escape(before), para):
        if m.start() <= rel and end <= m.end():
            at = m.start()
            break
    if at is None:
        return None, "«before» is not copied from the paragraph around the marked occurrence"
    if re.search(archaisms._pattern(entry.old), after) and not re.search(
            archaisms._pattern(entry.replacement), after):
        return None, f"«{entry.old}» is still in «after»"
    sm = difflib.SequenceMatcher(None, _words(before), _words(after), autojunk=False)
    dropped = sum(i2 - i1 for op, i1, i2, _j1, _j2 in sm.get_opcodes() if op in ("replace", "delete"))
    added = sum(j2 - j1 for op, _i1, _i2, j1, j2 in sm.get_opcodes() if op in ("replace", "insert"))
    if dropped > len(_words(entry.old)) + SLACK_WORDS or added > len(_words(entry.replacement)) + SLACK_WORDS:
        return None, f"changes {dropped} words out and {added} in; more than the swap needs"
    start = item["paragraph_start"] + at
    return {"start": start, "end": start + len(before), "before": before, "after": after}, None


def _answers(raw: str) -> dict[int, dict[str, Any]]:
    from src.judges.llm_io import extract_json

    doc = json.loads(extract_json(raw))
    rows = doc.get("items", []) if isinstance(doc, dict) else doc
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            out[int(row.get("n"))] = row
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def apply(project_dir: Path, *, no_realign: bool = False) -> dict[str, Any]:
    """Check every draft's answers, apply the replacements, record every decision."""
    from src.corrections_apply import apply_to_chunk, realign_chapter, recombine_chapter
    from src.harness import state as hstate
    from src.quotations.apply import _write_chunk_snapshot
    from src.utils.file_io import load_chunk, save_chunk

    project_dir = Path(project_dir)
    root = pass_dir(project_dir)
    items_path = root / "items.json"
    if not items_path.exists():
        return {"status": "error", "error": "nothing prepared; run `archaisms prepare` first"}
    items = json.loads(items_path.read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entries = {e.id: e for e in archaisms.load_list(project_dir)}
    writer = f"guided:{manifest.get('worker_model') or '?'}"
    stamp = datetime.now().isoformat(timespec="seconds")
    ts = time.strftime("%Y%m%dT%H%M%S")
    cfg = hstate.load_config(project_dir)
    src_lang = cfg.get("source_language_code") or "es"
    tgt_lang = cfg.get("language_code") or "es"

    by_chunk: dict[str, list[dict]] = {}
    for item in items:
        by_chunk.setdefault(item["chunk_id"], []).append(item)

    replaced, kept, problems, missing = [], [], [], []
    rows: list[dict] = []
    chapters: list[str] = []
    from web_ui.evaluations import mark_evaluation_stale

    for chunk_id, batch in by_chunk.items():
        draft = root / f"{chunk_id}.draft.json"
        if not draft.exists() or not draft.read_text(encoding="utf-8").strip():
            missing.append(chunk_id)
            continue
        try:
            answers = _answers(draft.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            problems.append({"chunk_id": chunk_id, "error": f"unreadable draft: {exc}"})
            continue
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        chunk = load_chunk(chunk_path)
        text = chunk.translated_text or ""
        edits: list[tuple[dict, dict, dict]] = []
        keeps: list[tuple[dict, dict]] = []
        for item in batch:
            answer = answers.get(item["n"])
            entry = entries.get(item["id"])
            base = {"chunk_id": chunk_id, "id": item["id"], "old": item["old"]}
            if answer is None or entry is None:
                problems.append({**base, "error": "no answer for this item" if entry else "entry left the list"})
                continue
            if text[item["offset"]: item["offset"] + len(item["old"])] != item["old"]:
                problems.append({**base, "error": "the chunk changed since prepare"})
                continue
            edit, problem = check_answer(item, entry, answer)
            reason = str(answer.get("reason") or "")
            if problem:
                problems.append({**base, "error": problem, "answer": answer})
            elif edit is None:
                keeps.append((item, {**base, "reason": reason}))
            else:
                edits.append((item, edit, {**base, "reason": reason}))
        # Overlapping edits: the first one (by position) wins, the other is asked again.
        edits.sort(key=lambda t: t[1]["start"])
        chosen, last_end = [], -1
        for item, edit, base in edits:
            if edit["start"] < last_end:
                problems.append({**base, "error": "overlaps another edit in this chunk; asked again next run"})
                continue
            chosen.append((item, edit, base))
            last_end = edit["end"]
        if chosen:
            records = [{"original_es": e["before"], "corrected_es": e["after"],
                        "chunk_offset_start": e["start"], "chunk_offset_end": e["end"]}
                       for _i, e, _b in chosen]
            updated, _n, applied = apply_to_chunk(chunk, records)
            if len(applied) != len(records):
                for k, (_i, _e, base) in enumerate(chosen):
                    if k not in applied:
                        problems.append({**base, "error": "edit did not land"})
            landed = [chosen[k] for k in sorted(applied)]
            if landed:
                _write_chunk_snapshot(project_dir, chunk.chapter_id, chunk_id, chunk_path, ts)
                save_chunk(updated, chunk_path)
                mark_evaluation_stale(project_dir, chunk_id, "translated_text edited by the archaism pass")
                if chunk.chapter_id not in chapters:
                    chapters.append(chunk.chapter_id)
            for item, edit, base in landed:
                replaced.append({**base, "before": edit["before"], "after": edit["after"]})
                rows.append({**base, "mode": "guided", "decision": "replace", "new": edit["after"],
                             "before": edit["before"], "after": edit["after"], "key": item["key"]})
            text = (updated.translated_text or "") if landed else text
            shifts = [(e["end"], len(e["after"]) - len(e["before"])) for _i, e, _b in landed]
        else:
            shifts = []
        # A keep is keyed on the text as it now stands: an edit beside the
        # occurrence changes the window its key is read from.
        for item, base in keeps:
            start = item["offset"] + sum(d for end_, d in shifts if end_ <= item["offset"])
            end = start + len(item["old"])
            kept.append(base)
            rows.append({**base, "mode": "guided", "decision": "keep", "new": None,
                         "key": occurrence_key(chunk_id, item["id"], text, start, end),
                         "context": text[max(0, start - 60): end + 60]})
        draft.rename(draft.with_suffix(".applied.json"))

    if rows:
        with (project_dir / archaisms.LOG_NAME).open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps({"at": stamp, "writer": writer, **row}, ensure_ascii=False) + "\n")

    realigned = []
    if chapters and not no_realign:
        with contextlib.redirect_stdout(sys.stderr):
            for chapter_id in chapters:
                recombine_chapter(project_dir, chapter_id)
                realign_chapter(project_dir, chapter_id, source_lang=src_lang, target_lang=tgt_lang)
                realigned.append(chapter_id)
    return {
        "status": "ok" if not problems and not missing else "partial",
        "replaced": replaced, "kept": kept, "problems": problems,
        "missing_drafts": missing,
        "chapters_edited": chapters, "chapters_realigned": realigned,
        "instructions": (
            "Re-run `archaisms prepare` and `archaisms fanout` to ask again about the problems."
            if problems or missing else "Done."
        ),
    }
