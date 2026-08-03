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
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Same ~4 chars/token estimate as ``src.judges.llm_io.estimate_tokens``. Copied
# rather than imported: the harness layer must not depend on the judges layer,
# and this file is meant to be deletable without unpicking an import graph.
_CHARS_PER_TOKEN = 4

# Per-job fixed overhead assumed until this machine has measured its own. From a
# 2026-07-30 real judge job (6,700-token prompt, `--system-prompt-file`, CLI
# 2.1.220): 10,554 billed input against 6,700 sent = 3,854 of fixed context.
#
# Deliberately NOT the 9,067 the friction log's no-op probe reported. That probe
# passed no `--system-prompt-file`, so it measured the CLI's *default* system
# prompt; a judge job replaces that with the judge preamble. Which suggests the
# cache split is worth more than the caching it is named for — it also evicts a
# ~5k default system prompt — but that is an inference from two runs with several
# differences, not a measurement. Grouped entries take the full-prompt path with
# no override, so testing them would settle it.
#
# Only load-bearing on a cold machine: three logged jobs and baseline_tokens()
# switches to the measured median.
DEFAULT_BASELINE_TOKENS = 3900

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
        ("cache_creation", ("cache_creation_input_tokens", "cacheCreationInputTokens")),
        ("cache_read", ("cache_read_input_tokens", "cacheReadInputTokens")),
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
    return out


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
) -> dict[str, Any]:
    """One JSONL row: what we sent, what was billed, and under which argv.

    ``cache`` is the prompt-cache mode we *requested* of the CLI (``5m`` /
    ``1h`` / ``off``, or ``None`` on Cursor). An account in overage is silently
    downgraded to the 5-minute TTL, so rows are only comparable within the same
    account state — do not infer the TTL from billed rates.
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

    ``None`` is the honest answer for a Cursor wave or a stubbed test runner, and
    keeps the ``usage`` key out of payloads that have nothing to put in it.

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


def read_recent(path: Path | str | None, limit: int = _BASELINE_WINDOW) -> list[dict[str, Any]]:
    """Last ``limit`` parseable rows of a usage log (newest last); [] if absent."""
    if path is None:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
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


def baseline_tokens(
    path: Path | str | None, default: int = DEFAULT_BASELINE_TOKENS
) -> tuple[int, str]:
    """``(per_job_overhead, provenance)`` for the pre-spawn estimate.

    Median rather than mean, so one pathological job does not move the number the
    usage gate quotes. Falls back to :data:`DEFAULT_BASELINE_TOKENS` until enough
    real jobs have been logged — which is what makes the estimate self-calibrate
    instead of trusting a constant forever.
    """
    overheads = [
        max(0, _billed_input(row) - int(row.get("prompt_sent") or 0))
        for row in read_recent(path)
        if _has_tokens(row) and row.get("rc") == 0
    ]
    if len(overheads) < _BASELINE_MIN_ROWS:
        return default, f"default: {default} (2026-07-30 baseline probe)"
    measured = int(statistics.median(overheads))
    return measured, f"measured: median of {len(overheads)} logged jobs"


def median_wall_s(path: Path | str | None) -> float | None:
    """Median ``wall_s`` of recent successful jobs, or ``None`` with no history.

    Feeds the prompt-cache auto picker: a warm-up that routinely runs past ~270 s
    risks expiring a 5-minute TTL entry before any follower can read it.
    """
    walls = [
        float(row["wall_s"])
        for row in read_recent(path)
        if row.get("rc") == 0 and isinstance(row.get("wall_s"), (int, float))
    ]
    if not walls:
        return None
    return float(statistics.median(walls))
