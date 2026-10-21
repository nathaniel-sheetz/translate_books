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


def estimate(
    project_dir: Path, *, judges: Optional[list[str]] = None, cli=None, worker_model=None,
    effort=None, cache: Optional[str] = None,
) -> dict[str, Any]:
    """Prepare the sweep and project its tokens. Spawns nothing."""
    from src.judges import editorial_wave as wave
    from src.judges import subagent

    project_dir = Path(project_dir)
    p = plan(project_dir, judges=judges)
    prof = _profile(project_dir, cli, worker_model, effort)
    out: dict[str, Any] = {"plan": p, "effective": prof.to_payload()}
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
        subagent.prepare(
            project_dir, list(p["owed"]), scopes_for(p["owed"]), context=context,
            cli=cli, worker_model=worker_model, effort=effort,
        )
        fan = subagent.fanout(project_dir, estimate=True, cache=cache)
        if fan.get("error"):
            return {**out, "status": "error", "error": fan["error"]}
        est = fan.get("estimate") or {}
        pass1 = {k: est.get(k) for k in (
            "jobs", "prompt_tokens", "projected_tokens", "output_tokens", "output_source",
            "baseline_tokens", "baseline_source",
        )}
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
    return {**out, "status": "ok", "pass1": pass1, "adjudication": adjudication}


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
) -> dict[str, Any]:
    """Run the sweep under the book's lock. Spends subscription tokens."""
    from src.harness import locks
    from src.judges import sweep

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
                adjudicate_scopes=["book"],
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
                prompt_cache=cache or "auto",
            )
            summary = {**summary, "adjudication": adjudication,
                       "error_count": (summary.get("error_count") or 0) + len(errors),
                       "errors": ((summary.get("errors") or []) + errors)[:5]}
    status = "error" if summary.get("fatal") else "ok"
    return {"status": status, "plan": p, "summary": summary,
            **({"error": summary["fatal"]} if summary.get("fatal") else {})}
