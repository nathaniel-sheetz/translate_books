"""Unit tests for the shared headless CLI launcher (claude + cursor profiles)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.harness import headless, usage


def test_build_cmd_claude_with_system_prompt_file(tmp_path: Path):
    spf = tmp_path / "preamble.txt"
    spf.write_text("PREAMBLE\n", encoding="utf-8")
    cmd = headless._build_cmd("claude", "claude", "sonnet", str(spf))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--system-prompt-file" in cmd
    assert str(spf.resolve()) in cmd
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    # json, not text: `text` discards the `usage` block the CLI already computed,
    # which is the whole reason per-job overhead was invisible.
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--model" in cmd and "sonnet" in cmd
    assert not [f for f in cmd if f.startswith("--strict")]  # no extra_flags by default


def test_build_cmd_claude_appends_extra_flags(tmp_path: Path):
    cmd = headless._build_cmd(
        "claude", "claude", "sonnet", None, extra_flags=["--strict-mcp-config"]
    )
    assert cmd[-1] == "--strict-mcp-config"


def test_build_cmd_cursor_ignores_extra_flags():
    """Claude argv on a Cursor wave would fail every job; silently drop it."""
    cmd = headless._build_cmd(
        "cursor", "cursor-agent", "grok-4.5", None, extra_flags=["--strict-mcp-config"]
    )
    assert "--strict-mcp-config" not in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"


def test_build_cmd_cursor_has_no_system_prompt_or_tools():
    cmd = headless._build_cmd("cursor", "cursor-agent", "grok-4.5", "ignored.txt")
    assert cmd == [
        "cursor-agent",
        "-p",
        "--trust",
        "--mode",
        "ask",
        "--model",
        "grok-4.5",
        "--output-format",
        "text",
    ]
    assert "--system-prompt-file" not in cmd
    assert "--tools" not in cmd
    assert "--force" not in cmd


def test_fold_system_prompt_claude_keeps_split(tmp_path: Path):
    spf = tmp_path / "preamble.txt"
    spf.write_text("PREAMBLE\n", encoding="utf-8")
    out_spf, stdin = headless._fold_system_prompt("claude", "BODY", str(spf))
    assert out_spf == str(spf)
    assert stdin == "BODY"


def test_fold_system_prompt_cursor_folds_into_stdin(tmp_path: Path):
    spf = tmp_path / "preamble.txt"
    spf.write_text("PREAMBLE", encoding="utf-8")
    out_spf, stdin = headless._fold_system_prompt("cursor", "BODY", str(spf))
    assert out_spf is None
    assert stdin == "PREAMBLE\nBODY"


def test_extract_output_unwraps_json_result_envelope():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "  final answer  ",
        "session_id": "abc",
    })
    # Both families now: claude asks for --output-format json, and cursor still
    # gets the hardening it always had in case a run produces an envelope.
    assert headless._extract_output("cursor", raw) == "final answer"
    assert headless._extract_output("claude", raw) == "final answer"
    assert headless._extract_output("cursor", "  plain prose  ") == "plain prose"


def test_extract_result_passes_non_envelope_json_through():
    """A judge verdict is JSON but not an envelope — it must not be unwrapped.

    This is also the fallback that keeps every stubbed-runner test (and a CLI
    build that ignores --output-format json) working: no envelope, no usage,
    same draft as before.
    """
    verdict = json.dumps({"compliant": False, "findings": [], "summary": "x"})
    prose, usage = headless._extract_result("claude", verdict)
    assert json.loads(prose)["compliant"] is False
    assert usage is None

    prose, usage = headless._extract_result("claude", "  plain prose  ")
    assert (prose, usage) == ("plain prose", None)


def test_extract_result_returns_usage_from_envelope():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "answer",
        "total_cost_usd": 0.0363,
        "duration_ms": 4120,
        "num_turns": 1,
        "usage": {
            "input_tokens": 4,
            "output_tokens": 12,
            "cache_creation_input_tokens": 5778,
            "cache_read_input_tokens": 3289,
        },
        "modelUsage": {
            "claude-sonnet-4-5-20250929": {"inputTokens": 4, "outputTokens": 12},
            "claude-haiku-4-5-20251001": {"inputTokens": 523, "outputTokens": 12},
        },
    })
    prose, usage = headless._extract_result("claude", raw, model="sonnet")
    assert prose == "answer"
    assert usage["input"] == 4
    assert usage["cache_creation"] == 5778
    assert usage["cache_read"] == 3289
    assert usage["cost_usd"] == 0.0363
    # The Haiku call every job fires, attributed away from the requested model.
    assert usage["side_calls"] == {"claude-haiku-4-5-20251001": 535}


def test_extract_output_cursor_rejects_error_envelope():
    raw = json.dumps({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "model overloaded",
    })
    with pytest.raises(ValueError, match="model overloaded"):
        headless._extract_output("cursor", raw)


def test_extract_output_cursor_rejects_non_string_result():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": {"nested": True},
    })
    with pytest.raises(ValueError, match="non-string result"):
        headless._extract_output("cursor", raw)


def test_run_headless_wave_cursor_folds_spf_and_writes_draft(tmp_path: Path):
    spf = tmp_path / "preamble.txt"
    spf.write_text("SYS\n", encoding="utf-8")
    out = tmp_path / "draft.txt"
    seen: list[tuple[list[str], str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen.append((list(cmd), input_text))
        assert "--system-prompt-file" not in cmd
        assert "--tools" not in cmd
        assert "--model" in cmd and "auto" in cmd
        assert Path(cwd).name == "claude-headless-empty"
        return 0, "translated prose here", ""

    result = headless.run_headless_wave(
        [{
            "id": "c0",
            "input_text": "BODY",
            "output_path": str(out),
            "system_prompt_file": str(spf),
        }],
        model="auto",
        concurrency=1,
        cli="cursor",
        runner=fake_runner,
    )
    assert result["counts"]["wrote"] == 1
    assert result["cli"] == "cursor"
    assert out.read_text(encoding="utf-8").strip() == "translated prose here"
    assert seen and seen[0][1] == "SYS\nBODY"


def test_run_headless_wave_rejects_claude_bin_with_cursor():
    result = headless.run_headless_wave(
        [],
        model="auto",
        concurrency=1,
        cli="cursor",
        claude_bin="/path/to/claude",
        runner=lambda *a, **k: (0, "", ""),
    )
    assert "error" in result
    assert "only valid with cli=claude" in result["error"]


def test_run_headless_wave_failure_reports_stdout_not_just_stderr(tmp_path: Path):
    """A warning on stderr must not hide the real cause on stdout.

    `claude -p` prints the actual reason it refused ("Credit balance is too low")
    on stdout while stderr carries an unrelated connectors warning. Preferring
    stderr reported the warning for every failed job and buried the one line that
    explained the whole wave.
    """
    out = tmp_path / "draft.txt"

    def fake_runner(cmd, *, input_text, cwd):
        return 1, "Credit balance is too low", "⚠ claude.ai connectors are disabled"

    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=fake_runner,
    )
    assert result["counts"]["failed"] == 1
    error = result["failed"][0]["error"]
    assert "Credit balance is too low" in error
    assert "connectors are disabled" in error


def test_run_headless_wave_failure_falls_back_to_exit_code(tmp_path: Path):
    out = tmp_path / "draft.txt"
    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (3, "", ""),
    )
    assert result["failed"][0]["error"] == "exit 3"


def test_run_headless_wave_cursor_error_envelope_fails_job(tmp_path: Path):
    out = tmp_path / "draft.txt"
    envelope = json.dumps({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "boom",
    })

    def fake_runner(cmd, *, input_text, cwd):
        return 0, envelope, ""

    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="auto",
        concurrency=1,
        cli="cursor",
        runner=fake_runner,
    )
    assert result["counts"]["failed"] == 1
    assert "boom" in result["failed"][0]["error"]
    assert not out.exists()


def test_run_headless_wave_cursor_missing_binary_error():
    result = headless.run_headless_wave(
        [],
        model="grok-4.5",
        concurrency=1,
        cli="cursor",
        cli_bin="definitely-not-cursor-agent-xyz",
        runner=None,
    )
    assert "error" in result
    assert "cursor-agent" in result["error"]
    assert "definitely-not-cursor-agent-xyz" in result["error"]
    assert "login" in result["error"]


def test_run_headless_wave_claude_empty_stdout_message(tmp_path: Path):
    out = tmp_path / "draft.txt"

    def empty_runner(cmd, *, input_text, cwd):
        return 0, "   \n", ""

    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        cli="claude",
        runner=empty_runner,
    )
    assert result["counts"]["failed"] == 1
    assert "empty stdout from claude -p" in result["failed"][0]["error"]


def test_run_headless_wave_rejects_unknown_cli():
    result = headless.run_headless_wave(
        [], model="x", concurrency=1, cli="gemini", runner=lambda *a, **k: (0, "", "")
    )
    assert "error" in result
    assert "unsupported headless cli" in result["error"]


# ---------------------------------------------------------------------------
# Subscription enforcement
#
# Headless must always bill the subscription, never metered API credit. The
# parent process legitimately holds ANTHROPIC_API_KEY (src/api_translator.py
# calls load_dotenv() at import and every fanout entry point pulls it in), and
# subprocess inherits os.environ by default, so waves used to bill the API until
# the balance ran out. Two layers now prevent that: the child env is scrubbed,
# and the wave refuses to start unless the CLI confirms a subscription login.
#
# The auth payloads below are verbatim `claude auth status --json` responses
# captured from a real CLI under each condition.
# ---------------------------------------------------------------------------

_AUTH_SUBSCRIPTION = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "someone@example.com",
    "orgId": "org-1",
    "orgName": "Personal",
    "subscriptionType": "pro",
}
_AUTH_API_KEY = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "apiKeySource": "ANTHROPIC_API_KEY",
    "email": None,
    "orgId": None,
    "orgName": None,
    "subscriptionType": None,
}
_AUTH_OAUTH_TOKEN = {
    "loggedIn": True,
    "authMethod": "oauth_token",
    "apiProvider": "firstParty",
}
_AUTH_BEDROCK = {
    "loggedIn": True,
    "authMethod": "third_party",
    "apiProvider": "bedrock",
}


def _prober(payload, rc: int = 0, stderr: str = ""):
    """A stub auth prober returning ``payload`` (dict -> JSON, or raw string)."""
    body = json.dumps(payload) if isinstance(payload, dict) else payload

    def probe(argv, *, env, cwd, timeout):
        return rc, body, stderr

    return probe


def _exploding_prober(argv, *, env, cwd, timeout):
    raise AssertionError("auth probe should not have run")


def test_subscription_env_drops_every_anthropic_var():
    base = {
        "ANTHROPIC_API_KEY": "sk-x",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_BASE_URL": "http://gateway.invalid",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Foo: bar",
        "ANTHROPIC_BEDROCK_BASE_URL": "http://bedrock.invalid",
        "ANTHROPIC_VERTEX_BASE_URL": "http://vertex.invalid",
        "ANTHROPIC_SOMETHING_INVENTED_LATER": "1",
        "PATH": "/usr/bin",
    }
    env = headless.subscription_env("claude", base=base)
    assert env == {"PATH": "/usr/bin"}


def test_subscription_env_drops_third_party_switches_and_cursor_key():
    base = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH": "1",
        "CURSOR_API_KEY": "cur-x",
        "PATH": "/usr/bin",
    }
    assert headless.subscription_env("claude", base=base) == {"PATH": "/usr/bin"}
    # One union list: the cursor key is scrubbed for the claude profile too.
    assert headless.subscription_env("cursor", base=base) == {"PATH": "/usr/bin"}


def test_subscription_env_keeps_oauth_token_and_ordinary_runtime():
    """CLAUDE_CODE_OAUTH_TOKEN *is* subscription auth (`claude setup-token`)."""
    base = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
        "CLAUDECODE": "1",
        "PATH": "/usr/bin",
        "PATHEXT": ".COM;.EXE;.CMD",
        "SYSTEMROOT": r"C:\Windows",
        "COMSPEC": r"C:\Windows\system32\cmd.exe",
        "HOME": "/home/x",
        "ANTHROPIC_API_KEY": "sk-x",
    }
    env = headless.subscription_env("claude", base=base)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    for key in ("CLAUDECODE", "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "HOME"):
        assert env[key] == base[key]


def test_subscription_env_preserves_key_spelling():
    """Env keys are case-sensitive on POSIX; survivors keep their original case."""
    base = {"Path": "/usr/bin", "myVar": "1", "ANTHROPIC_API_KEY": "sk-x"}
    env = headless.subscription_env("claude", base=base)
    assert env == {"Path": "/usr/bin", "myVar": "1"}


def test_subscription_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    env = headless.subscription_env("claude")
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env


def test_default_claude_runner_passes_scrubbed_env_to_subprocess(monkeypatch):
    """The regression test for the actual bug: no `env=` meant full inheritance."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(headless.subprocess, "run", fake_run)
    rc, out, err = headless.default_claude_runner(
        ["claude", "-p"], input_text="x", cwd=Path(".")
    )
    assert (rc, out, err) == (0, "ok", "")
    env = seen["env"]
    assert env is not None, "subprocess.run must be given an explicit env"
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env, "the scrub is a denylist; ordinary runtime must survive"


def test_default_claude_runner_accepts_a_precomputed_env(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(headless.subprocess, "run", fake_run)
    headless.default_claude_runner(
        ["claude", "-p"], input_text="x", cwd=Path("."), env={"PATH": "/usr/bin"}
    )
    assert seen["env"] == {"PATH": "/usr/bin"}


def test_subscription_auth_error_accepts_subscription():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(_AUTH_SUBSCRIPTION)
    )
    assert err is None


def test_subscription_auth_error_rejects_api_key_source():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(_AUTH_API_KEY)
    )
    assert err and "ANTHROPIC_API_KEY" in err
    assert "metered" in err


def test_subscription_auth_error_rejects_third_party_provider():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(_AUTH_BEDROCK)
    )
    assert err and "bedrock" in err


def test_subscription_auth_error_rejects_logged_out():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober({"loggedIn": False})
    )
    assert err and "not logged in" in err


def test_subscription_auth_error_accepts_setup_token_when_env_has_it():
    """`claude setup-token` auth reports oauth_token with no subscriptionType.

    ANTHROPIC_AUTH_TOKEN produces a byte-identical response, so the probe alone
    cannot tell them apart — the scrubbed env is the tiebreaker.
    """
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}
    err = headless.subscription_auth_error(
        "claude", "claude", env, cwd=".", prober=_prober(_AUTH_OAUTH_TOKEN)
    )
    assert err is None


def test_subscription_auth_error_accepts_setup_token_case_insensitive_env_key():
    """Windows / preserved spelling must not fail-close a valid setup-token."""
    env = {"Claude_Code_Oauth_Token": "sk-ant-oat01-x"}
    err = headless.subscription_auth_error(
        "claude", "claude", env, cwd=".", prober=_prober(_AUTH_OAUTH_TOKEN)
    )
    assert err is None


def test_subscription_auth_error_rejects_oauth_token_without_env():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(_AUTH_OAUTH_TOKEN)
    )
    assert err and "could not confirm a subscription login" in err


def test_subscription_auth_error_fails_closed_on_unregistered_cli():
    err = headless.subscription_auth_error(
        "typo-cli", "typo-cli", {}, cwd=".", prober=_exploding_prober
    )
    assert err and "no auth probe registered" in err
    assert "typo-cli" in err


def test_subscription_auth_error_fails_closed_on_nonzero_rc():
    err = headless.subscription_auth_error(
        "claude",
        "claude",
        {},
        cwd=".",
        prober=_prober("", rc=1, stderr="unknown command 'auth'"),
    )
    assert err and "unknown command" in err
    assert "--backend api" in err


def test_subscription_auth_error_fails_closed_on_unparseable_output():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober("not json at all")
    )
    assert err and "could not parse" in err


def test_subscription_auth_error_fails_closed_on_unknown_shape():
    err = headless.subscription_auth_error(
        "claude",
        "claude",
        {},
        cwd=".",
        prober=_prober({"loggedIn": True, "authMethod": "something-new"}),
    )
    assert err and "could not confirm a subscription login" in err


def test_subscription_auth_error_fail_closed_omits_pii():
    payload = {
        "loggedIn": True,
        "authMethod": "something-new",
        "apiProvider": "firstParty",
        "email": "someone@example.com",
        "orgId": "org-secret",
        "orgName": "Personal",
    }
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(payload)
    )
    assert err and "could not confirm a subscription login" in err
    assert "someone@example.com" not in err
    assert "org-secret" not in err
    assert "Personal" not in err
    assert "authMethod" in err


def test_subscription_auth_error_fails_closed_on_timeout():
    def boom(argv, *, env, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=boom, timeout=1.5
    )
    assert err and "timed out" in err
    assert "1.5" in err


def test_subscription_auth_error_fails_closed_on_oserror():
    def boom(argv, *, env, cwd, timeout):
        raise OSError("No such file or directory")

    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=boom
    )
    assert err and "could not run" in err
    assert "No such file or directory" in err


def test_subscription_auth_error_skips_cursor():
    """No verified cursor-agent probe exists; the scrub is its only guarantee."""
    err = headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=_exploding_prober
    )
    assert err is None


def test_subscription_auth_probe_uses_neutral_cwd_and_scrubbed_env(tmp_path: Path):
    """The probe must see what the workers see.

    `claude` reads project-local settings, so probing from the repo root can
    report a different auth path than a worker in the neutral cwd gets.
    """
    seen: dict[str, object] = {}

    def probe(argv, *, env, cwd, timeout):
        seen["argv"] = list(argv)
        seen["env"] = dict(env)
        seen["cwd"] = str(cwd)
        return 0, json.dumps(_AUTH_SUBSCRIPTION), ""

    out = tmp_path / "draft.txt"
    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "prose", ""),
        prober=probe,
    )
    assert result["counts"]["wrote"] == 1
    assert seen["argv"][1:] == ["auth", "status", "--json"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert Path(seen["cwd"]).name == "claude-headless-empty"


def test_run_headless_wave_blocks_the_wave_on_metered_auth(tmp_path: Path):
    out = tmp_path / "draft.txt"
    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=_exploding_prober,  # any invocation is a failure
        prober=_prober(_AUTH_API_KEY),
    )
    assert "error" in result
    assert "subscription preflight failed" in result["error"]
    assert "ANTHROPIC_API_KEY" in result["error"]
    assert result["counts"] == {"wrote": 0, "failed": 0, "todo": 0}
    assert not out.exists(), "no job may run, so no draft may be written"


def test_run_headless_wave_proceeds_on_subscription_auth(tmp_path: Path):
    out = tmp_path / "draft.txt"
    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "prose", ""),
        prober=_prober(_AUTH_SUBSCRIPTION),
    )
    assert result["counts"]["wrote"] == 1
    assert out.read_text(encoding="utf-8").strip() == "prose"


def test_run_headless_wave_skips_probe_for_stub_runner(tmp_path: Path, monkeypatch):
    """Pins the invariant that existing stub-runner tests never spawn a probe."""
    monkeypatch.setattr(headless, "_default_auth_prober", _exploding_prober)
    out = tmp_path / "draft.txt"
    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "prose", ""),
    )
    assert result["counts"]["wrote"] == 1


def test_run_headless_wave_skips_probe_when_there_are_no_jobs():
    result = headless.run_headless_wave(
        [],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "", ""),
        prober=_exploding_prober,
    )
    assert result["counts"]["todo"] == 0
    assert "error" not in result


# ---------------------------------------------------------------------------
# Usage telemetry (src/harness/usage.py)
#
# The 2026-07-30 friction log's finding was not that the wave was expensive but
# that nothing could see it: `--output-format text` threw the numbers away. These
# assert the numbers survive, that the detail stays out of the return value, and
# that every degraded path still writes a draft.
# ---------------------------------------------------------------------------


def _envelope(result: str, **usage) -> str:
    base = {"input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 3000}
    base.update(usage)
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False,
         "result": result, "total_cost_usd": 0.03, "usage": base}
    )


def _jobs(tmp_path: Path, n: int, body: str = "x" * 400) -> list[dict]:
    return [
        {"id": f"c{i}", "input_text": body, "output_path": str(tmp_path / f"d{i}.txt")}
        for i in range(n)
    ]


def test_wave_reports_usage_rollup_and_writes_job_log(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    result = headless.run_headless_wave(
        _jobs(tmp_path, 2),
        model="sonnet",
        concurrency=2,
        runner=lambda *a, **k: (0, _envelope('{"compliant": true}'), ""),
        usage_log=log,
    )
    assert result["counts"]["wrote"] == 2

    usage = result["usage"]
    assert usage["jobs"] == 2
    assert usage["cache_creation"] == 10_000
    assert usage["cache_read"] == 6_000
    # 400 chars / 4 = 100 tokens of real content per job; everything else billed
    # is per-process overhead, and the ratio is what makes that self-reporting.
    assert usage["prompt_sent"] == 200
    assert usage["overhead"] == 16_020 - 200
    assert usage["overhead_ratio"] == round((16_020 - 200) / 16_020, 3)
    assert "per_job" not in usage  # detail belongs on disk, not in context

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"c0", "c1"}
    assert all(r["rc"] == 0 and r["prompt_sent"] == 100 for r in rows)


def test_wave_omits_usage_when_the_cli_reports_none(tmp_path: Path):
    """Cursor waves and stubbed runners must not grow an empty `usage` key."""
    result = headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "plain prose", ""),
    )
    assert result["counts"]["wrote"] == 1
    assert "usage" not in result
    assert (tmp_path / "d0.txt").read_text(encoding="utf-8") == "plain prose\n"


def test_wave_logs_failed_jobs_too(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (1, "", "Credit balance is too low"),
        usage_log=log,
    )
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["rc"] == 1
    assert "Credit balance" in row["error"]


def _error_envelope(message: str, **usage) -> str:
    base = {"input_tokens": 5, "output_tokens": 9000,
            "cache_creation_input_tokens": 6000, "cache_read_input_tokens": 0}
    base.update(usage)
    return json.dumps(
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": message, "total_cost_usd": 0.4, "usage": base}
    )


def test_failed_job_still_reports_the_tokens_it_burned(tmp_path: Path):
    """A job that died after 9k output tokens cost real money; the wave must say so."""
    log = tmp_path / "usage.jsonl"
    result = headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (1, _error_envelope("Credit balance is too low"), ""),
        usage_log=log,
    )
    assert result["counts"]["failed"] == 1
    # Before this, `usage` came back None on a wave whose every job failed.
    assert result["usage"]["jobs"] == 1
    assert result["usage"]["output"] == 9000
    assert result["usage"]["cache_creation"] == 6000
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["rc"] == 1 and row["output"] == 9000


def test_failed_job_usage_does_not_move_the_baseline(tmp_path: Path):
    """Failures still burn tokens, but they are not what a healthy job costs."""
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 3),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (1, _error_envelope("boom"), ""),
        usage_log=log,
    )
    # Three logged jobs, but all rc != 0, so the estimate stays on its default.
    _, provenance = usage.baseline_tokens(log)
    assert provenance.startswith("default:")


def test_unwritable_usage_log_does_not_fail_the_wave(tmp_path: Path):
    """Telemetry that can take a wave down is worse than no telemetry."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    result = headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
        usage_log=blocker / "usage.jsonl",
    )
    assert result["counts"]["wrote"] == 1
    assert result["usage"]["jobs"] == 1


def test_warm_first_runs_job_one_alone_then_fans_out(tmp_path: Path):
    """One job warms the shared prefix so its siblings read it instead of re-creating it."""
    assert headless._wave_batches(list(range(8)), 5, True) == [[0], [1, 2, 3, 4, 5], [6, 7]]
    assert headless._wave_batches(list(range(8)), 5, False) == [[0, 1, 2, 3, 4], [5, 6, 7]]
    # Nothing to warm: a single job, or a serial wave, is unchanged.
    assert headless._wave_batches([0], 5, True) == [[0]]
    assert headless._wave_batches(list(range(3)), 1, True) == [[0], [1], [2]]


def test_warm_flag_is_recorded_per_job(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 3),
        model="sonnet",
        concurrency=2,
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
        usage_log=log,
    )
    rows = {
        json.loads(line)["id"]: json.loads(line)["warm"]
        for line in log.read_text(encoding="utf-8").splitlines()
    }
    assert rows == {"c0": True, "c1": False, "c2": False}


def test_extra_flags_are_recorded_with_the_job(tmp_path: Path):
    """The log is the A/B corpus: a row must say which argv produced it."""
    log = tmp_path / "usage.jsonl"
    seen: list[list[str]] = []

    def runner(cmd, *, input_text, cwd):
        seen.append(list(cmd))
        return 0, _envelope("ok"), ""

    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=runner,
        usage_log=log,
        extra_flags=["--strict-mcp-config"],
    )
    assert seen[0][-1] == "--strict-mcp-config"
    assert json.loads(log.read_text(encoding="utf-8"))["flags"] == ["--strict-mcp-config"]


def test_failed_job_reports_the_cause_not_the_envelope(tmp_path: Path):
    """Under --output-format json the reason a job died arrives wrapped."""
    envelope = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "Credit balance is too low",
        "session_id": "abc", "duration_ms": 12, "usage": {"input_tokens": 0},
    })
    result = headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (1, envelope, "claude.ai connectors are disabled"),
    )
    error = result["failed"][0]["error"]
    assert error.startswith("Credit balance is too low")
    assert "session_id" not in error  # the envelope itself stays out of the report
    assert "connectors are disabled" in error  # stderr still reported alongside


def test_failure_detail_names_the_cli_that_actually_failed(tmp_path: Path):
    """A failing Cursor job used to be reported as a 'claude result envelope error'."""
    envelope = json.dumps({
        "type": "result", "subtype": "error", "is_error": True, "result": "",
    })
    assert headless._failure_detail("cursor", envelope).startswith("cursor")
    assert headless._failure_detail("claude", envelope).startswith("claude")
    # Non-envelope stdout is still passed through untouched.
    assert headless._failure_detail("cursor", "  plain failure  ") == "plain failure"


# ---------------------------------------------------------------------------
# Prompt-cache TTL control
# ---------------------------------------------------------------------------


def test_prompt_cache_env_clears_an_inherited_contradiction():
    """An inherited knob would win over the resolved mode while the row logged the mode."""
    inherited = {"PATH": "/usr/bin", headless.DISABLE_PROMPT_CACHING: "1"}
    # Resolved 5m must actually be 5m, not "off with a 5m label on the usage row".
    assert headless.prompt_cache_env(inherited, mode="5m") == {
        "PATH": "/usr/bin", headless.FORCE_PROMPT_CACHING_5M: "1",
    }
    # 1h is the CLI default: both knobs gone, not "whatever was exported".
    assert headless.prompt_cache_env(inherited, mode="1h") == {"PATH": "/usr/bin"}
    assert headless.prompt_cache_env(
        {"PATH": "/usr/bin", headless.FORCE_PROMPT_CACHING_5M: "1"}, mode="off",
    ) == {"PATH": "/usr/bin", headless.DISABLE_PROMPT_CACHING: "1"}
    # Does not mutate the caller's mapping.
    assert inherited[headless.DISABLE_PROMPT_CACHING] == "1"


def test_prompt_cache_env_sets_exactly_one_var_per_mode():
    base = {"PATH": "/usr/bin", "FOO": "bar"}
    assert headless.prompt_cache_env(base, mode="1h") == base
    assert headless.prompt_cache_env(base, mode="5m") == {
        **base, headless.FORCE_PROMPT_CACHING_5M: "1",
    }
    assert headless.prompt_cache_env(base, mode="off") == {
        **base, headless.DISABLE_PROMPT_CACHING: "1",
    }
    # Does not mutate the input mapping.
    assert "FORCE_PROMPT_CACHING_5M" not in base
    with pytest.raises(ValueError, match="unknown prompt-cache mode"):
        headless.prompt_cache_env(base, mode="auto")


def test_resolve_cache_mode_picks_off_when_bodies_dominate():
    """Large U/P: plain input beats writing every body at 1.25×."""
    # P = 4k spf + 3.9k baseline; U ≈ 30k → U/P ≈ 3.8, above the N=5 threshold.
    spf = "preamble.txt"
    jobs = [
        {"input_text": "u" * (30_000 * 4), "system_prompt_file": spf}
        for _ in range(5)
    ]
    assert headless.resolve_cache_mode(
        jobs, {spf: 4_000}, baseline=3_900, warm_wall_s=None
    ) == "off"


def test_resolve_cache_mode_picks_5m_when_prefix_dominates():
    """Annotation-shaped: small bodies, large shared prefix → 5m."""
    spf = "preamble.txt"
    jobs = [
        {"input_text": "u" * (1_000 * 4), "system_prompt_file": spf}
        for _ in range(5)
    ]
    assert headless.resolve_cache_mode(
        jobs, {spf: 4_000}, baseline=3_900, warm_wall_s=None
    ) == "5m"
    # Just under the off threshold still stays on 5m.
    assert headless.resolve_cache_mode(
        jobs, {spf: 4_000}, baseline=3_900, warm_wall_s=100.0
    ) == "5m"


def test_resolve_cache_mode_slow_warmup_keeps_1h():
    """A warm-up over ~270 s risks expiring the 5-minute entry before followers."""
    spf = "preamble.txt"
    jobs = [
        {"input_text": "u" * (1_000 * 4), "system_prompt_file": spf}
        for _ in range(5)
    ]
    assert headless.resolve_cache_mode(
        jobs, {spf: 4_000}, baseline=3_900, warm_wall_s=300.0
    ) == "1h"
    # But off still wins when bodies dominate, even with a slow warm-up.
    big = [
        {"input_text": "u" * (30_000 * 4), "system_prompt_file": spf}
        for _ in range(5)
    ]
    assert headless.resolve_cache_mode(
        big, {spf: 4_000}, baseline=3_900, warm_wall_s=300.0
    ) == "off"


def test_resolve_cache_mode_no_spf_wave_uses_baseline_as_prefix():
    """Grouped judge entries have no --system-prompt-file; P is still the CLI baseline."""
    jobs = [{"input_text": "u" * 400} for _ in range(5)]
    assert headless.resolve_cache_mode(
        jobs, {}, baseline=3_900, warm_wall_s=None
    ) == "5m"


def test_resolve_cache_mode_no_history_assumes_fast():
    """No prior wall times → assume warm-up fits in the 5-minute window."""
    spf = "preamble.txt"
    jobs = [
        {"input_text": "u" * 400, "system_prompt_file": spf} for _ in range(3)
    ]
    assert headless.resolve_cache_mode(
        jobs, {spf: 4_000}, baseline=3_900, warm_wall_s=None
    ) == "5m"


def test_cache_off_collapses_warm_first_batches(tmp_path: Path):
    """off has nothing to warm — skip the serialized job-1 warm-up."""
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 3),
        model="sonnet",
        concurrency=2,
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
        usage_log=log,
        cache="off",
    )
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert all(r["cache"] == "off" for r in rows)
    assert all(r["warm"] is False for r in rows)


def test_resolved_cache_mode_lands_in_usage_rows(tmp_path: Path):
    """Stub runners never see the child env; the mode is still on every JSONL row."""
    log = tmp_path / "usage.jsonl"
    # Annotation-shaped bodies → auto resolves to 5m.
    spf = tmp_path / "preamble.txt"
    spf.write_text("p" * (4_000 * 4), encoding="utf-8")
    jobs = [
        {
            "id": f"c{i}",
            "input_text": "u" * (1_000 * 4),
            "output_path": str(tmp_path / f"d{i}.txt"),
            "system_prompt_file": str(spf),
        }
        for i in range(3)
    ]
    headless.run_headless_wave(
        jobs,
        model="sonnet",
        concurrency=2,
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
        usage_log=log,
        cache="auto",
    )
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {r["cache"] for r in rows} == {"5m"}
    assert rows[0]["warm"] is True  # warm-up still runs under 5m


def test_cursor_records_cache_none(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        cli="cursor",
        runner=lambda *a, **k: (0, "ok", ""),
        usage_log=log,
        cache="5m",
    )
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["cache"] is None
    assert row["effort"] is None
