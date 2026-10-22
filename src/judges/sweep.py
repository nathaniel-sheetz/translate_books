"""Run several judges over a book as one headless wave, then editorial pass 2.

The dashboard's "Run judges" job and ``scripts/book_review.py judge`` share this
body, so a sweep started from either surface prepares, fans out, commits and
adjudicates the same way. Callers own the gates that must come *before* any
destructive ``prepare``: a running-job / lock check and the CLI preflight.

Both functions report progress through ``emit(event, **fields)`` -- the jobs
module's callback shape -- and never raise once pass 1 has persisted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

Emit = Callable[..., None]


def adjudicate_editorial(
    project_dir, scopes, emit, *, backend, context,
    cli=None, worker_model=None, effort=None, prompt_cache="auto", concurrency=None,
    keep_drafts=False,
):
    """Pass 2 of the editorial judge, run inline after pass 1 in the same job.

    Pass 1's ``issues[]`` are *proposals*: the judge reads the Spanish alone and
    over-proposes on purpose, and ``metadata.verified`` stays false until this
    pass has adjudicated them against the English. Leaving that gap open in the
    GUI would light a dashboard badge from un-adjudicated candidates — and
    because :func:`web_ui.evaluations.merge_judge_result` replaces a judge's
    whole result, the obvious repair (re-run the judge later) silently discards
    whatever adjudication *had* landed. So the two passes are one gesture here.

    No ``force``: pass 1 has just rewritten these results, so ``verified`` is
    false again and every chunk carrying candidates is naturally pending. A
    chunk pass 1 found clean is skipped as ``no_candidates`` — a clean chunk,
    not a gap.

    Never raises. Pass 1's findings are already persisted by the time this runs,
    and a failure to second-guess them is not a reason to report the whole wave
    as stopped; the caller folds ``errors`` into the job summary and the Review
    tab's banner offers the retry. Returns ``(summary, errors)``.

    ``concurrency`` widens the headless wave; ``None`` keeps the manifest's
    ``batch_size``. ``keep_drafts`` is handed to ``prepare`` (``"if_unchanged"``
    resumes a cut-off wave). The API backend ignores both.
    """
    from src.judges import editorial_wave as wave

    errors: list[str] = []

    def summary(rollup=None, *, chunks=0, unverified=0):
        rollup = rollup or {}
        return {
            "chunks": chunks,
            "confirmed": rollup.get("confirmed", 0),
            "reclassified": rollup.get("reclassified", 0),
            "retracted": rollup.get("retracted", 0),
            "source_used": rollup.get("source_used", 0),
            "unverified": unverified,
        }

    try:
        if backend == "api":
            emit("phase", phase="adjudicate", message="Adjudicating findings…")
            # ``confirm`` because the caller already gated on an estimate that
            # included this pass; a second dollar gate inside a running job has
            # nobody to ask.
            out = wave.run_api(
                project_dir, scopes, context=context, persist=True, confirm=True
            )
            if out.get("status") == "error":
                errors.append(f"adjudication: {out.get('error')}")
                return summary(), errors
            results = out.get("results") or []
            chunks = (out.get("counts") or {}).get("chunks", 0)
            for record in results:
                if record.get("status") != "ok":
                    errors.append(
                        f"{record.get('chunk_id')}: adjudication "
                        f"{record.get('status') or 'failed'}"
                    )
            landed = sum(1 for r in results if r.get("status") == "ok")
            return (
                summary(out.get("rollup"), chunks=chunks,
                        unverified=max(0, chunks - landed)),
                errors,
            )

        emit("phase", phase="adjudicate_prepare", message="Preparing adjudication…")
        prep = wave.prepare(
            project_dir, scopes, context=context,
            cli=cli, worker_model=worker_model, effort=effort, quiet=True,
            keep_drafts=keep_drafts,
        )
        if prep.get("status") == "error":
            errors.append(f"adjudication: {prep.get('error')}")
            return summary(), errors
        chunks = (prep.get("counts") or {}).get("chunks", 0)
        if not chunks:
            # Pass 1 proposed nothing anywhere in scope. That is a clean book,
            # not a failure, and it costs no wave.
            return summary(), errors

        resumed = prep.get("resumed_drafts") or 0
        emit(
            "phase", phase="adjudicate",
            message=f"Adjudicating {chunks} chunk(s)"
            + (f", {resumed} already drafted" if resumed else "")
            + (f", {concurrency} at a time" if concurrency else "") + "…",
            total=chunks,
        )
        out = wave.fanout(
            project_dir,
            cache=prompt_cache,
            concurrency=concurrency,
            on_job_done=lambda rec: emit(
                "target_done",
                target_id=rec.get("id"),
                index=rec.get("done"),
                total=rec.get("total"),
                ok=rec.get("ok"),
            ),
        )
        if out.get("error"):
            errors.append(f"adjudication: {out['error']}")
            return summary(chunks=chunks, unverified=chunks), errors

        emit("phase", phase="adjudicate_commit", message="Committing verdicts…")
        landed = wave.commit(project_dir, persist=True)
        if landed.get("status") == "error":
            errors.append(
                f"adjudication commit failed: {landed.get('error') or 'unknown error'}"
            )
            return summary(chunks=chunks, unverified=chunks), errors

        errors += [
            f"{f.get('chunk_id')}: {f.get('error')}" for f in landed.get("failed") or []
        ]
        errors += [
            f"{m}: adjudication draft missing" for m in landed.get("missing") or []
        ]
        committed = landed.get("committed", 0)
        return (
            summary(landed.get("rollup"), chunks=chunks,
                    unverified=max(0, chunks - committed)),
            errors,
        )
    except Exception as exc:  # noqa: BLE001 - pass 1 is already persisted
        logger.exception("Editorial adjudication failed for %s", project_dir)
        errors.append(f"adjudication: {exc}")
        return summary(), errors


def run_headless_sweep(
    project_dir: Path,
    judge_names: list[str],
    scopes: Any,
    emit: Emit,
    *,
    context: Optional[dict[str, Any]],
    cli: Optional[str] = None,
    worker_model: Optional[str] = None,
    effort: Optional[str] = None,
    prompt_cache: Optional[str] = None,
    default_cli: Optional[str] = None,
    total_hint: int = 0,
    adjudicate_scopes: Any = None,
    concurrency: Optional[int] = None,
    keep_drafts: Any = False,
) -> dict[str, Any]:
    """``prepare`` -> ``fanout`` -> ``commit(persist)`` -> editorial pass 2.

    ``scopes`` is anything :func:`src.judges.subagent.prepare` accepts,
    including a ``{judge: [scope, ...]}`` mapping. Pass 2 runs over
    ``adjudicate_scopes`` (default: ``scopes``) whenever ``editorial`` is in
    ``judge_names``. ``concurrency`` is the width of both waves; ``None`` keeps
    each manifest's ``batch_size``. ``keep_drafts`` reaches both ``prepare``
    calls; ``"if_unchanged"`` lets a re-run resume a sweep that was cut off.

    Returns the job summary: ``ran``, ``total``, ``wrote``, ``failed``,
    ``missing``, ``adjudication``, ``error_count``, ``errors``, ``usage``,
    ``effective`` -- or ``{"fatal": ...}`` when the wave never ran or its
    commit failed outright.
    """
    from src.judges import subagent

    emit("phase", phase="prepare", message="Preparing prompts…")
    # Re-prepared inside the job (idempotent, no spend) so the manifest the
    # wave runs from is this request's, not whatever an estimate left behind.
    prep = subagent.prepare(
        project_dir, judge_names, scopes, context=context,
        cli=cli, worker_model=worker_model, effort=effort, keep_drafts=keep_drafts,
    )
    entries = len(prep.get("manifest") or [])
    resumed = prep.get("resumed_drafts") or 0
    emit(
        "phase", phase="fanout",
        message=f"Running {entries - resumed} CLI job(s)"
        + (f" ({resumed} resumed from drafts)" if resumed else "")
        + (f", {concurrency} at a time" if concurrency else "") + "…",
        total=entries,
    )
    out = subagent.fanout(
        project_dir,
        cache=prompt_cache,
        concurrency=concurrency,
        on_job_done=lambda rec: emit(
            "target_done",
            target_id=rec.get("id"),
            index=rec.get("done"),
            total=rec.get("total"),
            ok=rec.get("ok"),
        ),
    )
    if out.get("error"):
        # A launcher error (missing binary, failed preflight) means no job
        # ever ran; report it as the stop it is rather than as 0 of N done.
        return {"fatal": out["error"], "effective": out.get("effective")}

    cli_name = out.get("cli") or default_cli
    emit("phase", phase="commit", message="Committing verdicts…")
    landed = subagent.commit(
        project_dir, persist=True, backend=f"headless:{cli_name}"
    )
    if landed.get("status") == "error":
        # An unreadable/absent manifest returns no `counts`, so the summary
        # below would read `ran: 0, error_count: 0` and the modal would
        # render "0 of N done" for a commit that never happened. Same shape
        # as the launcher-error branch above, which the JS shows as "Stopped".
        return {
            "fatal": f"commit failed: {landed.get('error') or 'unknown error'}",
            "effective": out.get("effective"),
        }

    errors = [f"{f.get('id')}: {f.get('error')}" for f in out.get("failed") or []]
    errors += [
        f"{f.get('target_id')}/{f.get('judge')}: {f.get('problem')}"
        for f in landed.get("failed") or []
    ]
    # A draft the wave never wrote is a failure of this run, not an absence
    # worth staying quiet about: without it, an all-missing commit reports
    # zero errors and reads as a clean wave that simply judged nothing.
    missing = landed.get("missing") or []
    errors += [
        f"{m.get('target_id')}/{m.get('judge')}: draft missing" for m in missing
    ]
    errors += list(landed.get("persist_errors") or [])
    counts = landed.get("counts") or {}

    adjudication = None
    if "editorial" in judge_names:
        adjudication, adj_errors = adjudicate_editorial(
            project_dir, adjudicate_scopes if adjudicate_scopes is not None else scopes,
            emit, backend="headless", context=context,
            cli=cli, worker_model=worker_model, effort=effort,
            prompt_cache=prompt_cache or "auto", concurrency=concurrency,
            keep_drafts=keep_drafts,
        )
        errors += adj_errors

    return {
        "ran": counts.get("committed", 0),
        "total": entries or total_hint,
        "wrote": len(out.get("wrote") or []),
        "resumed": resumed,
        "failed": len(out.get("failed") or []),
        "missing": len(missing),
        "adjudication": adjudication,
        "error_count": len(errors),
        "errors": errors[:5],
        # The wave's *measured* overhead_ratio, not the projection — the
        # completion line is the one place the estimate can be checked.
        "usage": out.get("usage"),
        "effective": out.get("effective"),
    }
