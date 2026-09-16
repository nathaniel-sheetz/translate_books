"""The Phase 0 reader-edit audit: three model families judge every reader edit.

``prepare`` renders batches of edits into prompts, ``fanout`` runs one headless
wave per panel model, and ``commit`` parses the verdicts into per-row consensus
and a report. It is the prepare / fanout / commit flow of
``src/annotations/review.py``, over the corpus instead of one book: the input is
``scripts/ledger_census.py --export``, and everything lands in one run
directory (``audit/`` is gitignored for it).

A reader often saves a sentence and then edits it again. Judged alone, the
halfway state can look like a regression ("aprietan … haces" before "haces"
became "hacen"), so each run of saves on one sentence is audited once, as its
net change. See :func:`collapse_saves`.

Each job holds one book's edits and opens with that book's own standard: its
style guide, style rules and forms-of-address map, plus each edit's glossary
hits. Without them the fabre2 pilot split on edits the book had already settled:
its glossary gives "la madre Ambroisine", and its address map gives the children
tú with Uncle Paul. See :func:`load_book`.

Nothing here writes into ``projects/``. A consensus is a candidate label, not a
stamp: ``verified_by: "panel"`` waits until the pilot shows the panel holds.

Consensus, over rows every panel model judged:

- ``silver``: unanimous improvement, with every model naming the same class.
- ``taste``: unanimous taste, a suppression candidate.
- ``regression_queue``: at least one regression vote. These go back to the
  reader with each model's reasoning and rewrite.
- ``split``: anything else.

M6 is the share of those rows the panel unanimously calls a regression.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Optional

from src.audit.context import NO_CONTEXT, edit_context
from src.harness.usage import read_recent, rollup
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.context import load_style_rules
from src.judges.llm_io import (
    JudgeParseError,
    extract_json,
    load_template,
    prompt_version,
    render,
)
from src.utils.file_io import (
    filter_glossary_for_chunk,
    load_address_map,
    load_glossary,
    load_style_guide,
)
from src.utils.text_utils import _load_dialogue_block

_REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = "audit_reader_edit.txt"

#: The working panel chosen in Phase 0 §5: three families, all through Cursor.
PANEL_MODELS = (
    "grok-4.6[effort=medium,fast=false]",
    "gemini-3.8-flash-medium",
    "gpt-5.6-terra-medium",
)

VERDICTS = ("improvement", "taste", "regression")
DEFECT_CLASSES = (
    "meaning",
    "grammar",
    "word_choice",
    "naturalness",
    "register",
    "punctuation",
    "names_consistency",
    "typo",
    "none",
)
BUCKETS = ("silver", "taste", "regression_queue", "split")

#: Twenty rows in one process cost about a tenth as much per row as one row per
#: process on the non-Claude panel models (Phase 0 §5), and the pilot found
#: batching moves verdicts no more than a rerun does.
DEFAULT_ROWS_PER_JOB = 20
DEFAULT_CONCURRENCY = 3

#: Glossary entries shown per edit at most. A long sentence in a book with a
#: large glossary can match many, and the list is a reference, not the task.
MAX_GLOSSARY_HITS = 12

MANIFEST_FILENAME = "manifest.json"
PREAMBLE_FILENAME = "preamble.txt"
RESULTS_FILENAME = "results.jsonl"
REPORT_FILENAME = "report.md"

#: How many rows of a usage log to read: all of them, not the baseline window.
_USAGE_ROWS = 1_000_000

#: Rows of ``context_missing`` echoed by ``prepare``; the manifest keeps them all.
_ECHOED_MISSING = 20


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def model_slug(model: str) -> str:
    """A directory-safe name for a model id such as ``grok-4.6[effort=medium,fast=false]``."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model"


def _draft_path(run_dir: Path, model: str, job_id: str) -> Path:
    return run_dir / "drafts" / model_slug(model) / f"{job_id}.json"


def _usage_path(run_dir: Path, model: str) -> Path:
    return run_dir / "usage" / f"{model_slug(model)}.jsonl"


def _inside(run_dir: Path, relative: str) -> Path:
    """``run_dir / relative``, refusing a hand-edited manifest path that escapes the run."""
    path = (run_dir / relative).resolve()
    if not path.is_relative_to(run_dir.resolve()):
        raise ValueError(f"path escapes the run directory: {relative}")
    return path


def _has_draft(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def load_manifest(run_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(manifest, None)``, or ``(None, error)`` when the run has no readable manifest."""
    path = Path(run_dir) / MANIFEST_FILENAME
    if not path.exists():
        return None, f"no manifest in {run_dir}; run `prepare` first"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable manifest {path}: {exc}"
    if not isinstance(doc, dict) or not all(k in doc for k in ("models", "jobs", "rows")):
        return None, f"malformed manifest {path}: needs models, jobs and rows"
    return doc, None


def read_rows(path: Path) -> list[dict[str, Any]]:
    """The export rows in ``path``. Raises ``ValueError`` on a line that is not one."""
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: not JSON: {exc}") from exc
        if not isinstance(row, dict) or not row.get("audit_id"):
            raise ValueError(f"{path}:{n}: not an audit row (no audit_id)")
        rows.append(row)
    return rows


def _text(row: dict[str, Any], field: str) -> str:
    return (row.get(field) or "").strip()


def collapse_saves(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group successive saves on one sentence into chains, each in save order.

    A save continues an earlier one when it is in the same chunk, comes later,
    and starts from exactly the text the earlier one left. Links only run
    forward in save order, so a revert (A→B, then B→A) is one chain rather than
    a loop. A save continues at most one chain.
    """
    ordered = sorted(rows, key=lambda row: (str(row.get("timestamp") or row.get("applied_at") or ""), row["audit_id"]))
    position = {row["audit_id"]: n for n, row in enumerate(ordered)}
    starting: dict[tuple, list[dict[str, Any]]] = {}
    for row in ordered:
        starting.setdefault((row.get("project_id"), row.get("chunk_id"), _text(row, "es_before")), []).append(row)

    successor: dict[str, dict[str, Any]] = {}
    continued: set[str] = set()
    for row in ordered:
        after = _text(row, "es_after")
        if not after:
            continue
        for later in starting.get((row.get("project_id"), row.get("chunk_id"), after), ()):
            if position[later["audit_id"]] > position[row["audit_id"]] and later["audit_id"] not in continued:
                successor[row["audit_id"]] = later
                continued.add(later["audit_id"])
                break

    chains = []
    for row in ordered:
        if row["audit_id"] in continued:
            continue
        chain = [row]
        while chain[-1]["audit_id"] in successor:
            chain.append(successor[chain[-1]["audit_id"]])
        chains.append(chain)
    return chains


def net_edit(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """One chain as a single edit: the first save's before, the last save's after.

    It keeps the last save's ``audit_id``, and ``chain`` lists every save's id
    in order so a verdict joins back to all of them.
    """
    return {
        **chain[-1],
        "es_before": chain[0].get("es_before") or "",
        "chain": [row["audit_id"] for row in chain],
    }


def book_dirs(projects_root: Path, slugs: Iterable[str]) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """``({slug: book dir}, {shared slug: paths})`` for the books the rows name.

    Uses the census's own discovery, so a row always resolves to the book it was
    exported from. An export row carries only the slug, so a slug naming two
    books cannot be resolved at all.
    """
    from scripts.ledger_census import discover_projects, shared_slugs

    wanted = set(slugs)
    in_scope = [p for p in discover_projects(projects_root) if p.name in wanted]
    return {p.name: p for p in in_scope}, shared_slugs(projects_root, in_scope)


def _load_chunk(book_dir: Optional[Path], chunk_id: str) -> Optional[dict[str, Any]]:
    if book_dir is None or not chunk_id:
        return None
    try:
        data = json.loads((book_dir / "chunks" / f"{chunk_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_book(book_dir: Optional[Path]) -> dict[str, Any]:
    """The book's own standard: style guide, style rules, address map and glossary.

    Loaded as the judges load them: the style guide's ``content``, the rule
    sidecar through :func:`load_style_rules`, and the address map's prose
    ``content``, falling back to its ``global_rules``. Every part is optional; a
    missing or unreadable file comes back empty (``None`` for the glossary).
    """
    book: dict[str, Any] = {"style_guide": "", "style_rules": "", "address_map": "", "glossary": None}
    if book_dir is None:
        return book
    try:
        book["style_guide"] = (load_style_guide(book_dir / "style.json").content or "").strip()
    except Exception:  # noqa: BLE001 - an unusable file is an absent one
        pass
    book["style_rules"] = load_style_rules(book_dir)
    try:
        amap = load_address_map(book_dir / "address_map.json")
        book["address_map"] = (amap.content or "").strip() or (amap.global_rules or "").strip()
    except Exception:  # noqa: BLE001 - same
        pass
    try:
        book["glossary"] = load_glossary(book_dir / "glossary.json")
    except Exception:  # noqa: BLE001 - same
        pass
    return book


def book_summary(book: dict[str, Any]) -> dict[str, Any]:
    """Which parts of its standard a book's jobs carry, for the manifest."""
    return {
        "style_guide": bool(book["style_guide"]),
        "style_rules": bool(book["style_rules"]),
        "address_map": bool(book["address_map"]),
        "glossary_terms": len(book["glossary"].terms) if book["glossary"] else 0,
    }


def format_book_context(slug: str, book: dict[str, Any]) -> str:
    """The block a book's jobs open with. An absent part is named, not left out."""
    return "\n\n".join((
        f"BOOK: {slug}",
        "STYLE GUIDE\n" + (book["style_guide"] or "(none recorded for this book)"),
        "STYLE RULES\n" + (book["style_rules"] or "(none recorded for this book)"),
        "FORMS OF ADDRESS\n" + (book["address_map"] or "(no address map for this book)"),
    ))


def glossary_hits(glossary: Any, en: str) -> list[str]:
    """The glossary entries whose English appears in ``en``, as ``english → spanish``.

    Matched the way :func:`filter_glossary_for_chunk` matches a chunk's source,
    variants included, and capped at :data:`MAX_GLOSSARY_HITS`.
    """
    if glossary is None or not en.strip():
        return []
    hits = []
    for term in filter_glossary_for_chunk(glossary, en).terms[:MAX_GLOSSARY_HITS]:
        line = f"{term.english} → {term.spanish}"
        if term.alternatives:
            line += f" (also: {', '.join(term.alternatives)})"
        hits.append(line)
    return hits


def build_item(row: dict[str, Any], context: dict[str, Any], glossary: Iterable[str] = ()) -> dict[str, Any]:
    """One item as the panel reads it. The key order is the prompt's."""
    return {
        "id": row["audit_id"],
        "en": row.get("en") or "",
        "before": row.get("es_before") or "",
        "after": row.get("es_after") or "",
        "starts_paragraph": context["starts_paragraph"],
        "quote_continues": context["quote_continues"],
        "context_before_en": context["context_before_en"],
        "context_before_es": context["context_before_es"],
        "context_after_en": context["context_after_en"],
        "context_after_es": context["context_after_es"],
        "glossary": list(glossary),
    }


def build_prompt_parts(items: list[dict[str, Any]], book_context: str = "") -> tuple[str, str]:
    """``(preamble, body)`` for one job.

    The preamble is the same for every job. The body opens with the book's
    standard, since a job holds one book's edits.
    """
    rendered = render(
        load_template(TEMPLATE),
        {
            "dialogue_rules": _load_dialogue_block(),
            "item_count": str(len(items)),
            "book_context": book_context,
            # Last, so text inside an item is never itself scanned for a placeholder.
            "items": json.dumps(items, ensure_ascii=False, indent=1),
        },
    )
    prefix, marker, suffix = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    if not marker:
        raise ValueError(f"prompts/{TEMPLATE} has no cache split marker")
    return prefix, suffix.strip("\n")


def _error(message: str, schema: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, **extra, "_schema": schema}


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "run_dir": "the run directory that fanout and commit take",
    "models": "the panel this run was prepared for; fanout accepts only these",
    "rows": "net edits to audit, one per chain of saves on a sentence",
    "collapsed": "{chains, saves_merged, reverted}: chains of more than one save; saves "
    "folded into a later one's net edit; chains that ended where they started, which are "
    "dropped. Counted over the whole input, before --project, --limit and exclusions",
    "jobs": "headless processes per model: a job holds one book's rows, so each book's rows / "
    "rows_per_job, rounded up, summed over books",
    "rows_per_job": "edits rendered into one prompt",
    "by_project": "rows per book",
    "books": "{slug: {style_guide, style_rules, address_map, glossary_terms}}: the parts of its "
    "own standard each book's jobs open with. A part the book lacks is named as absent in the prompt",
    "context_missing": "{count, rows}: rows whose sentence was not found in its chunk in "
    "one language or both. They still render, with empty context for that language. "
    "rows lists at most the first 20; the manifest keeps them all",
    "preamble_chars": "size of the preamble every job carries",
    "instructions": "next step",
}


def prepare(
    input_path: Path,
    run_dir: Path,
    *,
    models: Iterable[str] = PANEL_MODELS,
    rows_per_job: int = DEFAULT_ROWS_PER_JOB,
    projects: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    projects_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Render one prompt per batch of edits, plus a manifest, into a new run directory.

    Successive saves on a sentence become one net edit first. Excluding any save
    excludes its net edit. Edits are sorted by ``audit_id`` before ``limit``
    applies, so the same input and filters always select and batch the same
    edits. Each job holds one book's edits and opens with that book's standard
    (:func:`load_book`). The run directory must be new or empty: re-rendering over drafts
    would pair verdicts with other rows. No spend.
    """
    input_path, run_dir = Path(input_path), Path(run_dir)
    models = list(dict.fromkeys(models))
    if not models:
        return _error("at least one model is required", _PREPARE_SCHEMA)
    slugs = [model_slug(m) for m in models]
    if len(set(slugs)) != len(slugs):
        return _error(f"two models share a draft directory name: {models}", _PREPARE_SCHEMA)
    if rows_per_job < 1:
        return _error(f"rows_per_job must be at least 1, got {rows_per_job}", _PREPARE_SCHEMA)
    if limit is not None and limit < 1:
        return _error(f"limit must be at least 1, got {limit}", _PREPARE_SCHEMA)
    if run_dir.exists() and (not run_dir.is_dir() or any(run_dir.iterdir())):
        return _error(
            f"{run_dir} is not an empty directory; prepare always writes a new run",
            _PREPARE_SCHEMA,
        )
    try:
        rows = read_rows(input_path)
    except (OSError, ValueError) as exc:
        return _error(str(exc), _PREPARE_SCHEMA)

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["audit_id"], row)
    changed = [row for row in unique.values() if _text(row, "es_before") != _text(row, "es_after")]
    chains = collapse_saves(changed)
    nets = [net_edit(chain) for chain in chains]
    kept = [net for net in nets if _text(net, "es_before") != _text(net, "es_after")]
    collapsed = {
        "chains": sum(1 for chain in chains if len(chain) > 1),
        "saves_merged": len(changed) - len(chains),
        "reverted": len(nets) - len(kept),
    }

    excluded = set(exclude_ids or ())
    wanted = set(projects) if projects else None
    selected = sorted(
        (
            net for net in kept
            if not excluded.intersection(net["chain"])
            and (wanted is None or net.get("project_id") in wanted)
        ),
        key=lambda net: net["audit_id"],
    )
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        return _error("no rows left to audit after filtering", _PREPARE_SCHEMA)

    root = Path(projects_root) if projects_root else _REPO_ROOT / "projects"
    books, shared = book_dirs(root, {row.get("project_id") or "" for row in selected})
    if shared:
        return _error(
            "a slug names more than one book, so its rows cannot be resolved",
            _PREPARE_SCHEMA,
            shared=shared,
        )

    chunks: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
    standards: dict[str, dict[str, Any]] = {}
    items_by_book: dict[str, list[dict[str, Any]]] = {}
    row_meta: dict[str, dict[str, Any]] = {}
    context_missing: list[dict[str, Any]] = []
    by_project: dict[str, int] = {}
    for row in selected:
        slug, chunk_id = row.get("project_id") or "", row.get("chunk_id") or ""
        if (slug, chunk_id) not in chunks:
            chunks[(slug, chunk_id)] = _load_chunk(books.get(slug), chunk_id)
        chunk = chunks[(slug, chunk_id)]
        context = edit_context(chunk, row) if chunk is not None else dict(NO_CONTEXT)
        if not (context["en_found"] and context["es_found"]):
            context_missing.append({
                "audit_id": row["audit_id"],
                "project_id": slug,
                "chunk_id": chunk_id,
                "reason": (
                    "book_not_found" if slug not in books
                    else "chunk_unreadable" if chunk is None
                    else "sentence_not_found"
                ),
                "en_found": context["en_found"],
                "es_found": context["es_found"],
            })
        if slug not in standards:
            standards[slug] = load_book(books.get(slug))
        hits = glossary_hits(standards[slug]["glossary"], row.get("en") or "")
        items_by_book.setdefault(slug, []).append(build_item(row, context, hits))
        by_project[slug] = by_project.get(slug, 0) + 1
        row_meta[row["audit_id"]] = {
            "project_id": slug,
            "chapter_id": row.get("chapter_id"),
            "chunk_id": chunk_id,
            "es_idx": row.get("es_idx"),
            "en": row.get("en") or "",
            "es_before": row.get("es_before") or "",
            "es_after": row.get("es_after") or "",
            "chain": row["chain"],
            "verified_by": row.get("verified_by"),
            "status": row.get("status"),
            "starts_paragraph": context["starts_paragraph"],
            "quote_continues": context["quote_continues"],
            "glossary": hits,
        }

    # One book per job, so each job can open with that book's standard.
    batches = [
        (slug, book_items[i:i + rows_per_job])
        for slug, book_items in sorted(items_by_book.items())
        for i in range(0, len(book_items), rows_per_job)
    ]
    width = max(3, len(str(len(batches))))
    # Render every job before writing anything: a failure part-way would leave a
    # directory the empty-run check above then refuses to prepare again.
    preamble: Optional[str] = None
    rendered: list[tuple[str, str, list[dict[str, Any]], str]] = []
    try:
        for n, (slug, batch) in enumerate(batches, 1):
            job_id = f"job-{n:0{width}d}"
            prefix, body = build_prompt_parts(batch, format_book_context(slug, standards[slug]))
            if preamble is None:
                preamble = prefix
            elif prefix != preamble:
                raise ValueError(f"{job_id}: the preamble differs between jobs")
            rendered.append((job_id, slug, batch, body))
    except (OSError, ValueError) as exc:
        return _error(f"could not render the prompts: {exc}", _PREPARE_SCHEMA)

    (run_dir / "jobs").mkdir(parents=True, exist_ok=True)
    (run_dir / PREAMBLE_FILENAME).write_text(preamble or "", encoding="utf-8")
    jobs = []
    for job_id, slug, batch, body in rendered:
        body_rel = f"jobs/{job_id}.body.txt"
        (run_dir / body_rel).write_text(body, encoding="utf-8")
        for item in batch:
            row_meta[item["id"]]["job_id"] = job_id
        jobs.append({
            "id": job_id,
            "project_id": slug,
            "audit_ids": [item["id"] for item in batch],
            "body_path": body_rel,
        })

    manifest = {
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "input": str(input_path),
        "template": TEMPLATE,
        "prompt_version": prompt_version(TEMPLATE),
        "models": models,
        "rows_per_job": rows_per_job,
        "collapsed": collapsed,
        "filters": {
            "projects": sorted(wanted) if wanted else None,
            "limit": limit,
            "excluded": sum(1 for net in kept if excluded.intersection(net["chain"])),
        },
        "books": {slug: book_summary(book) for slug, book in sorted(standards.items())},
        "preamble_path": PREAMBLE_FILENAME,
        "jobs": jobs,
        "rows": row_meta,
        "context_missing": context_missing,
    }
    (run_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return {
        "status": "ok",
        "run_dir": str(run_dir),
        "models": models,
        "rows": len(row_meta),
        "collapsed": collapsed,
        "jobs": len(jobs),
        "rows_per_job": rows_per_job,
        "by_project": by_project,
        "books": {slug: book_summary(book) for slug, book in sorted(standards.items())},
        "context_missing": {"count": len(context_missing), "rows": context_missing[:_ECHOED_MISSING]},
        "preamble_chars": len(preamble or ""),
        "instructions": (
            f"Run `fanout --run {run_dir}` (every panel model in turn, or --model for one), "
            f"then `commit --run {run_dir}`. Nothing here spends."
        ),
        "_schema": _PREPARE_SCHEMA,
    }


# ---------------------------------------------------------------------------
# fanout
# ---------------------------------------------------------------------------

_FANOUT_SCHEMA = {
    "model": "the panel model this wave ran",
    "cli": "headless CLI used (cursor for the panel)",
    "concurrency": "max parallel CLI processes",
    "wrote": "job ids whose drafts were written this wave",
    "failed": "list of {id, error}; re-run fanout for these",
    "skipped": "job ids that already had a non-empty draft",
    "counts": "{wrote, failed, skipped, todo}",
    "usage": "what the wave consumed, as the headless launcher reports it; per-job rows "
    "go to usage/<model>.jsonl in the run directory. Cursor reports no cost",
    "error": "present only when the launcher refused to start (missing CLI, login, bad model)",
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
    run_dir: Path,
    *,
    model: str,
    concurrency: Optional[int] = None,
    job_ids: Optional[list[str]] = None,
    cli: str = "cursor",
    cli_bin: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless wave of ``model`` over the run's jobs.

    Each job passes the shared preamble as ``system_prompt_file`` and its batch
    as ``input_text``; the launcher writes the model's output to the draft path.
    A job that already has a non-empty draft is skipped, so re-running resumes.

    ``runner`` is a test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    from src.harness.headless import run_headless_wave

    run_dir = Path(run_dir)
    manifest, error = load_manifest(run_dir)
    if error:
        return _fanout_error(error)
    if model not in manifest["models"]:
        return _fanout_error(f"model {model!r} is not on this run's panel: {manifest['models']}")
    concurrency = DEFAULT_CONCURRENCY if concurrency is None else int(concurrency)
    if concurrency < 1:
        return _fanout_error(f"concurrency must be at least 1, got {concurrency}")

    jobs = [job for job in manifest["jobs"] if isinstance(job, dict) and job.get("id")]
    if job_ids is not None:
        wanted = set(job_ids)
        unknown = sorted(wanted - {job["id"] for job in jobs})
        if unknown:
            return _fanout_error(f"job ids not in the manifest: {unknown}")
        jobs = [job for job in jobs if job["id"] in wanted]

    try:
        preamble = _inside(run_dir, manifest.get("preamble_path") or PREAMBLE_FILENAME)
    except ValueError as exc:
        return _fanout_error(str(exc))

    ready: list[dict[str, Any]] = []
    skipped: list[str] = []
    pre_failed: list[dict[str, str]] = []
    for job in jobs:
        draft = _draft_path(run_dir, model, job["id"])
        if _has_draft(draft):
            skipped.append(job["id"])
            continue
        try:
            body = _inside(run_dir, job.get("body_path") or "").read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            pre_failed.append({"id": job["id"], "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        ready.append({
            "id": job["id"],
            "input_text": body,
            "output_path": str(draft),
            "system_prompt_file": str(preamble),
        })

    base = {"model": model, "cli": cli, "concurrency": concurrency, "_schema": _FANOUT_SCHEMA}
    if not ready:
        return {
            **base,
            "wrote": [],
            "failed": pre_failed,
            "skipped": skipped,
            "counts": {"wrote": 0, "failed": len(pre_failed), "skipped": len(skipped), "todo": len(pre_failed)},
            "instructions": "Fix the failed jobs, then re-run fanout." if pre_failed else "Run `commit`.",
        }

    wave = run_headless_wave(
        ready,
        model=model,
        concurrency=concurrency,
        cli=cli,
        cli_bin=cli_bin,
        runner=runner,
        usage_log=_usage_path(run_dir, model),
    )
    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        return {
            **_fanout_error(
                wave["error"],
                failed=pre_failed,
                skipped=skipped,
                counts={
                    "wrote": 0,
                    "failed": len(pre_failed),
                    "skipped": len(skipped),
                    "todo": len(ready) + len(pre_failed),
                },
            ),
            "model": model,
            "cli": cli,
            "concurrency": concurrency,
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
            "wrote": len(wrote),
            "failed": len(failed),
            "skipped": len(skipped),
            "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Re-run fanout for the failed jobs, then commit."
            if failed
            else "Run `commit` once every panel model has run."
        ),
    }
    if wave.get("usage"):
        out["usage"] = wave["usage"]
    return out


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

_COMMIT_SCHEMA = {
    "status": "'ok' | 'error'",
    "results_path": "results.jsonl: one row per edit with every model's vote and the consensus",
    "report_path": "report.md: verdicts, agreement, consensus, M6, the regression queue, usage",
    "rows": "edits in the run",
    "complete": "edits every panel model judged; consensus and M6 count only these",
    "buckets": "{silver, taste, regression_queue, split} over complete rows",
    "m6": "{unanimous_regression, any_regression, of, share, any_share}",
    "by_model": "{model: {improvement, taste, regression, judged, failed, missing}}: failed and "
    "missing count jobs",
    "agreement": "list of {models, same, of}: rows both models judged, and how many share a verdict",
    "failed": "list of {model, job, problem}. The draft was renamed to <job>.rejected.json, so "
    "re-running fanout re-runs the job",
    "missing": "list of {model, job} with no draft yet",
    "usage": "{model: token rollup from usage/<model>.jsonl, or null}",
    "instructions": "next step",
}


def parse_draft(raw: str, audit_ids: list[str]) -> list[dict[str, Any]]:
    """The verdicts in one job's draft.

    Raises:
        JudgeParseError: not a JSON array; an item with an unknown verdict or
            class; or ids that are not exactly the job's. A batch that answers
            about the wrong rows must never be joined to them.
    """
    try:
        data = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeParseError(f"not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise JudgeParseError(f"expected a JSON array, got {type(data).__name__}")

    verdicts = []
    for n, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise JudgeParseError(f"item {n} is not an object")
        audit_id = str(obj.get("id") or "").strip()
        label = audit_id or f"#{n}"
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            raise JudgeParseError(f"item {label}: verdict {obj.get('verdict')!r} is not one of {list(VERDICTS)}")
        # "word choice" and "word-choice" name the same class as "word_choice".
        defect_class = re.sub(r"[\s-]+", "_", str(obj.get("defect_class") or "").strip().lower())
        if defect_class not in DEFECT_CLASSES:
            raise JudgeParseError(
                f"item {label}: defect_class {obj.get('defect_class')!r} is not one of {list(DEFECT_CLASSES)}"
            )
        rewrite = obj.get("native_rewrite")
        verdicts.append({
            "id": audit_id,
            "verdict": verdict,
            "defect_class": defect_class,
            "reason": str(obj.get("reason") or "").strip(),
            "native_rewrite": str(rewrite).strip() if rewrite not in (None, "") else None,
        })

    ids = [v["id"] for v in verdicts]
    if len(ids) != len(set(ids)):
        raise JudgeParseError("an id appears more than once")
    if set(ids) != set(audit_ids):
        missing = sorted(set(audit_ids) - set(ids))
        unexpected = sorted(set(ids) - set(audit_ids))
        raise JudgeParseError(f"ids do not match the job: missing {missing}, unexpected {unexpected}")
    return verdicts


def consensus(votes: dict[str, dict[str, Any]], models: list[str]) -> Optional[str]:
    """The row's bucket, or ``None`` until every panel model has voted."""
    if any(model not in votes for model in models):
        return None
    verdicts = [votes[model]["verdict"] for model in models]
    if "regression" in verdicts:
        return "regression_queue"
    if all(v == "improvement" for v in verdicts):
        classes = {votes[model]["defect_class"] for model in models}
        return "silver" if len(classes) == 1 else "split"
    if all(v == "taste" for v in verdicts):
        return "taste"
    return "split"


def _share(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def commit(run_dir: Path) -> dict[str, Any]:
    """Parse every draft, join the votes to their rows, and write results and report.

    Safe to re-run as more waves land. A draft that fails to parse is renamed to
    ``<job>.rejected.json``, keeping the evidence while freeing the job for the
    next fanout.
    """
    run_dir = Path(run_dir)
    manifest, error = load_manifest(run_dir)
    if error:
        return _error(error, _COMMIT_SCHEMA)
    models: list[str] = list(manifest["models"])
    jobs = [job for job in manifest["jobs"] if isinstance(job, dict) and job.get("id")]
    rows: dict[str, dict[str, Any]] = manifest["rows"]

    votes: dict[str, dict[str, dict[str, Any]]] = {audit_id: {} for audit_id in rows}
    failed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    jobs_per_model: dict[str, dict[str, int]] = {m: {"failed": 0, "missing": 0} for m in models}
    for model in models:
        for job in jobs:
            draft = _draft_path(run_dir, model, job["id"])
            try:
                raw = draft.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = ""
            except (OSError, UnicodeDecodeError) as exc:
                failed.append({"model": model, "job": job["id"], "problem": f"{type(exc).__name__}: {exc}"[:500]})
                jobs_per_model[model]["failed"] += 1
                continue
            if not raw.strip():
                missing.append({"model": model, "job": job["id"]})
                jobs_per_model[model]["missing"] += 1
                continue
            try:
                parsed = parse_draft(raw, list(job.get("audit_ids") or []))
            except JudgeParseError as exc:
                failed.append({"model": model, "job": job["id"], "problem": str(exc)[:500]})
                jobs_per_model[model]["failed"] += 1
                draft.replace(draft.with_name(f"{job['id']}.rejected.json"))
                continue
            for verdict in parsed:
                if verdict["id"] in votes:
                    votes[verdict["id"]][model] = {k: v for k, v in verdict.items() if k != "id"}

    results = []
    for audit_id in sorted(rows):
        row_votes = votes[audit_id]
        bucket = consensus(row_votes, models)
        results.append({
            "audit_id": audit_id,
            **rows[audit_id],
            "votes": row_votes,
            "complete": bucket is not None,
            "consensus": bucket,
            "unanimous_regression": bucket is not None
            and all(row_votes[m]["verdict"] == "regression" for m in models),
        })

    complete = [r for r in results if r["complete"]]
    buckets = {b: sum(1 for r in complete if r["consensus"] == b) for b in BUCKETS}
    unanimous = sum(1 for r in complete if r["unanimous_regression"])
    m6 = {
        "unanimous_regression": unanimous,
        "any_regression": buckets["regression_queue"],
        "of": len(complete),
        "share": _share(unanimous, len(complete)),
        "any_share": _share(buckets["regression_queue"], len(complete)),
    }
    by_model = {
        model: {
            **{v: sum(1 for r in results if r["votes"].get(model, {}).get("verdict") == v) for v in VERDICTS},
            "judged": sum(1 for r in results if model in r["votes"]),
            **jobs_per_model[model],
        }
        for model in models
    }
    agreement = []
    for a, b in combinations(models, 2):
        both = [r for r in results if a in r["votes"] and b in r["votes"]]
        same = sum(1 for r in both if r["votes"][a]["verdict"] == r["votes"][b]["verdict"])
        agreement.append({"models": [a, b], "same": same, "of": len(both)})
    usage = {model: rollup(read_recent(_usage_path(run_dir, model), limit=_USAGE_ROWS)) for model in models}

    summary = {
        "committed_at": datetime.now().isoformat(timespec="seconds"),
        "rows": len(results),
        "complete": len(complete),
        "buckets": buckets,
        "m6": m6,
        "by_model": by_model,
        "agreement": agreement,
        "usage": usage,
    }
    results_path = run_dir / RESULTS_FILENAME
    results_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8",
    )
    report_path = run_dir / REPORT_FILENAME
    report_path.write_text(render_report(run_dir, manifest, summary, results), encoding="utf-8")

    return {
        "status": "ok",
        "results_path": str(results_path),
        "report_path": str(report_path),
        **summary,
        "failed": failed,
        "missing": missing,
        "instructions": (
            "Re-run fanout for the failed and missing jobs, then commit again."
            if failed or missing
            else f"Every panel model judged every row. Read {report_path}."
        ),
        "_schema": _COMMIT_SCHEMA,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _pct(share: Optional[float]) -> str:
    return "n/a" if share is None else f"{100 * share:.1f}%"


def _cell(text: Optional[str]) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_report(
    run_dir: Path,
    manifest: dict[str, Any],
    summary: dict[str, Any],
    results: list[dict[str, Any]],
) -> str:
    models: list[str] = manifest["models"]
    m6 = summary["m6"]
    lines = [
        f"# Reader-edit audit: {run_dir.name}",
        "",
        f"Prepared {manifest.get('prepared_at')} from `{manifest.get('input')}`: "
        f"{summary['rows']} edits in {len(manifest['jobs'])} jobs of up to "
        f"{manifest.get('rows_per_job')}, prompt `{str(manifest.get('prompt_version'))[:12]}`. "
        f"Committed {summary['committed_at']}.",
    ]
    collapsed = manifest.get("collapsed") or {}
    if collapsed.get("saves_merged") or collapsed.get("reverted"):
        lines.append(
            f"Across the whole input, before filters: {collapsed.get('saves_merged', 0)} later saves were folded into "
            f"{collapsed.get('chains', 0) - collapsed.get('reverted', 0)} net edits, and {collapsed.get('reverted', 0)} "
            "sequences that ended where they started were left out."
        )
    if manifest.get("context_missing"):
        lines.append(
            f"{len(manifest['context_missing'])} edits had context missing in one language or "
            "both (listed in the manifest)."
        )

    lines += [
        "",
        "## Verdicts by model",
        "",
        "| model | judged | improvement | taste | regression | failed jobs | missing jobs |",
        "|---|---|---|---|---|---|---|",
    ]
    for model in models:
        s = summary["by_model"][model]
        lines.append(
            f"| `{model}` | {s['judged']} | {s['improvement']} | {s['taste']} | "
            f"{s['regression']} | {s['failed']} | {s['missing']} |"
        )

    lines += ["", "## Agreement", "", "| pair | same verdict | rows both judged |", "|---|---|---|"]
    for pair in summary["agreement"]:
        lines.append(f"| `{pair['models'][0]}` – `{pair['models'][1]}` | {pair['same']} | {pair['of']} |")

    lines += [
        "",
        f"## Consensus ({summary['complete']} of {summary['rows']} edits judged by every model)",
        "",
        "| bucket | edits | share |",
        "|---|---|---|",
    ]
    for bucket in BUCKETS:
        count = summary["buckets"][bucket]
        lines.append(f"| {bucket} | {count} | {_pct(_share(count, summary['complete']))} |")
    lines += [
        "",
        f"**M6, unanimous regression:** {m6['unanimous_regression']} of {m6['of']} "
        f"({_pct(m6['share'])}). Any regression vote: {m6['any_regression']} ({_pct(m6['any_share'])}).",
    ]

    queue = [r for r in results if r["consensus"] == "regression_queue"]
    lines += ["", f"## Regression queue ({len(queue)})"]
    for r in queue:
        saves = f" · {len(r['chain'])} saves" if len(r.get("chain") or []) > 1 else ""
        lines += [
            "",
            f"### `{r['audit_id']}` · {r['project_id']} · {r['chunk_id']}{saves}",
            "",
            f"- **EN:** {_cell(r['en'])}",
            f"- **Before:** {_cell(r['es_before'])}",
            f"- **After:** {_cell(r['es_after'])}",
        ]
        for model in models:
            vote = r["votes"][model]
            rewrite = f" Rewrite: {_cell(vote['native_rewrite'])}" if vote.get("native_rewrite") else ""
            lines.append(
                f"- **`{model}`:** {vote['verdict']} ({vote['defect_class']}). {_cell(vote['reason'])}{rewrite}"
            )

    lines += [
        "",
        "## Usage",
        "",
        "| model | jobs | input | cache write | cache read | output |",
        "|---|---|---|---|---|---|",
    ]
    for model in models:
        u = summary["usage"].get(model) or {}
        lines.append(
            f"| `{model}` | {u.get('jobs', 0)} | {u.get('input', 0):,} | {u.get('cache_creation', 0):,} | "
            f"{u.get('cache_read', 0):,} | {u.get('output', 0):,} |"
        )
    lines.append("")
    lines.append(
        "Cursor reports no cost. Price a wave from these counts at the rates on "
        "cursor.com/docs/models, billing cache writes at the input rate."
    )
    return "\n".join(lines) + "\n"
