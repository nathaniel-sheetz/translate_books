"""Per-job usage telemetry for headless CLI waves.

**A diagnostic, deliberately isolated.** The 2026-07-30 stormy-misty friction log
measured an 8-job judge wave and found ~59% of its input tokens were fixed
per-process overhead — ~9,100 tokens for a job that did nothing — and that the
framework had *no path* by which an orchestrator could notice: ``_build_cmd``
asked for ``--output-format text``, which discards the ``usage`` block that
reports all of it.

This module owns every number that answers "what did that wave actually cost".
It is scoped so it can be removed in one commit: delete this file, drop the
``usage_log=`` kwarg from :func:`~src.harness.headless.run_headless_wave`, and
drop the ``usage`` key from the four fan-out payloads. The ``--output-format
json`` switch and the argv reductions it justifies are the *fix* and stay behind.

Two outputs, split by who pays for them:

- :func:`rollup` — ~10 numbers per wave, returned to the orchestrator (and thence
  into its context). Cheap enough to always show.
- :func:`append_usage` — one JSONL row per job, written beside the drafts and
  never read into context. This is the corpus: it accumulates across runs, records
  the argv variable under test (``flags``) and whether the job was the cache
  warm-up, so an A/B is a query over the log rather than a bespoke harness.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.harness.model_ids import model_family

# Same ~4 chars/token estimate as ``src.judges.llm_io.estimate_tokens``. Copied
# rather than imported: the harness layer must not depend on the judges layer,
# and this file is meant to be deletable without unpicking an import graph.
_CHARS_PER_TOKEN = 4

# Per-job fixed overhead assumed until this machine has measured its own, **per
# CLI family**. One number for both was wrong by ~4.4x on the Cursor path.
#
# claude — from a 2026-07-30 real judge job (6,700-token prompt,
# `--system-prompt-file`, CLI 2.1.220): 10,554 billed input against 6,700 sent =
# 3,854 of fixed context. Deliberately NOT the 9,067 the friction log's no-op
# probe reported: that probe passed no `--system-prompt-file`, so it measured the
# CLI's *default* system prompt, which a judge job replaces with the judge
# preamble.
#
# cursor — from the 2026-08-10 probe (cursor-agent 2026.08.04-aaa8809): a no-op
# job billed ~18.2k input, and a 4.5k-token prompt billed 21.8k total, putting
# the fixed per-process prefix at ~17.2k. There is no client-side lever on it:
# `cursor-agent` has no `--system-prompt-file` and no cache-TTL knob.
#
# The prefix depends on the *model*, not only the CLI. The 2026-09-14 panel probe
# (cursor-agent 2026.09.10) measured Grok 4.6 ~17.9k, Gemini 3.8 Flash ~16.3k,
# GPT-5.6 Terra ~16.3k and Claude Sonnet 5 ~30.6k. So this constant fits the
# non-Claude models and would quote a Claude-on-Cursor wave ~13k low per job.
#
# Only load-bearing on a cold machine: three logged jobs of the wave's own
# model (in this book or another) and baseline_tokens() switches to that
# model's measured median; three of any model on the CLI and it switches to
# theirs, labelled as borrowed.
DEFAULT_BASELINE_TOKENS: dict[str, int] = {
    "claude": 3900,
    "cursor": 17200,
}
_BASELINE_PROVENANCE: dict[str, str] = {
    "claude": "2026-07-30 baseline probe",
    "cursor": "2026-08-10 Cursor probe",
}
# What an un-threaded caller (``cli=None``) gets. Claude is the default headless
# family, and under-quoting the Claude path is the smaller error.
_BASELINE_FALLBACK_CLI = "claude"

# Rows :func:`baseline_tokens` reads back, and the minimum it needs before it
# prefers measurement over the default.
_BASELINE_WINDOW = 40
_BASELINE_MIN_ROWS = 3

# Token fields, in the order they are reported.
_TOKEN_FIELDS = ("input", "output", "cache_creation", "cache_read")


def approx_tokens(text: str | None) -> int:
    """Rough token count for a prompt we are about to send (0 for empty)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _first_int(source: Mapping[str, Any], *names: str) -> int | None:
    """First of ``names`` present in ``source`` as an int.

    The CLI reports the same quantity as ``cache_read_input_tokens`` at the top
    of ``usage`` and as ``cacheReadInputTokens`` inside ``modelUsage``; accepting
    both spellings keeps this parser from silently zeroing out on a rename.
    """
    for name in names:
        value = source.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _side_calls(model_usage: Any, model: str | None) -> dict[str, int]:
    """Tokens billed to models other than the one the wave asked for.

    Every headless job fires an extra Haiku call unrelated to the task (523 in /
    12 out in the baseline probe). ``modelUsage`` is keyed by full model id while
    ``--model`` is usually an alias, so "ours" is decided by substring — a
    misfire only mislabels a line in the log, since the wave totals come from the
    top-level ``usage`` block either way.
    """
    if not isinstance(model_usage, Mapping):
        return {}
    alias = (model or "").strip().lower()
    out: dict[str, int] = {}
    for key, entry in model_usage.items():
        if not isinstance(entry, Mapping):
            continue
        if alias and alias in str(key).lower():
            continue
        total = 0
        for names in (
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("cacheCreationInputTokens", "cache_creation_input_tokens"),
            ("cacheReadInputTokens", "cache_read_input_tokens"),
        ):
            total += _first_int(entry, *names) or 0
        if total:
            out[str(key)] = total
    return out


def usage_from_envelope(obj: Any, *, model: str | None = None) -> dict[str, Any] | None:
    """Pull the reportable numbers out of a ``--output-format json`` envelope.

    Returns ``None`` when the envelope carries no ``usage`` block. **Never
    raises**: telemetry that can fail a wave is worse than no telemetry, so every
    field is optional and anything unrecognised is dropped.

    Handles both CLI families. ``cursor-agent`` spells its cache fields
    ``cacheReadTokens`` / ``cacheWriteTokens`` (verified 2026-08-10 on
    2026.08.04-aaa8809) rather than Claude's ``cache_read_input_tokens``; the
    ``inputTokens`` / ``outputTokens`` spellings it shares were already accepted.

    Semantics are the same across both, which is what lets one ``rollup`` price
    either: Cursor's ``inputTokens`` **excludes** cache reads (repeat probe run 2:
    20,105 + 1,664 = 21,769 = run 1's total), so ``input + cache_creation +
    cache_read`` is billed input on both families and ``overhead_ratio`` needs no
    per-CLI arithmetic.

    Some Cursor models put the prefix in a different field. GPT-5.6 Terra
    reported ~16.7k ``cacheWriteTokens`` against ``inputTokens`` of 3 on
    2026-09-14, and the same sum still counts it. Cursor envelopes carry no cost,
    so ``cost_usd`` is absent and a Cursor wave's ``cost_equiv_usd`` is 0.
    """
    if not isinstance(obj, Mapping):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, Mapping):
        return None

    out: dict[str, Any] = {}
    for field, names in (
        ("input", ("input_tokens", "inputTokens")),
        ("output", ("output_tokens", "outputTokens")),
        (
            "cache_creation",
            ("cache_creation_input_tokens", "cacheCreationInputTokens", "cacheWriteTokens"),
        ),
        (
            "cache_read",
            ("cache_read_input_tokens", "cacheReadInputTokens", "cacheReadTokens"),
        ),
    ):
        value = _first_int(usage, *names)
        if value is not None:
            out[field] = value
    if not out:
        return None

    cost = _first_number(obj, "total_cost_usd", "costUSD")
    if cost is not None:
        out["cost_usd"] = round(cost, 6)
    duration = _first_int(obj, "duration_ms", "durationMs")
    if duration is not None:
        out["duration_ms"] = duration
    turns = _first_int(obj, "num_turns", "numTurns")
    if turns is not None:
        out["num_turns"] = turns
    side = _side_calls(obj.get("modelUsage"), model)
    if side:
        out["side_calls"] = side
    # Written only when the envelope names one, so every row this corpus already
    # holds keeps its shape (the same rule ``timed_out`` follows in job_record).
    resolved = _resolved_model(obj.get("modelUsage"), model)
    if resolved:
        out["resolved_model"] = resolved
    return out


def _resolved_model(model_usage: Any, model: str | None) -> str | None:
    """The full model id the CLI actually ran ``model`` as, if it reports one.

    ``modelUsage`` is keyed by full id while ``--model`` is usually an alias, so
    this is the key :func:`_side_calls` treats as "ours" — the busiest one when
    an alias matches several. ``None`` for a Cursor envelope (no ``modelUsage``)
    and for an id the envelope does not echo.
    """
    if not isinstance(model_usage, Mapping):
        return None
    alias = (model or "").strip().lower()
    if not alias:
        return None
    best: tuple[int, str] | None = None
    for key, entry in model_usage.items():
        if alias not in str(key).lower() or not isinstance(entry, Mapping):
            continue
        total = sum(
            _first_int(entry, *names) or 0
            for names in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheCreationInputTokens", "cache_creation_input_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"),
            )
        )
        if best is None or total > best[0]:
            best = (total, str(key))
    return best[1] if best else None


def _first_number(source: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = source.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def job_record(
    *,
    job_id: str,
    cli: str,
    model: str,
    prompt_sent: int,
    wall_s: float,
    rc: int,
    flags: Sequence[str] = (),
    warm: bool = False,
    usage: Mapping[str, Any] | None = None,
    error: str | None = None,
    effort: str | None = None,
    cache: str | None = None,
    timed_out: bool = False,
) -> dict[str, Any]:
    """One JSONL row: what we sent, what was billed, and under which argv.

    ``cache`` is the prompt-cache mode we *requested* of the CLI (``5m`` /
    ``1h`` / ``off``, or ``None`` on Cursor). An account in overage is silently
    downgraded to the 5-minute TTL, so rows are only comparable within the same
    account state — do not infer the TTL from billed rates.

    ``timed_out`` is written **only when true**, so the shape of every row this
    corpus already holds is unchanged — ``usage.jsonl`` is an A/B corpus and a
    silently re-shaped row is a corrupted one. It makes a killed job queryable
    instead of inferable from ``rc``.
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "id": job_id,
        "cli": cli,
        "model": model,
        "flags": list(flags),
        "effort": effort,
        "cache": cache,
        "warm": warm,
        "wall_s": round(wall_s, 2),
        "rc": rc,
        "prompt_sent": prompt_sent,
    }
    if usage:
        record.update(usage)
    if timed_out:
        record["timed_out"] = True
    if error:
        record["error"] = error[:300]
    return record


def append_usage(path: Path | str | None, record: Mapping[str, Any]) -> None:
    """Append one row. Best effort — a telemetry failure must never fail a wave."""
    if path is None:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _billed_input(record: Mapping[str, Any]) -> int:
    return sum(int(record.get(field) or 0) for field in ("input", "cache_creation", "cache_read"))


def _has_tokens(record: Mapping[str, Any]) -> bool:
    return any(isinstance(record.get(field), int) for field in _TOKEN_FIELDS)


def rollup(records: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Wave summary, or ``None`` when no job reported token usage.

    ``None`` is the honest answer for a stubbed test runner or a CLI build that
    ignored ``--output-format json``, and keeps the ``usage`` key out of payloads
    that have nothing to put in it. Both CLI families report usage now — Cursor
    stopped being a permanent ``None`` when its profile moved off
    ``--output-format text``.

    ``overhead`` is billed input minus the prompt we meant to send, and
    ``overhead_ratio`` is that as a share of billed input — the one number that
    makes this class of waste self-reporting on every future run.
    """
    rows = [r for r in records if _has_tokens(r)]
    if not rows:
        return None

    totals = {field: sum(int(r.get(field) or 0) for r in rows) for field in _TOKEN_FIELDS}
    prompt_sent = sum(int(r.get("prompt_sent") or 0) for r in rows)
    billed = totals["input"] + totals["cache_creation"] + totals["cache_read"]
    overhead = max(0, billed - prompt_sent)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in rows)

    out: dict[str, Any] = {
        "jobs": len(rows),
        **totals,
        "prompt_sent": prompt_sent,
        "overhead": overhead,
        "overhead_ratio": round(overhead / billed, 3) if billed else None,
        "cost_equiv_usd": round(cost, 4),
    }
    # Wave-level mode: every job in a wave shares one requested cache setting.
    if "cache" in rows[0]:
        out["cache"] = rows[0].get("cache")
    side_total: dict[str, int] = {}
    for row in rows:
        for model, tokens in (row.get("side_calls") or {}).items():
            side_total[model] = side_total.get(model, 0) + int(tokens or 0)
    if side_total:
        out["side_calls"] = side_total
    return out


def read_recent(
    path: Path | str | None, limit: int | None = _BASELINE_WINDOW
) -> list[dict[str, Any]]:
    """Last ``limit`` parseable rows of a usage log (newest last); [] if absent.

    ``limit=None`` reads the whole log, for a caller that has to filter before
    it windows (see :func:`_model_rows`).
    """
    if path is None:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    for line in lines if limit is None else lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


# Whole-log reads, keyed by path and invalidated by (mtime, size). One resolved
# profile asks four per-model questions of this book's log and of every sibling
# book's, and a library of twenty books would otherwise re-parse each of them
# four times per `status` call. Rows are shared, so readers must not mutate them.
_ROWS_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def _all_rows(path: Path | str | None) -> list[dict[str, Any]]:
    """Every parseable row of a usage log, re-read only when the file changed."""
    if path is None:
        return []
    try:
        stat = Path(path).stat()
    except OSError:
        return []
    key, stamp = str(path), (stat.st_mtime_ns, stat.st_size)
    cached = _ROWS_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    rows = read_recent(path, limit=None)
    _ROWS_CACHE[key] = (stamp, rows)
    return rows


def _model_rows(
    paths: Iterable[Path | str | None],
    *,
    cli: str | None,
    family: str,
    limit: int = _BASELINE_WINDOW,
) -> list[dict[str, Any]]:
    """The last ``limit`` rows across ``paths`` that ran ``family`` on ``cli``.

    Filtered *before* it is windowed, unlike :func:`read_recent`: a book that ran
    twenty jobs on one model and then a hundred on another still has those twenty
    rows, and they are the only measurement of that model there is.

    A row belongs to ``family`` by the id it was *asked* for or by the id the CLI
    says it *ran as* (``resolved_model``), so an alias and the full id behind it
    are one history: 564 ``sonnet`` jobs that ran as ``claude-sonnet-5`` are a
    measurement of ``claude-sonnet-5`` whichever of the two a book pins.

    And an alias is only its newest meaning. Once a row shows ``sonnet`` running
    as a different id, the rows that say they ran as the old one are dropped:
    they measured a model this alias no longer names. Rows from before
    ``resolved_model`` was logged cannot be told apart and are kept; they age
    out of the window as new rows arrive.
    """
    wanted = (cli or "").strip().lower() or None
    family = _snapshot_free(family)
    rows: list[dict[str, Any]] = []
    for path in paths:
        for row in _all_rows(path):
            if wanted and str(row.get("cli") or "").strip().lower() != wanted:
                continue
            if family in (_asked_for(row), _ran_as(row)):
                rows.append(row)
    # Rows from several books interleave by timestamp; a stable sort keeps one
    # log's own order where the stamps tie or are missing.
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    current = next(
        (
            _ran_as(row)
            for row in reversed(rows)
            if _asked_for(row) == family and _ran_as(row)
        ),
        "",
    )
    if current:
        rows = [
            row
            for row in rows
            if _asked_for(row) != family or _ran_as(row) in ("", current)
        ]
    return rows[-limit:]


# A dated snapshot (``claude-haiku-4-5-20251001``) is the model its undated id
# names. Only for matching a logged ``resolved_model`` against a requested id;
# :func:`~src.harness.model_ids.model_family` itself stays a pure knob-stripper.
_SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{8}$")


def _snapshot_free(family: str) -> str:
    return _SNAPSHOT_SUFFIX_RE.sub("", family)


def _asked_for(row: Mapping[str, Any]) -> str:
    """The family of the id a row's job was launched with."""
    return _snapshot_free(model_family(str(row.get("model") or "")))


def _ran_as(row: Mapping[str, Any]) -> str:
    """The family of the id the CLI reported running, or ``""`` if it did not say."""
    return _snapshot_free(model_family(str(row.get("resolved_model") or "")))


def _overheads(rows: Iterable[Mapping[str, Any]]) -> list[int]:
    """Billed input beyond the prompt, for each successful job that reported any."""
    return [
        max(0, _billed_input(row) - int(row.get("prompt_sent") or 0))
        for row in rows
        if _has_tokens(row) and row.get("rc") == 0
    ]


def default_baseline_tokens(cli: str | None = None) -> tuple[int, str]:
    """``(tokens, provenance)`` assumed for ``cli`` before anything is measured."""
    key = (cli or _BASELINE_FALLBACK_CLI).strip().lower()
    if key not in DEFAULT_BASELINE_TOKENS:
        key = _BASELINE_FALLBACK_CLI
    return DEFAULT_BASELINE_TOKENS[key], _BASELINE_PROVENANCE[key]


def baseline_tokens(
    path: Path | str | None,
    default: int | None = None,
    *,
    cli: str | None = None,
    model: str | None = None,
    sibling_logs: Iterable[Path | str] = (),
) -> tuple[int, str]:
    """``(per_job_overhead, provenance)`` for the pre-spawn estimate.

    Median rather than mean, so one pathological job does not move the number the
    usage gate quotes. Falls back to :data:`DEFAULT_BASELINE_TOKENS` until enough
    real jobs have been logged — which is what makes the estimate self-calibrate
    instead of trusting a constant forever.

    ``cli`` restricts both halves to one CLI family, and is **required for a
    correct number on any project that has run both**. One ``usage.jsonl`` holds
    every family's rows, and the families are ~4.4x apart on fixed overhead, so a
    median over the mixture describes neither: a Cursor wave following three
    Claude ones would quote ~3.9k against a real ~17.2k. Rows with no ``cli`` key
    are excluded when filtering — unknown provenance cannot calibrate a family.

    ``model`` narrows it again, to the model a wave will actually run. The fixed
    prefix depends on the model and not only the CLI (Claude Sonnet 5 on Cursor
    is ~30.6k against ~17.9k for Grok 4.6), so the first tier that holds
    :data:`_BASELINE_MIN_ROWS` answers, and the provenance says which it was:

    1. this log, this model
    2. this log plus ``sibling_logs`` (the same wave type in other books), this
       model — so a model already measured elsewhere is not quoted off a constant
       the first time a new book uses it
    3. this log, every model on ``cli`` — the pre-``model`` behaviour, now
       labelled as a different model's number rather than passed off as this one's
    4. the per-CLI constant

    Nothing here names a model: a new release calibrates itself after three jobs.

    ``default`` overrides the per-CLI constant (kept for callers that already
    hold a measurement); ``None`` means use the constant for ``cli``.
    """
    fallback, provenance = default_baseline_tokens(cli)
    if default is not None:
        fallback, provenance = int(default), "caller-supplied"

    wanted = (cli or "").strip().lower() or None
    scope = f" {wanted}" if wanted else ""
    family = model_family(model)
    if family:
        for paths, where in (
            ((path,), ""),
            ((path, *sibling_logs), " across books"),
        ):
            own = _overheads(_model_rows(paths, cli=wanted, family=family))
            if len(own) >= _BASELINE_MIN_ROWS:
                return int(statistics.median(own)), (
                    f"measured: median of {len(own)} logged{scope} {family} "
                    f"jobs{where}"
                )

    rows = read_recent(path)
    if wanted is not None:
        rows = [r for r in rows if str(r.get("cli") or "").strip().lower() == wanted]
    overheads = _overheads(rows)
    if len(overheads) < _BASELINE_MIN_ROWS:
        return fallback, f"default: {fallback} ({provenance})"
    measured = int(statistics.median(overheads))
    note = f" including other models (too few {family} rows yet)" if family else ""
    return measured, f"measured: median of {len(overheads)} logged{scope} jobs{note}"


def output_ratio(
    path: Path | str | None,
    *,
    cli: str | None = None,
    model: str | None = None,
    sibling_logs: Iterable[Path | str] = (),
) -> tuple[float | None, str]:
    """``(output tokens per prompt token, provenance)`` for ``model``, if measured.

    The input side of a quote is roughly a property of the CLI; the output side
    is a property of the model. Grok 4.7 at medium returned ~1 output token per
    input token where the gate had projected none (440k unquoted, 2026-09-28),
    because the estimate was input-only and the only history was another model's.

    So unlike :func:`baseline_tokens` this **never borrows another model's
    number**: with fewer than :data:`_BASELINE_MIN_ROWS` rows for ``model`` — in
    this log, then across ``sibling_logs`` — it returns ``None`` and says there
    is no data, which is the honest quote for a model nobody has run yet.
    """
    family = model_family(model)
    if not family:
        return None, "no worker model to calibrate output on"
    wanted = (cli or "").strip().lower() or None
    have = 0
    for paths, where in (
        ((path,), ""),
        ((path, *sibling_logs), " across books"),
    ):
        ratios = [
            int(row["output"]) / int(row["prompt_sent"])
            for row in _model_rows(paths, cli=wanted, family=family)
            if row.get("rc") == 0
            and isinstance(row.get("output"), int)
            and isinstance(row.get("prompt_sent"), int)
            and row["prompt_sent"] > 0
        ]
        if len(ratios) >= _BASELINE_MIN_ROWS:
            return round(statistics.median(ratios), 3), (
                f"measured: median output/prompt of {len(ratios)} logged "
                f"{family} jobs{where}"
            )
        have = len(ratios)
    if have:
        plural = "s" if have > 1 else ""
        return None, (
            f"only {have} output row{plural} for {family} yet "
            f"({_BASELINE_MIN_ROWS} needed)"
        )
    return None, f"no output rows for {family} yet"


def estimate_output_tokens(prompt_tokens: int, ratio: float | None) -> int | None:
    """Projected output for a wave, or ``None`` when its model is unmeasured.

    ``None`` rather than 0 on purpose: a gate that prints 0 reads as "this wave
    writes nothing", and a missing number has to look missing.
    """
    if ratio is None:
        return None
    return int(round(max(0, int(prompt_tokens)) * ratio))


def model_seen(
    path: Path | str | None,
    *,
    cli: str | None = None,
    model: str | None = None,
    sibling_logs: Iterable[Path | str] = (),
) -> bool:
    """True when a successful job on ``model`` is logged here or in a sibling book."""
    family = model_family(model)
    if not family:
        return False
    wanted = (cli or "").strip().lower() or None
    return any(
        row.get("rc") == 0
        for row in _model_rows((path, *sibling_logs), cli=wanted, family=family)
    )


def last_resolved_model(
    path: Path | str | None,
    *,
    cli: str | None = None,
    model: str | None = None,
    sibling_logs: Iterable[Path | str] = (),
) -> str | None:
    """The full model id ``model`` most recently ran as, from the log, or ``None``.

    A tier alias is resolved by the CLI, not by this repo, and it lags a release:
    on 2026-09-29 ``sonnet`` still meant ``claude-sonnet-5`` with Sonnet 5.5
    available, and nothing at the gate said so. The envelope does say which id
    answered (see :func:`usage_from_envelope`), so the last logged answer is the
    best available evidence of what the alias will mean this time. Evidence, not
    a promise — the CLI can move the alias between two waves.
    """
    family = model_family(model)
    if not family:
        return None
    wanted = (cli or "").strip().lower() or None
    for row in reversed(_model_rows((path, *sibling_logs), cli=wanted, family=family)):
        resolved = row.get("resolved_model")
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
    return None


def median_wall_s(
    path: Path | str | None, *, cli: str | None = None
) -> float | None:
    """Median ``wall_s`` of recent successful jobs, or ``None`` with no history.

    Feeds the prompt-cache auto picker: a warm-up that routinely runs past ~270 s
    risks expiring a 5-minute TTL entry before any follower can read it.

    ``cli`` restricts to one family, matching :func:`baseline_tokens` — Cursor
    and Claude wall times live in the same ``usage.jsonl`` and must not mix when
    picking a Claude cache TTL.
    """
    wanted = (cli or "").strip().lower() or None
    rows = read_recent(path)
    if wanted is not None:
        rows = [r for r in rows if str(r.get("cli") or "").strip().lower() == wanted]
    walls = [
        float(row["wall_s"])
        for row in rows
        if row.get("rc") == 0 and isinstance(row.get("wall_s"), (int, float))
    ]
    if not walls:
        return None
    return float(statistics.median(walls))
