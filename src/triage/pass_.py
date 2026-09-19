"""Prepare, run and commit a machine-triage wave over one book's coded findings.

``prepare`` renders batches of findings into prompts, ``fanout`` runs one
headless wave, and ``commit`` writes the verdicts into
``evaluations/_triage.jsonl``. It is the prepare / fanout / commit flow of
``src/audit/panel.py``, scoped to one book: the input is whatever
``src.triage.findings`` can anchor onto a sentence, and everything except the
verdicts themselves lands under ``.harness/triage/``.

The model is pinned at ``prepare`` time and recorded in the manifest, so
``fanout`` inherits it rather than re-reading the book's ``worker_model``. That
is what keeps this pass off the book's default backend — the reason it has its
own wave type at all — and it is the mechanism ``src/footnote_pass/scan.py``
already uses.

Nothing here edits the book. A verdict suppresses a finding at read time; the
finding stays on disk exactly as the checker wrote it.

Named ``pass_`` because ``pass`` is a keyword and ``src.triage.pass`` cannot be
imported.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.audit.panel import load_book, model_slug
from src.harness import state as hstate
from src.harness.profile import usage_log_for
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.llm_io import (
    JudgeParseError,
    extract_json,
    load_template,
    prompt_version,
    render,
)
from src.triage import findings as tfindings

#: Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_triage``
#: and ``profile.USAGE_LOG_RELPATH``.
COMMAND = "triage"

TEMPLATE = "triage_coded_finding.txt"

#: Findings rendered into one prompt. The same 20 the reader-edit audit settled
#: on: the fixed preamble is most of a single-item job, and batching there moved
#: verdicts no more than a re-run did. Re-measure before moving it.
DEFAULT_ITEMS_PER_JOB = 20
DEFAULT_CONCURRENCY = 3

#: Glossary entries shown per item at most. A long sentence in a book with a
#: large glossary can match many, and the list is a reference, not the task.
MAX_GLOSSARY_HITS = 12

MANIFEST_FILENAME = "manifest.json"
PREAMBLE_FILENAME = "preamble.txt"
REPORT_FILENAME = "report.md"

#: How many rows of the usage log to read for the report: all of them.
_USAGE_ROWS = 1_000_000


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def triage_dir(project_dir: Path | str) -> Path:
    """``.harness/triage/`` — the run directory for this book."""
    return Path(project_dir) / ".harness" / COMMAND


def _manifest_path(project_dir: Path) -> Path:
    return triage_dir(project_dir) / MANIFEST_FILENAME


def _draft_path(project_dir: Path, model: str, job_id: str) -> Path:
    return triage_dir(project_dir) / "drafts" / model_slug(model) / f"{job_id}.json"


def _inside(project_dir: Path, relative: str) -> Path:
    """``triage_dir / relative``, refusing a hand-edited path that escapes it."""
    root = triage_dir(project_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes the triage directory: {relative}")
    return path


def _has_draft(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def _live_drafts(project_dir: Path) -> list[Path]:
    """Draft files a ``commit`` would still read, oldest path first.

    ``<job>.rejected.json`` is deliberately not one. ``commit`` renames a draft
    it could not parse so the evidence survives, and that file is never read
    again — counting it as live would let one malformed draft block every future
    run of the pass on this book.
    """
    root = triage_dir(project_dir) / "drafts"
    if not root.exists():
        return []
    return [
        path for path in sorted(root.rglob("*.json"))
        if not path.name.endswith(".rejected.json")
    ]


def load_manifest(project_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(manifest, None)``, or ``(None, error)`` when there is no readable one."""
    path = _manifest_path(project_dir)
    if not path.exists():
        return None, f"no triage manifest in {triage_dir(project_dir)}; run `prepare` first"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable manifest {path}: {exc}"
    if not isinstance(doc, dict) or not all(k in doc for k in ("jobs", "items", "model")):
        return None, f"malformed manifest {path}: needs jobs, items and model"
    return doc, None


def _error(message: str, schema: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, **extra, "_schema": schema}


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------

def glossary_hits_for_sentence(
    glossary: Any, sentence: str, *, limit: int = MAX_GLOSSARY_HITS
) -> list[str]:
    """The book's glossary entries whose Spanish appears in ``sentence``.

    ``panel.glossary_hits`` matches a glossary against the *English* source,
    which is what a reader-edit audit row carries. A triage item has only the
    Spanish sentence and a Spanish token, so the useful match runs the other
    way: a flagged word that is some term's agreed rendering is settled, and
    that collision is the whole reason to show the list.

    Matched case-insensitively on the Spanish and on each accepted alternative.
    Substring rather than word-boundary on purpose — an inflected or possessive
    form of a glossary term is still that term, and a false hit costs the model
    one line of irrelevant reference.
    """
    if glossary is None or not sentence.strip():
        return []
    haystack = sentence.casefold()
    hits: list[str] = []
    for term in getattr(glossary, "terms", None) or []:
        spanish = (getattr(term, "spanish", "") or "").strip()
        alternatives = [a for a in (getattr(term, "alternatives", None) or []) if a]
        forms = [spanish, *alternatives]
        if not any(f and f.casefold() in haystack for f in forms):
            continue
        line = f"{getattr(term, 'english', '')} → {spanish}"
        if alternatives:
            line += f" (also: {', '.join(alternatives)})"
        hits.append(line)
        if len(hits) >= limit:
            break
    return hits


def format_book_context(slug: str, book: dict[str, Any]) -> str:
    """The block a job opens with. An absent part is named, not left out.

    The address map is deliberately not here: forms of address are a question
    about who is speaking to whom, and no dictionary or grammar finding turns on
    it. Carrying it would cost every job its tokens for nothing.
    """
    return "\n\n".join((
        f"BOOK: {slug}",
        "STYLE GUIDE\n" + (book.get("style_guide") or "(none recorded for this book)"),
        "STYLE RULES\n" + (book.get("style_rules") or "(none recorded for this book)"),
    ))


def build_prompt_parts(
    items: list[dict[str, Any]], book_context: str = ""
) -> tuple[str, str]:
    """``(preamble, body)`` for one job.

    The preamble is byte-identical for every job in a run so the provider's
    prompt cache serves it after the first; the body carries the book's standard
    and the items.
    """
    rendered = render(
        load_template(TEMPLATE),
        {
            "book_context": book_context,
            "item_count": str(len(items)),
            # Last, so text inside an item is never itself scanned for a placeholder.
            "items": json.dumps(items, ensure_ascii=False, indent=1),
        },
    )
    prefix, marker, suffix = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    if not marker:
        raise ValueError(f"prompts/{TEMPLATE} has no cache split marker")
    return prefix, suffix.strip("\n")


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "run_dir": "the .harness/triage directory fanout and commit read",
    "run_id": "stamped onto every verdict this run writes, so a bad wave can be found again",
    "items": "findings to triage, each with every sentence it fired in; a word a "
    "checker found more than once in a chunk is one finding, not one per occurrence",
    "jobs": "headless processes: items / items_per_job, rounded up",
    "items_per_job": "findings rendered into one prompt",
    "by_eval": "items per checker",
    "skipped": "{dismissed, ignored, already_triaged, unanchored, stale_moved, no_evaluation}: "
    "findings deliberately not sent, and why. unanchored and stale_moved are the two that "
    "mean 'no sentence to judge from' — they stay live for the human",
    "cleared_drafts": "drafts from an earlier run deleted before rendering this one. "
    "Pass keep_drafts to refuse instead, when a wave is still in flight",
    "effective": "the resolved profile this run is pinned to (cli, worker_model, effort, "
    "effort_channel, ...). fanout inherits it from the manifest rather than the book's config",
    "preamble_chars": "size of the cacheable preamble every job carries",
    "instructions": "next step",
}


def prepare(
    project_dir: Path | str,
    *,
    chapters: Optional[list[str]] = None,
    items_per_job: int = DEFAULT_ITEMS_PER_JOB,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    effort: Optional[str] = None,
    eval_names: tuple[str, ...] = tfindings.TRIAGE_EVAL_NAMES,
    keep_drafts: bool = False,
    cfg: Optional[dict] = None,
) -> dict[str, Any]:
    """Render one prompt per batch of findings, plus a manifest. No spend.

    Drafts from an earlier run are cleared before rendering, the same default
    ``src/annotations/review.py`` and the editorial pipeline settled on. It
    matters more here than it does there: those two key a draft by note or chunk
    id, while this pass joins by *position*, so a surviving ``job-001.json``
    would answer a fresh ``job-001`` about entirely different findings. Pass
    ``keep_drafts`` to refuse rather than clear when a wave is still in flight.

    Clearing rather than refusing is also what lets a book be triaged twice: a
    committed draft is never removed by ``commit`` — it is what makes a second
    ``commit`` idempotent — so before this, the wave after the first one could
    not be prepared at all.

    Findings are sorted by id before batching, so the same book and filters
    always produce the same jobs.
    """
    from src.harness.profile import resolve_profile
    from src.harness.state import load_config

    project_dir = Path(project_dir)
    if items_per_job < 1:
        return _error(f"items_per_job must be at least 1, got {items_per_job}", _PREPARE_SCHEMA)

    run_dir = triage_dir(project_dir)
    stale = _live_drafts(project_dir)
    if stale and keep_drafts:
        return _error(
            f"{run_dir} holds {len(stale)} draft(s) and keep_drafts was asked for; "
            "commit them, or re-run without keep_drafts to clear them",
            _PREPARE_SCHEMA,
        )

    cfg = load_config(project_dir) if cfg is None else cfg
    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli,
        cli_source="cli",
        worker_model=worker_model,
        worker_model_source="cli",
        effort=effort,
        effort_source="cli",
        cfg=cfg,
        usage_log=usage_log_for(project_dir, COMMAND),
    )

    items, skips = tfindings.collect_book(
        project_dir, chapters=chapters, eval_names=eval_names
    )
    if not items:
        return _error(
            "no findings left to triage: every coded finding is already dismissed, "
            "ignored, triaged, or could not be anchored to a sentence",
            _PREPARE_SCHEMA,
            skipped=skips,
        )
    items.sort(key=lambda it: it["id"])

    book = load_book(project_dir)
    book_context = format_book_context(Path(project_dir).name, book)

    batches = [items[i:i + items_per_job] for i in range(0, len(items), items_per_job)]
    width = max(3, len(str(len(batches))))

    # Render everything before writing anything: a failure part-way would leave
    # a directory the empty-run check above then refuses to prepare again.
    preamble: Optional[str] = None
    rendered: list[tuple[str, list[dict[str, Any]], str]] = []
    try:
        for n, batch in enumerate(batches, 1):
            job_id = f"job-{n:0{width}d}"
            # 1-based, and the same order `jobs[].item_ids` is written in below:
            # that shared order is the whole join, so nothing here may re-sort.
            views = [
                tfindings.item_prompt_view(
                    it,
                    glossary_hits_for_sentence(
                        book.get("glossary"), "\n".join(it.get("sentences") or ())
                    ),
                    number=n,
                )
                for n, it in enumerate(batch, 1)
            ]
            prefix, body = build_prompt_parts(views, book_context)
            if preamble is None:
                preamble = prefix
            elif prefix != preamble:
                raise ValueError(f"{job_id}: the preamble differs between jobs")
            rendered.append((job_id, batch, body))
    except (OSError, ValueError) as exc:
        return _error(f"could not render the prompts: {exc}", _PREPARE_SCHEMA)

    (run_dir / "jobs").mkdir(parents=True, exist_ok=True)
    # Here rather than beside the check above: everything is rendered before
    # anything is written, so a render that failed part-way leaves the previous
    # run's drafts intact and still committable.
    for path in stale:
        path.unlink(missing_ok=True)
    (run_dir / PREAMBLE_FILENAME).write_text(preamble or "", encoding="utf-8")

    run_id = f"triage-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    jobs: list[dict[str, Any]] = []
    item_meta: dict[str, dict[str, Any]] = {}
    for job_id, batch, body in rendered:
        body_rel = f"jobs/{job_id}.body.txt"
        (run_dir / body_rel).write_text(body, encoding="utf-8")
        for it in batch:
            item_meta[it["id"]] = {**it, "job_id": job_id}
        jobs.append({
            "id": job_id,
            "item_ids": [it["id"] for it in batch],
            "body_path": body_rel,
        })

    by_eval: dict[str, int] = {}
    for it in items:
        by_eval[it["eval_name"]] = by_eval.get(it["eval_name"], 0) + 1

    manifest = {
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "template": TEMPLATE,
        "prompt_version": prompt_version(TEMPLATE),
        "model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "items_per_job": items_per_job,
        "eval_names": list(eval_names),
        "chapters": list(chapters) if chapters else None,
        "skipped": skips,
        "preamble_path": PREAMBLE_FILENAME,
        "jobs": jobs,
        "items": item_meta,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "status": "ok",
        "run_dir": str(run_dir),
        "run_id": run_id,
        "items": len(item_meta),
        "jobs": len(jobs),
        "items_per_job": items_per_job,
        "by_eval": by_eval,
        "skipped": skips,
        "cleared_drafts": len(stale),
        "effective": prof.to_payload(),
        "preamble_chars": len(preamble or ""),
        "instructions": (
            f"Run `fanout --project {Path(project_dir).name}`, then "
            f"`commit --project {Path(project_dir).name}`. Nothing here spends."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


# ---------------------------------------------------------------------------
# fanout
# ---------------------------------------------------------------------------

_FANOUT_SCHEMA = {
    "model": "the model this wave ran, from the manifest unless overridden",
    "cli": "headless CLI used",
    "concurrency": "max parallel CLI processes",
    "wrote": "job ids whose drafts were written this wave",
    "failed": "list of {id, error}; re-run fanout for these",
    "skipped": "job ids that already had a non-empty draft",
    "counts": "{wrote, failed, skipped, todo}",
    "usage": "what the wave consumed; per-job rows go to .harness/triage/usage.jsonl",
    "effective": "the resolved profile this wave ran under",
    "error": "present only when the launcher refused to start",
    "instructions": "next step",
}


def _fanout_error(message: str, **extra: Any) -> dict[str, Any]:
    return {
        "error": message,
        "wrote": [],
        "failed": [],
        "skipped": [],
        "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
        **extra,
        "_schema": _FANOUT_SCHEMA,
    }


def fanout(
    project_dir: Path | str,
    *,
    concurrency: Optional[int] = None,
    job_ids: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    effort: Optional[str] = None,
    cli_bin: Optional[str] = None,
    runner=None,
    cfg: Optional[dict] = None,
) -> dict[str, Any]:
    """Run one headless wave over the prepared jobs.

    Each job passes the shared preamble as ``system_prompt_file`` and its batch
    as ``input_text``. A job that already has a non-empty draft is skipped, so
    re-running resumes rather than re-spending.

    The model, CLI and effort come from the manifest — ``prepare`` already
    resolved and recorded them — unless overridden here, in which case the
    manifest is rewritten so ``commit`` records what actually ran.

    ``runner`` is a test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    from src.harness.headless import run_headless_wave
    from src.harness.profile import resolve_profile
    from src.harness.state import load_config

    project_dir = Path(project_dir)
    manifest, error = load_manifest(project_dir)
    if error:
        return _fanout_error(error)

    concurrency = DEFAULT_CONCURRENCY if concurrency is None else int(concurrency)
    if concurrency < 1:
        return _fanout_error(f"concurrency must be at least 1, got {concurrency}")

    cfg = load_config(project_dir) if cfg is None else cfg
    # A level resolved for one CLI is that CLI's table number; carrying it onto
    # the other would write an `[effort=…]` bracket onto a model that never had
    # one. Inherit only when the family has not changed.
    inherited_effort, inherited_effort_source = effort, "cli"
    if not effort and (cli or manifest.get("cli")) == manifest.get("cli"):
        inherited_effort = manifest.get("effort") or "default"
        inherited_effort_source = "manifest"

    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli or manifest.get("cli"),
        cli_source="cli" if cli else "manifest",
        worker_model=worker_model or manifest.get("model"),
        worker_model_source="cli" if worker_model else "manifest",
        effort=inherited_effort,
        effort_source=inherited_effort_source,
        cfg=cfg,
        usage_log=usage_log_for(project_dir, COMMAND),
    )

    # Only the argv channel takes a `--effort` flag; on Cursor the level rides
    # in the model's own bracket and feeding it here would put the flag on a CLI
    # that has none.
    extra_flags = hstate.compose_headless_argv(
        cfg, prof.effort if prof.effort_channel == "argv" else None
    )

    if worker_model or cli or effort:
        manifest["model"] = prof.worker_model
        manifest["cli"] = prof.cli
        manifest["effort"] = prof.effort
        _manifest_path(project_dir).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    jobs = [j for j in manifest["jobs"] if isinstance(j, dict) and j.get("id")]
    if job_ids is not None:
        wanted = set(job_ids)
        unknown = sorted(wanted - {j["id"] for j in jobs})
        if unknown:
            return _fanout_error(f"job ids not in the manifest: {unknown}")
        jobs = [j for j in jobs if j["id"] in wanted]

    try:
        preamble = _inside(project_dir, manifest.get("preamble_path") or PREAMBLE_FILENAME)
    except ValueError as exc:
        return _fanout_error(str(exc))

    ready: list[dict[str, Any]] = []
    skipped: list[str] = []
    pre_failed: list[dict[str, str]] = []
    for job in jobs:
        draft = _draft_path(project_dir, prof.worker_model, job["id"])
        if _has_draft(draft):
            skipped.append(job["id"])
            continue
        try:
            body = _inside(project_dir, job.get("body_path") or "").read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            pre_failed.append({"id": job["id"], "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        draft.parent.mkdir(parents=True, exist_ok=True)
        ready.append({
            "id": job["id"],
            "input_text": body,
            "output_path": str(draft),
            "system_prompt_file": str(preamble),
        })

    base = {
        "model": prof.worker_model,
        "cli": prof.cli,
        "concurrency": concurrency,
        "effective": prof.to_payload(),
        "_schema": _FANOUT_SCHEMA,
    }
    if not ready:
        return {
            **base,
            "wrote": [],
            "failed": pre_failed,
            "skipped": skipped,
            "counts": {
                "wrote": 0, "failed": len(pre_failed),
                "skipped": len(skipped), "todo": len(pre_failed),
            },
            "instructions": "Fix the failed jobs, then re-run fanout." if pre_failed else "Run `commit`.",
        }

    wave = run_headless_wave(
        ready,
        model=prof.worker_model,
        concurrency=concurrency,
        cli=prof.cli,
        cli_bin=cli_bin,
        runner=runner,
        usage_log=usage_log_for(project_dir, COMMAND),
        extra_flags=extra_flags,
        effort=prof.effort,
    )
    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        return {
            **_fanout_error(
                wave["error"],
                failed=pre_failed,
                skipped=skipped,
                counts={
                    "wrote": 0, "failed": len(pre_failed), "skipped": len(skipped),
                    "todo": len(ready) + len(pre_failed),
                },
            ),
            **base,
            "instructions": "Fix the launcher error, then re-run fanout.",
        }

    failed = pre_failed + list(wave.get("failed") or [])
    wrote = list(wave.get("wrote") or [])
    out = {
        **base,
        "wrote": wrote,
        "failed": failed,
        "skipped": skipped,
        "counts": {
            "wrote": len(wrote), "failed": len(failed),
            "skipped": len(skipped), "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Re-run fanout for the failed jobs, then commit."
            if failed else "Run `commit`."
        ),
    }
    for key in ("usage", "warning", "warnings"):
        if wave.get(key):
            out[key] = wave[key]
    if prof.warnings:
        out["warnings"] = list(prof.warnings) + list(out.get("warnings") or [])
    return out


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

_COMMIT_SCHEMA = {
    "status": "'ok' | 'error'",
    "written": "verdicts appended to evaluations/_triage.jsonl this commit",
    "already_recorded": "items that already carried a verdict; commit is re-runnable",
    "suppressed": "verdicts that will actually hide a finding (suppress at or above the floor)",
    "kept": "verdicts that hide nothing: keep, or suppress below the floor",
    "floor": "TRIAGE_CONFIDENCE_FLOOR the counts above were split on",
    "by_eval": "{eval_name: {suppressed, kept}}",
    "failed": "list of {job, problem}. The draft was renamed to <job>.rejected.json, so "
    "re-running fanout re-runs the job",
    "missing": "job ids with no draft yet",
    "report_path": "report.md: what was suppressed, what was kept, and the usage",
    "instructions": "next step",
}


def _item_number(value: Any) -> Optional[int]:
    """``value`` as a 1-based item number, or ``None`` if it is not one.

    Accepts ``3``, ``3.0`` and ``"3"``. A model that quoted its number or emitted
    it as a JSON float has still said unambiguously which finding it means, and
    rejecting the draft over that would cost the whole job to make a point about
    types -- the same twenty-findings-for-one-mistake trade this scheme exists to
    end.

    ``True`` is not a number here. ``bool`` subclasses ``int``, so a stray
    ``"item": true`` would otherwise resolve to item 1 and file that verdict
    against a real finding.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_draft(raw: str, item_ids: list[str]) -> list[dict[str, Any]]:
    """The verdicts in one job's draft, resolved back onto the job's item ids.

    The draft answers with ``item``, each finding's 1-based position in the
    prompt. Those positions resolve against ``item_ids``, which ``prepare`` wrote
    in the order it rendered them, and the returned records carry the full ``id``
    again so ``commit`` joins them to the manifest exactly as before.

    Positions rather than the stored id because a model cannot copy a
    16-hex-character key back reliably; see
    :func:`src.triage.findings.item_prompt_view` for the two different hashes one
    real wave invented for a single finding. The cost of the trade is that a
    number carries no evidence of which finding it means, so the order written
    here and the order rendered there have to stay the same order -- which is why
    neither side ever re-sorts a batch.

    Raises:
        JudgeParseError: not a JSON array; an entry with an unknown verdict, a
            confidence that is not a number in [0, 1], or an ``item`` that is not
            a whole number; or item numbers that are not exactly the job's. A
            batch answering about the wrong findings must never be joined to
            them, and a malformed confidence must never be coerced upward into a
            suppression.
    """
    from web_ui.evaluations import TRIAGE_VERDICTS

    try:
        data = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeParseError(f"not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise JudgeParseError(f"expected a JSON array, got {type(data).__name__}")

    verdicts: list[dict[str, Any]] = []
    for n, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise JudgeParseError(f"entry {n} is not an object")
        number = _item_number(obj.get("item"))
        if number is None:
            raise JudgeParseError(
                f"entry {n}: item {obj.get('item')!r} is not a whole number"
            )
        label = f"item {number}"
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in TRIAGE_VERDICTS:
            raise JudgeParseError(
                f"{label}: verdict {obj.get('verdict')!r} is not one of "
                f"{sorted(TRIAGE_VERDICTS)}"
            )
        raw_confidence = obj.get("confidence")
        # bool subclasses int, so float(True) is 1.0 — a malformed draft
        # would otherwise suppress at full confidence. Same guard as
        # ``_item_number``.
        if isinstance(raw_confidence, bool):
            raise JudgeParseError(
                f"{label}: confidence {raw_confidence!r} is not a number"
            )
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            raise JudgeParseError(
                f"{label}: confidence {raw_confidence!r} is not a number"
            ) from None
        if not 0.0 <= confidence <= 1.0:
            raise JudgeParseError(
                f"{label}: confidence {confidence} is outside [0, 1]"
            )
        verdicts.append({
            "item": number,
            "verdict": verdict,
            "confidence": confidence,
            "reason": str(obj.get("reason") or "").strip(),
        })

    numbers = [v["item"] for v in verdicts]
    if len(numbers) != len(set(numbers)):
        raise JudgeParseError("an item number appears more than once")
    expected = set(range(1, len(item_ids) + 1))
    if set(numbers) != expected:
        missing = sorted(expected - set(numbers))
        unexpected = sorted(set(numbers) - expected)
        raise JudgeParseError(
            f"item numbers do not match the job: missing {missing}, "
            f"unexpected {unexpected}"
        )
    return [
        {
            "id": item_ids[v["item"] - 1],
            "verdict": v["verdict"],
            "confidence": v["confidence"],
            "reason": v["reason"],
        }
        for v in verdicts
    ]


def commit(project_dir: Path | str) -> dict[str, Any]:
    """Parse every draft and append its verdicts to ``evaluations/_triage.jsonl``.

    Safe to re-run: an item that already carries a verdict in the sidecar is
    counted and skipped rather than appended twice. A draft that fails to parse
    is renamed to ``<job>.rejected.json``, keeping the evidence while freeing
    the job for the next fanout.
    """
    from web_ui.evaluations import (
        TRIAGE_CONFIDENCE_FLOOR,
        append_triage,
        build_triaged,
        load_all_triage_by_chunk,
    )

    project_dir = Path(project_dir)
    manifest, error = load_manifest(project_dir)
    if error:
        return _error(error, _COMMIT_SCHEMA)

    model = manifest.get("model")
    run_id = manifest.get("run_id")
    pversion = manifest.get("prompt_version")
    items: dict[str, dict[str, Any]] = manifest["items"]
    jobs = [j for j in manifest["jobs"] if isinstance(j, dict) and j.get("id")]

    existing = load_all_triage_by_chunk(project_dir)
    by_chunk_key = {
        chunk_id: build_triaged(records) for chunk_id, records in existing.items()
    }

    written = 0
    already = 0
    suppressed = 0
    kept = 0
    by_eval: dict[str, dict[str, int]] = {}
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    decided: list[dict[str, Any]] = []

    for job in jobs:
        draft = _draft_path(project_dir, model, job["id"])
        try:
            raw = draft.read_text(encoding="utf-8")
        except FileNotFoundError:
            missing.append(job["id"])
            continue
        except (OSError, UnicodeDecodeError) as exc:
            failed.append({"job": job["id"], "problem": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        if not raw.strip():
            missing.append(job["id"])
            continue
        try:
            parsed = parse_draft(raw, list(job.get("item_ids") or []))
        except JudgeParseError as exc:
            failed.append({"job": job["id"], "problem": str(exc)[:500]})
            draft.replace(draft.with_name(f"{job['id']}.rejected.json"))
            continue

        for verdict in parsed:
            item = items.get(verdict["id"])
            if not item:
                continue
            chunk_id = item["chunk_id"]
            eval_name = item["eval_name"]
            stands = by_chunk_key.get(chunk_id, {}).get((eval_name, item["issue_key"]))
            if stands is not None:
                already += 1
                continue

            append_triage(
                project_dir,
                chunk_id,
                eval_name,
                item["issue_index"],
                verdict["verdict"],
                key=item["issue_key"],
                confidence=verdict["confidence"],
                reason=verdict["reason"],
                term=item.get("term"),
                rule_id=item.get("rule_id"),
                model=model,
                prompt_version=pversion,
                run_id=run_id,
            )
            written += 1
            hides = (
                verdict["verdict"] == "suppress"
                and verdict["confidence"] >= TRIAGE_CONFIDENCE_FLOOR
            )
            bucket = by_eval.setdefault(eval_name, {"suppressed": 0, "kept": 0})
            if hides:
                suppressed += 1
                bucket["suppressed"] += 1
            else:
                kept += 1
                bucket["kept"] += 1
            decided.append({**item, **verdict, "hides": hides})

    report_path = triage_dir(project_dir) / REPORT_FILENAME
    report_path.write_text(
        render_report(project_dir, manifest, decided, TRIAGE_CONFIDENCE_FLOOR),
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "written": written,
        "already_recorded": already,
        "suppressed": suppressed,
        "kept": kept,
        "floor": TRIAGE_CONFIDENCE_FLOOR,
        "by_eval": by_eval,
        "failed": failed,
        "missing": missing,
        "report_path": str(report_path),
        "instructions": (
            "Re-run fanout for the failed and missing jobs, then commit again."
            if failed or missing
            else f"Every job is committed. Read {report_path}."
        ),
        "_schema": _COMMIT_SCHEMA,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _cell(text: Optional[str]) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_report(
    project_dir: Path,
    manifest: dict[str, Any],
    decided: list[dict[str, Any]],
    floor: float,
) -> str:
    """A human-readable account of what this run suppressed and what it kept.

    The suppressed list is written out in full, not summarized. Every line on it
    is a finding a human will now never see, so the only way the pass stays
    auditable is if reading that list is possible.
    """
    from src.harness.usage import read_recent, rollup

    hides = [d for d in decided if d["hides"]]
    keeps = [d for d in decided if not d["hides"]]
    lines = [
        f"# Coded-finding triage: {Path(project_dir).name}",
        "",
        f"Run `{manifest.get('run_id')}` · prepared {manifest.get('prepared_at')} · "
        f"model `{manifest.get('model')}` on `{manifest.get('cli')}` "
        f"(effort {manifest.get('effort')}) · prompt "
        f"`{str(manifest.get('prompt_version'))[:12]}`.",
        "",
        f"**{len(hides)} suppressed** at confidence ≥ {floor}, **{len(keeps)} left live** "
        f"for the reader, out of {len(decided)} judged.",
        "",
        "Nothing was deleted. Every finding below is still in "
        "`evaluations/<chunk_id>.json` exactly as the checker wrote it.",
    ]

    skipped = manifest.get("skipped") or {}
    if skipped:
        parts = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()) if v)
        if parts:
            lines += ["", f"Not sent for triage: {parts}."]

    lines += ["", f"## Suppressed ({len(hides)})", ""]
    if hides:
        lines += [
            "| chunk | checker | term | confidence | reason |",
            "|---|---|---|---|---|",
        ]
        for d in sorted(hides, key=lambda x: (-x["confidence"], x["id"])):
            lines.append(
                f"| {d['chunk_id']} | {d['eval_name']} | {_cell(d.get('term'))} | "
                f"{d['confidence']:.2f} | {_cell(d.get('reason'))} |"
            )
    else:
        lines.append("Nothing was suppressed.")

    lines += ["", f"## Left live ({len(keeps)})", ""]
    if keeps:
        lines += [
            "| chunk | checker | term | verdict | confidence | reason |",
            "|---|---|---|---|---|---|",
        ]
        for d in sorted(keeps, key=lambda x: x["id"]):
            lines.append(
                f"| {d['chunk_id']} | {d['eval_name']} | {_cell(d.get('term'))} | "
                f"{d['verdict']} | {d['confidence']:.2f} | {_cell(d.get('reason'))} |"
            )
    else:
        lines.append("Everything judged was suppressed.")

    usage = rollup(read_recent(usage_log_for(project_dir, COMMAND), limit=_USAGE_ROWS))
    if usage:
        lines += [
            "",
            "## Usage",
            "",
            "| jobs | input | cache write | cache read | output |",
            "|---|---|---|---|---|",
            f"| {usage.get('jobs', 0)} | {usage.get('input', 0):,} | "
            f"{usage.get('cache_creation', 0):,} | {usage.get('cache_read', 0):,} | "
            f"{usage.get('output', 0):,} |",
        ]
    return "\n".join(lines) + "\n"


__all__ = [
    "COMMAND",
    "TEMPLATE",
    "commit",
    "fanout",
    "format_book_context",
    "glossary_hits_for_sentence",
    "load_manifest",
    "parse_draft",
    "prepare",
    "render_report",
    "triage_dir",
]
