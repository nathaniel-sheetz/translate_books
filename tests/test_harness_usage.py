"""Unit tests for the headless usage telemetry (``src/harness/usage.py``).

The module's contract is narrow and defensive: it must extract what it can from a
CLI envelope, and **never** raise — a measurement that can fail a wave is worse
than no measurement. Most of these tests are therefore about malformed input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.harness import usage


def test_approx_tokens():
    assert usage.approx_tokens("") == 0
    assert usage.approx_tokens(None) == 0
    assert usage.approx_tokens("x" * 400) == 100
    assert usage.approx_tokens("ab") == 1  # floored, never zero for real text


def test_usage_from_envelope_reads_both_key_spellings():
    snake = {"usage": {"input_tokens": 1, "cache_read_input_tokens": 2}}
    camel = {"usage": {"inputTokens": 1, "cacheReadInputTokens": 2}}
    assert usage.usage_from_envelope(snake) == usage.usage_from_envelope(camel)


def test_usage_from_envelope_survives_garbage():
    """Never raise: every field optional, anything unrecognised dropped."""
    assert usage.usage_from_envelope(None) is None
    assert usage.usage_from_envelope("not a dict") is None
    assert usage.usage_from_envelope({}) is None
    assert usage.usage_from_envelope({"usage": "not a mapping"}) is None
    assert usage.usage_from_envelope({"usage": {}}) is None
    assert usage.usage_from_envelope({"usage": {"input_tokens": "many"}}) is None
    # Booleans are ints in Python; they are not token counts.
    assert usage.usage_from_envelope({"usage": {"input_tokens": True}}) is None
    # Partial data is still data.
    assert usage.usage_from_envelope({"usage": {"output_tokens": 7}}) == {"output": 7}


def test_side_calls_attribute_away_from_the_requested_model():
    envelope = {
        "usage": {"input_tokens": 1},
        "modelUsage": {
            "claude-sonnet-4-5-20250929": {"inputTokens": 100},
            "claude-haiku-4-5-20251001": {"inputTokens": 523, "outputTokens": 12},
            "junk": "not a mapping",
        },
    }
    out = usage.usage_from_envelope(envelope, model="sonnet")
    assert out["side_calls"] == {"claude-haiku-4-5-20251001": 535}
    # With no model to compare against, nothing can be called a side call... but
    # everything is still reported rather than silently dropped.
    out = usage.usage_from_envelope(envelope, model=None)
    assert set(out["side_calls"]) == {"claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"}


def test_rollup_is_none_without_token_data():
    """Cursor waves and stub runners report nothing; say so rather than zero."""
    assert usage.rollup([]) is None
    assert usage.rollup([{"id": "a", "wall_s": 1.0, "prompt_sent": 50}]) is None


def test_rollup_totals_and_overhead_ratio():
    rows = [
        {"input": 10, "output": 20, "cache_creation": 5000, "cache_read": 3000,
         "prompt_sent": 1000, "cost_usd": 0.03},
        {"input": 10, "output": 20, "cache_creation": 0, "cache_read": 8000,
         "prompt_sent": 1000, "cost_usd": 0.01},
    ]
    out = usage.rollup(rows)
    assert out["jobs"] == 2
    assert out["cache_creation"] == 5000
    assert out["cache_read"] == 11_000
    billed = 20 + 5000 + 11_000
    assert out["overhead"] == billed - 2000
    assert out["overhead_ratio"] == round((billed - 2000) / billed, 3)
    assert out["cost_equiv_usd"] == 0.04


def test_rollup_never_reports_negative_overhead():
    """A prompt estimate that overshoots what was billed is not free money."""
    out = usage.rollup([{"input": 10, "cache_read": 0, "prompt_sent": 99_999}])
    assert out["overhead"] == 0
    assert out["overhead_ratio"] == 0.0


def test_append_and_read_usage_roundtrip(tmp_path: Path):
    log = tmp_path / "nested" / "usage.jsonl"
    for i in range(3):
        usage.append_usage(log, usage.job_record(
            job_id=f"c{i}", cli="claude", model="sonnet",
            prompt_sent=100, wall_s=1.5, rc=0, usage={"input": 5},
        ))
    rows = usage.read_recent(log)
    assert [r["id"] for r in rows] == ["c0", "c1", "c2"]
    assert rows[0]["input"] == 5


def test_append_usage_swallows_write_failures(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    usage.append_usage(blocker / "usage.jsonl", {"id": "c0"})  # must not raise
    usage.append_usage(None, {"id": "c0"})
    # Unserializable payloads are dropped rather than raised.
    usage.append_usage(tmp_path / "u.jsonl", {"bad": object()})


def test_read_recent_skips_corrupt_lines(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    log.write_text('{"id": "a"}\nnot json\n\n["not a dict"]\n{"id": "b"}\n', encoding="utf-8")
    assert [r["id"] for r in usage.read_recent(log)] == ["a", "b"]
    assert usage.read_recent(tmp_path / "absent.jsonl") == []
    assert usage.read_recent(None) == []


def test_baseline_falls_back_until_enough_measurements(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    value, source = usage.baseline_tokens(log)
    assert value == usage.DEFAULT_BASELINE_TOKENS
    assert source.startswith("default:")

    # Two rows is not yet enough to prefer measurement over the documented default.
    for _ in range(2):
        usage.append_usage(log, {"input": 0, "cache_creation": 9000, "cache_read": 0,
                                 "prompt_sent": 0, "rc": 0})
    assert usage.baseline_tokens(log)[1].startswith("default:")

    usage.append_usage(log, {"input": 0, "cache_creation": 9000, "cache_read": 0,
                             "prompt_sent": 0, "rc": 0})
    value, source = usage.baseline_tokens(log)
    assert value == 9000
    assert source.startswith("measured:")


def test_baseline_ignores_failed_jobs(tmp_path: Path):
    """A job that died before doing work is not evidence about per-job overhead."""
    log = tmp_path / "usage.jsonl"
    for _ in range(3):
        usage.append_usage(log, {"input": 0, "cache_creation": 7000, "cache_read": 0,
                                 "prompt_sent": 0, "rc": 0})
    for _ in range(5):
        usage.append_usage(log, {"input": 0, "cache_creation": 10, "cache_read": 0,
                                 "prompt_sent": 0, "rc": 1})
    assert usage.baseline_tokens(log)[0] == 7000


def test_job_record_shape():
    record = usage.job_record(
        job_id="c0", cli="claude", model="sonnet", prompt_sent=100,
        wall_s=1.234, rc=0, flags=["--strict-mcp-config"], warm=True,
        usage={"input": 5, "cache_read": 9}, error=None,
    )
    assert record["flags"] == ["--strict-mcp-config"]
    assert record["warm"] is True
    assert record["wall_s"] == 1.23
    assert record["input"] == 5 and record["cache_read"] == 9
    assert "error" not in record
    # Round-trips as one JSONL line.
    assert json.loads(json.dumps(record))["id"] == "c0"


def test_headless_extra_flags_accepts_list_or_string():
    """`config-set --value` can only deliver a string; a key that silently did
    nothing when set the documented way would be worse than no key."""
    from src.harness import state

    assert state.headless_extra_flags({"headless_extra_flags": ["--effort", "low"]}) == [
        "--effort", "low",
    ]
    assert state.headless_extra_flags({"headless_extra_flags": "--effort low"}) == [
        "--effort", "low",
    ]
    # Anything else runs today's argv rather than taking the wave down.
    assert state.headless_extra_flags({}) == []
    assert state.headless_extra_flags({"headless_extra_flags": None}) == []
    assert state.headless_extra_flags({"headless_extra_flags": 42}) == []
    assert state.headless_extra_flags({"headless_extra_flags": ["--ok", {"bad": 1}]}) == ["--ok"]
