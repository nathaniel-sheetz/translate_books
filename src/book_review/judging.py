"""The judge sweep a book review needs before it can read anything.

Runs every LLM judge the book is set up for over the chapters that still owe
that judge (never run, partial, or stale since the prose changed), as one
headless wave, then editorial pass 2 over every chunk still awaiting it. The
wave itself is :func:`src.judges.sweep.run_headless_sweep`, the same body the
dashboard's "Run judges" job runs.

Three entry points, mirroring the consent gate every subscription wave uses:

- :func:`plan` -- what a sweep would cover. Free, writes nothing.
- :func:`estimate` -- ``prepare`` plus a zero-spawn token projection. Free in
  tokens, but ``prepare`` clears the judge drafts directory, so it refuses
  while another wave holds the book.
- :func:`run` -- the sweep, under the book's lock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Optional

#: Judge order inside one wave. Address only runs when the book has a map.
SWEEP_JUDGES: tuple[str, ...] = ("dialogue", "address", "editorial")
LOCK_KIND = "book-review-judges"
#: Wave width when none is passed. Wider than the judge manifests' own 5: a
#: grok-4.7 sweep over a 30-chapter book is ~110 jobs at ~8 min each, and pass 2
#: ran clean at 10 on Cursor (Pollyanna, 2026-10-21).
DEFAULT_CONCURRENCY = 10
#: The agent tool's limit on one background command. A sweep projected past
#: :data:`WALL_WARN_S` is quoted as not fitting, leaving room for the long tail.
BACKGROUND_CAP_S = 7200
WALL_WARN_S = 6000
#: Re-prepare keeps a draft whose prompt is byte-identical to the one it answered,
#: so a sweep cut off part-way resumes instead of re-running what already landed.
RESUME = "if_unchanged"


def _chunks_per_chapter(project_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in (Path(project_dir) / "chunks").glob("*.json"):
        chapter = path.stem.split("_chunk")[0]
        counts[chapter] = counts.get(chapter, 0) + 1
    return counts


def plan(project_dir: Path, *, judges: Optional[list[str]] = None) -> dict[str, Any]:
    """Owed chapters per judge, and chunks awaiting editorial pass 2."""
    from src.judges import editorial_wave as wave
    from src.judges.status import build_status

    project_dir = Path(project_dir)
    has_map = (project_dir / "address_map.json").exists()
    names = [j for j in (judges or SWEEP_JUDGES) if j != "address" or has_map]
    status = build_status(project_dir, judges=names)
    owed: dict[str, list[str]] = {}
    for name in names:
        buckets = ((status.get("judges") or {}).get(name) or {}).get("chapters") or {}
        chapters = [c for state in ("stale", "partial", "not_run") for c in buckets.get(state, [])]
        if chapters:
            owed[name] = chapters
    pending_pass2 = 0
    if "editorial" in names:
        pending, _skipped = wave.collect_pending(project_dir, ["book"])
        pending_pass2 = len(pending)
    per_chapter = _chunks_per_chapter(project_dir)
    return {
        "judges": names,
        "address_map": has_map,
        "owed": owed,
        "owed_chunks": {j: sum(per_chapter.get(c, 0) for c in cs) for j, cs in owed.items()},
        "pending_pass2_chunks": pending_pass2,
        "nothing_to_do": not owed and not pending_pass2,
    }


def scopes_for(owed: dict[str, list[str]]) -> dict[str, list[str]]:
    """The ``{judge: [chapter:<id>, ...]}`` mapping :func:`subagent.prepare` takes."""
    return {judge: [f"chapter:{c}" for c in chapters] for judge, chapters in owed.items()}


def _profile(project_dir: Path, cli, worker_model, effort):
    from src.harness.profile import resolve_profile

    return resolve_profile(
        project_dir, command="judges", cli=cli, worker_model=worker_model, effort=effort,
    )


def _gate(project_dir: Path, prof) -> Optional[str]:
    """Why a destructive ``prepare`` must not run now, or ``None``."""
    from src.harness import locks
    from src.harness.headless import preflight_error

    holder = locks.holder_of(project_dir)
    if holder is not None and not locks.held_by_this_process(holder):
        return (f"Another wave holds this book's lock ({holder.get('kind') or 'background'}, "
                f"pid {holder.get('pid')}); wait for it to finish.")
    return preflight_error(prof.cli, model=prof.worker_model)


def _context(project_dir: Path, names: list[str]):
    from src.judges.context import build_judge_context

    return build_judge_context(project_dir, names, None, None)


def _bad_concurrency(concurrency: Optional[int]) -> Optional[str]:
    if concurrency is not None and concurrency < 1:
        return f"concurrency must be at least 1, got {concurrency}"
    return None


def _project_wall(
    project_dir: Path, prof, *, pass1_jobs: int, pass2_jobs: int, width: int,
    per_judge: dict[str, int],
) -> Optional[dict[str, Any]]:
    """Wall time of both waves from logged job walls, or ``None`` with no history.

    A rolling pool of ``width`` takes about ``jobs x median / width``, plus one
    slow job at the end (p90). Pass 2 is a ceiling: one job per chunk that may
    carry candidates.
    """
    from src.harness.usage import wall_stats
    from src.judges.subagent import usage_log_path

    stats = wall_stats(usage_log_path(project_dir), cli=prof.cli, model=prof.worker_model)
    if not stats:
        return None
    median, p90 = stats["median_s"], stats["p90_s"]

    def span(jobs: int) -> int:
        return round(jobs * median / width + p90) if jobs else 0

    pass1, pass2 = span(pass1_jobs), span(pass2_jobs)
    total = pass1 + pass2
    return {
        "pass1_s": pass1,
        "pass2_max_s": pass2,
        "total_s": total,
        "per_judge_s": {j: span(n) for j, n in per_judge.items()},
        "median_job_s": round(median),
        "p90_job_s": round(p90),
        "source": stats["source"],
        "background_cap_s": BACKGROUND_CAP_S,
        "fits_background_cap": total <= WALL_WARN_S,
    }


def _todo_per_judge(project_dir: Path) -> dict[str, int]:
    """Manifest entries per judge that have no draft yet."""
    import json

    from src.judges.subagent import _judges_dir

    try:
        doc = json.loads((_judges_dir(project_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, int] = {}
    for entry in doc.get("entries") or []:
        draft = Path(entry.get("draft_path") or "")
        if draft.name and draft.exists() and draft.read_text(encoding="utf-8").strip():
            continue
        out[entry.get("judge") or "?"] = out.get(entry.get("judge") or "?", 0) + 1
    return out


def estimate(
    project_dir: Path, *, judges: Optional[list[str]] = None, cli=None, worker_model=None,
    effort=None, cache: Optional[str] = None, concurrency: Optional[int] = None,
) -> dict[str, Any]:
    """Prepare the sweep and project its tokens. Spawns nothing."""
    from src.judges import editorial_wave as wave
    from src.judges import subagent

    project_dir = Path(project_dir)
    p = plan(project_dir, judges=judges)
    prof = _profile(project_dir, cli, worker_model, effort)
    out: dict[str, Any] = {"plan": p, "effective": prof.to_payload(),
                           "concurrency": concurrency or DEFAULT_CONCURRENCY,
                           "concurrency_source": "--concurrency" if concurrency else "default"}
    bad = _bad_concurrency(concurrency)
    if bad:
        return {**out, "status": "error", "error": bad}
    if p["nothing_to_do"]:
        return {**out, "status": "ok", "jobs": 0}
    blocked = _gate(project_dir, prof)
    if blocked:
        return {**out, "status": "error", "error": blocked}

    pass1: dict[str, Any] = {"jobs": 0}
    if p["owed"]:
        context, err = _context(project_dir, list(p["owed"]))
        if err:
            return {**out, "status": "error", "error": err}
        prep = subagent.prepare(
            project_dir, list(p["owed"]), scopes_for(p["owed"]), context=context,
            cli=cli, worker_model=worker_model, effort=effort, keep_drafts=RESUME,
        )
        fan = subagent.fanout(project_dir, estimate=True, cache=cache)
        if fan.get("error"):
            return {**out, "status": "error", "error": fan["error"]}
        est = fan.get("estimate") or {}
        pass1 = {k: est.get(k) for k in (
            "jobs", "prompt_tokens", "projected_tokens", "output_tokens", "output_source",
            "baseline_tokens", "baseline_source",
        )}
        pass1["resumed_drafts"] = prep.get("resumed_drafts", 0)
        out["warnings"] = fan.get("warnings") or []
        out["effective"] = fan.get("effective") or out["effective"]

    adjudication = None
    if "editorial" in p["judges"]:
        # Pass 2 cannot be measured before pass 1 proposes anything: quote a
        # ceiling of one job per chunk that may carry candidates.
        ceiling = p["owed_chunks"].get("editorial", 0) + p["pending_pass2_chunks"]
        baseline, source = wave.baseline_for(project_dir, prof.cli, prof.worker_model)
        adjudication = {"jobs_max": ceiling, "tokens_max": ceiling * baseline,
                        "baseline_tokens": baseline, "baseline_source": source}
    wall = _project_wall(
        project_dir, prof, pass1_jobs=int(pass1.get("jobs") or 0),
        pass2_jobs=(adjudication or {}).get("jobs_max", 0), width=out["concurrency"],
        per_judge=_todo_per_judge(project_dir) if p["owed"] else {},
    )
    if wall is None:
        out.setdefault("warnings", []).append(
            "no logged jobs for this CLI/model; wall time unknown")
    return {**out, "status": "ok", "pass1": pass1, "adjudication": adjudication,
            "wall": wall}


def _stderr_emit(event: str, **fields: Any) -> None:
    message = fields.get("message")
    if event == "phase" and message:
        print(f"[book-review] {message}", file=sys.stderr, flush=True)
    elif event == "target_done":
        print(f"[book-review] {fields.get('index')}/{fields.get('total')} "
              f"{fields.get('target_id')} {'ok' if fields.get('ok') else 'FAILED'}",
              file=sys.stderr, flush=True)


def run(
    project_dir: Path, *, judges: Optional[list[str]] = None, cli=None, worker_model=None,
    effort=None, cache: Optional[str] = None, emit: Callable[..., None] = _stderr_emit,
    concurrency: Optional[int] = None,
) -> dict[str, Any]:
    """Run the sweep under the book's lock. Spends subscription tokens.

    ``concurrency`` is the width of both waves (pass 1 and editorial pass 2);
    ``None`` means :data:`DEFAULT_CONCURRENCY`. A sweep cut off part-way resumes
    on the next call: drafts whose prompt is unchanged are kept, not re-run.
    """
    from src.harness import locks
    from src.judges import sweep

    bad = _bad_concurrency(concurrency)
    if bad:
        return {"status": "error", "error": bad}
    width = concurrency or DEFAULT_CONCURRENCY
    project_dir = Path(project_dir)
    p = plan(project_dir, judges=judges)
    prof = _profile(project_dir, cli, worker_model, effort)
    if p["nothing_to_do"]:
        return {"status": "ok", "plan": p, "summary": None,
                "message": "Every judge is current and nothing awaits pass 2."}
    blocked = _gate(project_dir, prof)
    if blocked:
        return {"status": "error", "error": blocked, "plan": p}

    with locks.project_lock(project_dir, kind=LOCK_KIND):
        names = list(p["owed"])
        summary: dict[str, Any] = {"ran": 0, "error_count": 0, "errors": []}
        if names:
            context, err = _context(project_dir, names)
            if err:
                return {"status": "error", "error": err, "plan": p}
            summary = sweep.run_headless_sweep(
                project_dir, names, scopes_for(p["owed"]), emit,
                context=context, cli=cli, worker_model=worker_model, effort=effort,
                prompt_cache=cache, default_cli=prof.cli,
                adjudicate_scopes=["book"], concurrency=width, keep_drafts=RESUME,
            )
        # The sweep adjudicates only when editorial is among the judges it ran; pass 2
        # can still be owed (and was quoted) when editorial itself is current.
        if "editorial" not in names and p["pending_pass2_chunks"] and not summary.get("fatal"):
            context, err = _context(project_dir, ["editorial"])
            if err:
                return {"status": "error", "error": err, "plan": p}
            adjudication, errors = sweep.adjudicate_editorial(
                project_dir, ["book"], emit, backend="headless", context=context,
                cli=cli, worker_model=worker_model, effort=effort,
                prompt_cache=cache or "auto", concurrency=width, keep_drafts=RESUME,
            )
            summary = {**summary, "adjudication": adjudication,
                       "error_count": (summary.get("error_count") or 0) + len(errors),
                       "errors": ((summary.get("errors") or []) + errors)[:5]}
    status = "error" if summary.get("fatal") else "ok"
    return {"status": status, "plan": p, "summary": summary,
            **({"error": summary["fatal"]} if summary.get("fatal") else {})}
