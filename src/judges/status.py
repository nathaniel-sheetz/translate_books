"""Judge-coverage status: which chapters have a current verdict, and which don't.

The question this answers — *what has actually been judged?* — used to require an
ad-hoc Python walk over ``evaluations/*.json`` at the start of every judge-review
session, and got answered wrong when it wasn't: an evaluation **file** exists as
soon as the deterministic evaluators run, so a chapter with ``judges: {}`` reads
as "already evaluated" while carrying no LLM verdict at all. (2026-08-11 fabre2:
eight of ten chapters in scope were deterministic-only, and the gap was found by
hand.)

Nothing here is new machinery. The dashboard's Review tab already computes this
from the ``eval_runs`` content-hash ledger in :mod:`web_ui.evaluations`; this
module composes the same primitives — :func:`~web_ui.evaluations.iter_chapter_chunks`,
:func:`~web_ui.evaluations.chapter_chunk_states`,
:func:`~web_ui.evaluations.rollup_group_state` — into a payload shaped for an
agent rather than for a table of pips, and adds the provenance
(``executed_at`` / ``worker_model`` / ``backend``) and the freshness *basis* the
route drops. The two share a chapter universe and a state machine by
construction, so the CLI and the dashboard cannot disagree.

Read-only: opens chunks and evaluations, writes nothing, spends nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from src.judges.registry import available_judges
from web_ui.evaluations import (
    JUDGE_STATUS_GROUPS,
    chapter_chunk_states,
    iter_chapter_chunks,
    rollup_group_state,
)

# The four chapter/book verdicts, worst first. Order is the bucket order in the
# output and the priority order for the ``next`` line: a stale verdict is a
# wrong answer standing in the book, which outranks one that was never asked.
_STATES = ("stale", "partial", "not_run", "done")

# Buckets whose chapters still owe this judge work, in the order a caller should
# think about them. ``done`` is deliberately absent — that is the whole point.
_NEEDS_STATES = ("stale", "partial", "not_run")

# Staleness evidence, strongest claim first. ``flag`` (an apply stale-stamped the
# chunk) and ``hash`` (the translation's content hash moved) are proof; ``mtime``
# (a pre-``eval_runs`` file compared by timestamp) is only a suspicion.
_BASIS_RANK = ("flag", "hash", "mtime")


class StatusScopeError(ValueError):
    """Raised when a ``--scope`` filter is malformed or matches no chapter."""


def _judge_groups(judges: Optional[Iterable[str]] = None) -> dict[str, tuple[str, ...]]:
    """The status groups to report, narrowed to ``judges`` when given.

    Starts from :data:`~web_ui.evaluations.JUDGE_STATUS_GROUPS` (``coded`` plus
    one group per shipped judge) and appends a solo group for any registered
    judge that constant does not name — a judge added to the registry must not
    be invisible here just because the dashboard's cell list wasn't updated.
    """
    groups: dict[str, tuple[str, ...]] = dict(JUDGE_STATUS_GROUPS)
    for name in available_judges():
        if name not in groups:
            groups[name] = (name,)
    if judges is None:
        return groups

    wanted = list(dict.fromkeys(judges))
    unknown = [name for name in wanted if name not in groups]
    if unknown:
        raise StatusScopeError(
            f"Unknown judge(s) {', '.join(repr(u) for u in unknown)}; expected one of: "
            + ", ".join(sorted(groups))
        )
    return {name: groups[name] for name in wanted}


def _filter_chapters(chapter_ids: list[str], scopes: Optional[Iterable[str]]) -> list[str]:
    """Narrow the chapter list by ``--scope`` strings (a filter, not a resolver).

    Accepts the grammar the rest of the CLI uses — ``book``, ``chapter:<id>``,
    ``chunk:<id>`` (reported as its parent chapter) — plus the inclusive range
    form ``chapter:<first>..<last>``. The range resolves against the *enumerated*
    chapter list rather than by comparing ids, so it works on a book whose
    chapters are not zero-padded or not named ``chapter_NN``.

    Deliberately does not go through :func:`src.judges.scope.build_targets`:
    that raises when a chapter has no translated chunks, which is exactly the
    case a status report exists to show.
    """
    if not scopes:
        return chapter_ids

    known = set(chapter_ids)
    index = {cid: i for i, cid in enumerate(chapter_ids)}
    keep: list[str] = []

    for raw in scopes:
        scope = (raw or "").strip()
        if scope.lower().rstrip(":") == "book":
            return chapter_ids
        kind, sep, rest = scope.partition(":")
        kind, rest = kind.strip().lower(), rest.strip()
        if not sep or not rest:
            raise StatusScopeError(
                f"Malformed scope {raw!r}; expected 'chapter:<id>', "
                "'chapter:<first>..<last>', 'chunk:<id>' or 'book'."
            )

        if kind == "chunk":
            from web_ui.evaluations import chapter_id_from_chunk_id

            candidates = [chapter_id_from_chunk_id(rest)]
        elif kind == "chapter" and ".." in rest:
            first, _, last = rest.partition("..")
            first, last = first.strip(), last.strip()
            absent = [c for c in (first, last) if c not in known]
            if absent:
                span = (
                    f" Known chapters run {chapter_ids[0]}..{chapter_ids[-1]}."
                    if chapter_ids
                    else ""
                )
                raise StatusScopeError(
                    f"Range endpoint(s) not in this project: {', '.join(absent)}.{span}"
                )
            lo, hi = sorted((index[first], index[last]))
            candidates = chapter_ids[lo : hi + 1]
        elif kind == "chapter":
            candidates = [rest]
        else:
            raise StatusScopeError(
                f"Unknown scope kind {kind!r}; expected one of: chapter, chunk, book."
            )

        for cid in candidates:
            if cid not in known:
                raise StatusScopeError(f"No chapter {cid!r} in this project (from {raw!r}).")
            keep.append(cid)

    kept = set(keep)
    return [cid for cid in chapter_ids if cid in kept]


def _provenance(record: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """When a group last ran on one chunk, and what ran it.

    Judges keep their own ``executed_at`` and a ``metadata.worker_model`` /
    ``metadata.backend`` inside ``judges[<name>]``; the deterministic evaluators
    have only the ledger's ``eval_runs[<name>].at`` (falling back to the file's
    ``evaluated_at``). Both are read here so a caller never has to know which
    shape it is looking at.
    """
    payload = record.get("evaluation")
    if not isinstance(payload, dict):
        return {"at": None, "worker_model": None, "backend": None}

    ledger = payload.get("eval_runs")
    ledger = ledger if isinstance(ledger, dict) else {}
    judges = payload.get("judges")
    judges = judges if isinstance(judges, dict) else {}

    stamps: list[str] = []
    worker_model = backend = None
    for name in names:
        entry = judges.get(name)
        if isinstance(entry, dict):
            meta = entry.get("metadata")
            meta = meta if isinstance(meta, dict) else {}
            worker_model = worker_model or meta.get("worker_model") or meta.get("model")
            backend = backend or meta.get("backend") or (
                "api" if meta.get("provider") else None
            )
            if entry.get("executed_at"):
                stamps.append(str(entry["executed_at"]))
                continue
        led = ledger.get(name)
        if isinstance(led, dict) and led.get("at"):
            stamps.append(str(led["at"]))
        elif name in judges and payload.get("judges_at"):
            stamps.append(str(payload["judges_at"]))
        elif name not in judges and payload.get("evaluated_at"):
            stamps.append(str(payload["evaluated_at"]))

    return {
        "at": max(stamps) if stamps else None,
        "worker_model": worker_model,
        "backend": backend,
    }


def _stale_bases(record: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    """Which evidence made this chunk's group verdict stale.

    Only the members that are themselves stale are consulted: a group goes stale
    on one member, and the *fresh* members of that same group carry a basis too
    (they were compared and passed), so reading the first basis in the group
    would happily report a fresh evaluator's evidence for a stale verdict.
    """
    out: list[str] = []
    for name in names:
        entry = record["detail"].get(name) or {}
        if entry.get("state") == "stale" and entry.get("basis"):
            basis = str(entry["basis"])
            if basis not in out:
                out.append(basis)
    # Strongest claim first, so a caller counting one basis per stale chunk
    # attributes it to proof (an apply stamp, a moved hash) over suspicion.
    return sorted(out, key=_BASIS_RANK.index)


def _owed(entry: dict[str, Any]) -> list[str]:
    """The chapters a group still owes work on: stale, then partial, then never run."""
    return [cid for state in _NEEDS_STATES for cid in entry["chapters"][state]]


def build_status(
    project_dir: Path,
    *,
    judges: Optional[Iterable[str]] = None,
    scopes: Optional[Iterable[str]] = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Report per-judge coverage over a project's chapters.

    Args:
        project_dir: ``projects/<id>/`` directory.
        judges: Status groups to report (``coded``, ``dialogue``, ``address``,
            any registered judge). Defaults to all of them.
        scopes: ``--scope`` filter strings; see :func:`_filter_chapters`.
            Defaults to the whole book, which is the usual question.
        detail: Add the per-chapter ``chapters[]`` array — provenance per group,
            plus the chunk ids responsible for a ``partial`` or ``stale``
            verdict. Off by default: the bucketed lists answer "what's left?" in
            a fraction of the tokens.

    Raises:
        StatusScopeError: For an unknown judge, or a scope that is malformed or
            matches no chapter in this project.
    """
    project_dir = Path(project_dir)
    groups = _judge_groups(judges)
    # Always walk every group, report only the requested ones. Narrowing to one
    # judge must not blind the deterministic-only detection below: whether a
    # chapter is the "looks evaluated, was never judged" shape is a fact about
    # the chapter, not about which judge the caller happened to ask after.
    all_groups = _judge_groups(None)

    chunks_by_chapter = iter_chapter_chunks(project_dir)
    chapters_dir = project_dir / "chapters"
    from_txt = (
        {f.stem for f in chapters_dir.glob("chapter_*.txt")} if chapters_dir.exists() else set()
    )
    # Same universe as the dashboard's Review tab (web_ui/app.py), including a
    # chapter that exists only as chunks — chapters/*.txt is written by Combine,
    # so a chunked-but-uncombined book would otherwise report nothing.
    all_chapter_ids = sorted(from_txt | set(chunks_by_chapter))
    chapter_ids = _filter_chapters(all_chapter_ids, scopes)

    buckets: dict[str, dict[str, list[str]]] = {
        group: {state: [] for state in _STATES} for group in groups
    }
    chunk_totals: dict[str, list[str]] = {group: [] for group in groups}
    stale_basis: dict[str, dict[str, int]] = {
        group: {"flag": 0, "hash": 0, "mtime": 0} for group in groups
    }
    last_run: dict[str, Optional[str]] = {group: None for group in groups}
    worker_models: dict[str, list[str]] = {group: [] for group in groups}

    chapter_rows: list[dict[str, Any]] = []
    total_chunks = translated_chunks = 0
    # Chapters holding an evaluation file that carries no LLM verdict at all —
    # the shape that reads as "already evaluated" and is not.
    coded_only = 0

    for chapter_id in chapter_ids:
        chunks = chunks_by_chapter.get(chapter_id, [])
        records = chapter_chunk_states(project_dir, chunks, groups=all_groups)
        total_chunks += len(chunks)
        translated_chunks += len(records)

        row: dict[str, Any] = {
            "id": chapter_id,
            "chunks": len(chunks),
            "translated": len(records),
        }
        has_coded = any(rec["states"].get("coded", "missing") != "missing" for rec in records)
        has_judge = any(
            rec["states"].get(name, "missing") != "missing"
            for rec in records
            for name in all_groups
            if name != "coded"
        )

        for group, names in groups.items():
            states = [rec["states"].get(group, "missing") for rec in records]
            rollup = rollup_group_state(states)
            buckets[group][rollup["state"]].append(chapter_id)
            chunk_totals[group].extend(states)

            prov_at: list[str] = []
            models: list[str] = []
            backends: list[str] = []
            offenders: list[str] = []
            bases: list[str] = []
            reasons: list[str] = []
            for rec, state in zip(records, states):
                if state == "stale":
                    chunk_bases = _stale_bases(rec, names)
                    if chunk_bases:
                        # One tally per stale chunk, on its strongest evidence, so
                        # stale_basis always sums to chunks.stale. A ``coded``
                        # chunk can have two stale evaluators on different
                        # evidence; the detail cell below still lists both.
                        stale_basis[group][chunk_bases[0]] += 1
                    for basis in chunk_bases:
                        if basis not in bases:
                            bases.append(basis)
                    reason = (rec["evaluation"] or {}).get("stale_reason")
                    if reason and str(reason) not in reasons:
                        reasons.append(str(reason))
                if state != "missing":
                    prov = _provenance(rec, names)
                    if prov["at"]:
                        prov_at.append(prov["at"])
                    if prov["worker_model"]:
                        models.append(str(prov["worker_model"]))
                    if prov["backend"]:
                        backends.append(str(prov["backend"]))
                if state in ("stale", "missing"):
                    offenders.append(str(rec["chunk_id"]))

            if prov_at:
                newest = max(prov_at)
                if last_run[group] is None or newest > str(last_run[group]):
                    last_run[group] = newest
            for model in models:
                if model not in worker_models[group]:
                    worker_models[group].append(model)
            if detail:
                cell: dict[str, Any] = {
                    "state": rollup["state"],
                    "at": max(prov_at) if prov_at else None,
                }
                if models:
                    cell["worker_model"] = (
                        models[0] if len(set(models)) == 1 else sorted(set(models))
                    )
                if backends:
                    cell["backend"] = (
                        backends[0] if len(set(backends)) == 1 else sorted(set(backends))
                    )
                if rollup["state"] in ("stale", "partial"):
                    cell["chunk_ids"] = offenders
                if bases:
                    cell["basis"] = bases
                if reasons:
                    cell["stale_reason"] = reasons
                row[group] = cell

        # An evaluation file with coded results and no judge verdict at all: the
        # shape that reads as "already evaluated" and is the reason this command
        # exists. A chapter with nothing at all is just not_run, not a trap.
        if has_coded and not has_judge:
            coded_only += 1
        if detail:
            chapter_rows.append(row)

    judges_out: dict[str, Any] = {}
    needs: dict[str, dict[str, int]] = {}
    for group in groups:
        rollup = rollup_group_state(chunk_totals[group])
        entry: dict[str, Any] = {
            "state": rollup["state"],
            "chunks": {k: rollup[k] for k in ("fresh", "stale", "missing")},
            "chapters": {state: buckets[group][state] for state in _STATES},
            "last_run": last_run[group],
        }
        if worker_models[group]:
            entry["worker_models"] = worker_models[group]
        if rollup["stale"]:
            entry["stale_basis"] = {k: v for k, v in stale_basis[group].items() if v}
        judges_out[group] = entry

        # Counts only. The chapter ids are already in ``chapters`` above, and a
        # pre-built 53-flag --scope string would restate every one of them for a
        # command that, in practice, gets run over a hand-picked subset.
        if _owed(entry):
            needs[group] = {
                state: len(entry["chapters"][state])
                for state in _NEEDS_STATES
                if entry["chapters"][state]
            }

    warnings: list[str] = []
    if coded_only:
        warnings.append(
            f"{coded_only} chapter(s) in scope have an evaluation file holding deterministic "
            "results only (judges: {}) — an evaluations/*.json file is not a judge verdict."
        )
    mtime_stale = sum(basis.get("mtime", 0) for basis in stale_basis.values())
    if mtime_stale:
        warnings.append(
            f"{mtime_stale} stale verdict(s) were decided by file mtime, not by content hash "
            "(pre-eval_runs evaluations). That is a suspicion, not proof the text changed — a "
            "re-run of that evaluator settles it and writes the ledger."
        )

    payload: dict[str, Any] = {
        "status": "ok",
        "project": str(project_dir),
        "totals": {
            "chapters": len(all_chapter_ids),
            "chapters_in_scope": len(chapter_ids),
            "chunks": total_chunks,
            "translated": translated_chunks,
        },
        "judges": judges_out,
        "needs": needs,
        "warnings": warnings or None,
        "next": _next_line(judges_out, needs, translated_chunks),
    }
    if detail:
        payload["chapters"] = chapter_rows
    return payload


def _next_line(
    judges_out: dict[str, Any], needs: dict[str, dict[str, int]], translated: int
) -> str:
    """One sentence naming the most useful thing to run next.

    Short on purpose: it names the group and where the chapter ids are, rather
    than inlining them — they are already in ``judges[<group>].chapters``, and
    a caller almost always runs the next wave over a subset the user picked.
    """
    if not translated:
        return "nothing translated in scope yet — there is nothing to judge."
    if not needs:
        return "every judge has a current verdict for every chapter in scope; nothing owed."

    # Judges before coded (coded reruns are cheap and local; a judge wave is the
    # expensive, consent-gated thing a caller is here to plan), stale before gaps.
    def rank(group: str) -> tuple[int, int]:
        return (0 if group != "coded" else 1, _STATES.index(judges_out[group]["state"]))

    group = min(needs, key=rank)
    counts = needs[group]
    breakdown = ", ".join(f"{n} {state}" for state, n in counts.items())
    owed = sum(counts.values())
    if group == "coded":
        return (
            f"{owed} chapter(s) owe the deterministic evaluators ({breakdown}) — rerun "
            "them from the dashboard's Review tab (no spend)."
        )
    return (
        f"{owed} chapter(s) owe a {group} verdict ({breakdown}) — take the ids from "
        f"judges.{group}.chapters and run `prepare --judge {group} --scope chapter:<id> ...`"
    )
