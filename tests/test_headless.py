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
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_build_cmd_cursor_has_no_system_prompt_or_tools():
    cmd = headless._build_cmd("cursor", "cursor-agent", "grok-4.5", "ignored.txt")
    # json, not text: verified 2026-08-10 that cursor-agent emits the same
    # {"type":"result", …, "usage":{…}} envelope. On text it computed a ~17.2k
    # per-process overhead and threw the number away, exactly as claude did.
    assert cmd == [
        "cursor-agent",
        "-p",
        "--trust",
        "--mode",
        "ask",
        "--model",
        "grok-4.5",
        "--output-format",
        "json",
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


# Verbatim `cursor-agent status --format json`, 2026.08.04-aaa8809.
_CURSOR_AUTHENTICATED = {
    "status": "authenticated",
    "isAuthenticated": True,
    "hasAccessToken": True,
    "hasRefreshToken": True,
    "userInfo": {
        "email": "someone@example.com",
        "userId": 351249343,
        "firstName": "Someone",
        "lastName": "Example",
        "createdAt": "2026-04-21T16:40:15.477Z",
    },
}


def test_subscription_auth_error_accepts_cursor_authenticated():
    err = headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=_prober(_CURSOR_AUTHENTICATED)
    )
    assert err is None


def test_subscription_auth_error_accepts_cursor_status_string_alone():
    """Either key alone is enough; the CLI has spelled it both ways."""
    assert headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".",
        prober=_prober({"status": "authenticated"}),
    ) is None
    assert headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=_prober({"isAuthenticated": True}),
    ) is None


def test_subscription_auth_error_rejects_logged_out_cursor():
    err = headless.subscription_auth_error(
        "cursor",
        "cursor-agent",
        {},
        cwd=".",
        prober=_prober({"status": "unauthenticated", "isAuthenticated": False}),
    )
    assert err and "not logged in" in err
    assert "cursor-agent login" in err


def test_subscription_auth_error_cursor_never_echoes_user_info():
    """The payload carries email / real name / userId; only the verdict keys ship."""
    payload = {**_CURSOR_AUTHENTICATED, "status": "expired", "isAuthenticated": False}
    err = headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=_prober(payload)
    )
    assert err
    assert "someone@example.com" not in err
    assert "351249343" not in err
    assert "Someone" not in err
    assert "expired" in err


def test_subscription_auth_error_cursor_fails_closed_on_unknown_shape():
    err = headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=_prober({"somethingElse": 1})
    )
    assert err and "not logged in" in err


def test_subscription_auth_error_cursor_probe_argv():
    """`status --format json`, not a guess — and reported as itself when it fails."""
    seen: dict[str, object] = {}

    def probe(argv, *, env, cwd, timeout):
        seen["argv"] = list(argv)
        return 1, "", "unknown command 'status'"

    err = headless.subscription_auth_error(
        "cursor", "cursor-agent", {}, cwd=".", prober=probe
    )
    assert seen["argv"] == ["cursor-agent", "status", "--format", "json"]
    assert err and "cursor-agent status --format json" in err
    assert "unknown command" in err


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
    # `sonnet` carries no bracket, so there is genuinely no effort to record.
    assert row["effort"] is None


def test_cursor_records_the_bracket_effort_not_null(tmp_path: Path):
    """A Cursor wave logged `effort: null` while plainly running at some effort.

    `--effort` is Claude argv and is dropped from a Cursor command line, so the
    level has to be read back from the model's own bracket — otherwise "what did
    this wave run at?" is unanswerable from the log, which is the only place the
    answer is kept.
    """
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="grok-4.5[effort=high,fast=false]",
        concurrency=1,
        cli="cursor",
        runner=lambda *a, **k: (0, "ok", ""),
        usage_log=log,
        effort="medium",  # Claude argv: inert here, and must not be logged as truth
    )
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["effort"] == "high"


def test_claude_still_records_the_argv_effort(tmp_path: Path):
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
        usage_log=log,
        effort="medium",
    )
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["effort"] == "medium"


# ── Cursor model brackets: parse / compose / effort ─────────────────────────


def test_parse_and_compose_cursor_model_round_trip():
    parse, compose = headless.parse_cursor_model, headless.compose_cursor_model
    assert parse("grok-4.5[effort=high,fast=false]") == (
        "grok-4.5", {"effort": "high", "fast": "false"}
    )
    assert parse("grok-4.5") == ("grok-4.5", {})
    assert parse("") == ("", {})
    for model in ("grok-4.5[effort=high,fast=false]", "grok-4.5", "auto"):
        base, params = parse(model)
        assert compose(base, params) == model


def test_parse_cursor_model_drops_junk_rather_than_raising():
    """This parses argv a human may have typed; a bad knob must not kill a wave."""
    parse = headless.parse_cursor_model
    assert parse("grok-4.5[effort=high") == ("grok-4.5", {"effort": "high"})
    assert parse("grok-4.5[,,]") == ("grok-4.5", {})
    assert parse("grok-4.5[bare]") == ("grok-4.5", {})
    assert parse(None) == ("", {})


def test_with_cursor_effort_preserves_other_parameters():
    out = headless.with_cursor_effort("grok-4.5[effort=low,fast=false]", "xhigh")
    assert out == "grok-4.5[effort=xhigh,fast=false]"
    assert headless.cursor_model_effort(out) == "xhigh"
    # A None effort is "leave it alone", not "strip it".
    assert headless.with_cursor_effort(out, None) == out


def test_with_cursor_effort_leaves_the_auto_sentinel_alone():
    """`auto[effort=…]` is not known to be accepted, and any bracket forces a probe."""
    assert headless.with_cursor_effort("auto", "high") == "auto"
    assert headless.cursor_model_effort("auto") is None


def test_cursor_model_base_still_strips_brackets_for_the_alias_warning():
    """_cursor_model_base is now the parser's first element — same behaviour."""
    assert headless._cursor_model_base("sonnet[effort=low]") == "sonnet"
    warning = headless.warn_cursor_claude_model("cursor", "sonnet[effort=low]")
    assert warning and "headless_cli=cursor" in warning


def test_cursor_wave_now_reports_usage(tmp_path: Path):
    """The whole point of the argv flip: a Cursor wave stops being a blind spot."""
    log = tmp_path / "usage.jsonl"
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "translated prose", "duration_ms": 9099,
        "usage": {"inputTokens": 13874, "outputTokens": 29,
                  "cacheReadTokens": 5248, "cacheWriteTokens": 0},
    })
    result = headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="grok-4.5",
        concurrency=1,
        cli="cursor",
        runner=lambda *a, **k: (0, envelope, ""),
        usage_log=log,
    )
    assert result["counts"]["wrote"] == 1
    assert result["usage"]["input"] == 13874
    assert result["usage"]["cache_read"] == 5248
    # 100 tokens of real body against 19,122 billed — the ratio is the argument
    # about how many Cursor processes a wave is worth, now self-reporting.
    assert result["usage"]["overhead"] == 19_122 - 100
    row = json.loads(log.read_text(encoding="utf-8"))
    assert row["cli"] == "cursor" and row["cache_read"] == 5248


# ---------------------------------------------------------------------------
# Cursor model selection + validation
# ---------------------------------------------------------------------------


def _cursor_config(tmp_path: Path, doc) -> Path:
    path = tmp_path / "cli-config.json"
    path.write_text(
        doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8"
    )
    return path


def test_cursor_default_model_composes_the_bracket_form(tmp_path: Path):
    cfg = _cursor_config(tmp_path, {
        "selectedModel": {
            "modelId": "grok-4.5",
            "parameters": [
                {"id": "effort", "value": "low"},
                {"id": "fast", "value": "false"},
            ],
        },
    })
    assert headless.cursor_default_model(cfg) == "grok-4.5[effort=low,fast=false]"


def test_cursor_default_model_without_parameters(tmp_path: Path):
    cfg = _cursor_config(tmp_path, {"selectedModel": {"modelId": "gpt-5.2", "parameters": []}})
    assert headless.cursor_default_model(cfg) == "gpt-5.2"


def test_cursor_default_model_maps_cursors_own_auto(tmp_path: Path):
    """Cursor spells "let me pick" as modelId=default; --model spells it auto."""
    for model_id in ("default", "auto", ""):
        cfg = _cursor_config(tmp_path, {"selectedModel": {"modelId": model_id}})
        assert headless.cursor_default_model(cfg) == "auto"


def test_cursor_default_model_never_raises(tmp_path: Path):
    """A preferences file must not be able to stop a wave."""
    assert headless.cursor_default_model(tmp_path / "absent.json") == "auto"
    assert headless.cursor_default_model(_cursor_config(tmp_path, "{not json")) == "auto"
    assert headless.cursor_default_model(_cursor_config(tmp_path, "[1, 2]")) == "auto"
    assert headless.cursor_default_model(_cursor_config(tmp_path, {})) == "auto"
    assert headless.cursor_default_model(
        _cursor_config(tmp_path, {"selectedModel": "not a dict"})
    ) == "auto"
    # Junk parameters are dropped, not rendered into the argv.
    assert headless.cursor_default_model(_cursor_config(tmp_path, {
        "selectedModel": {
            "modelId": "grok-4.5",
            "parameters": ["nope", {"id": "", "value": 1}, {"id": "effort"},
                           {"id": "x", "value": {"nested": 1}},
                           {"id": "fast", "value": False}],
        },
    })) == "grok-4.5[fast=false]"


def test_default_worker_model_is_a_function_of_the_cli(monkeypatch):
    assert headless.default_worker_model("claude") == "sonnet"
    assert headless.default_worker_model("") == "sonnet"
    monkeypatch.setattr(headless, "cursor_default_model", lambda: "grok-4.5[effort=low]")
    assert headless.default_worker_model("cursor") == "grok-4.5[effort=low]"
    # And what it returns never trips the Claude-alias warning it replaced.
    assert headless.warn_cursor_claude_model(
        "cursor", headless.default_worker_model("cursor")
    ) is None


def test_warn_cursor_claude_model_strips_bracket_suffix():
    """A Claude alias with ``[effort=…]`` must still warn — operators paste both forms."""
    assert headless.warn_cursor_claude_model("cursor", "sonnet[effort=low]")
    assert headless.warn_cursor_claude_model("cursor", "claude-opus-4")
    assert headless.warn_cursor_claude_model("cursor", "grok-4.5[effort=low]") is None
    assert headless.warn_cursor_claude_model("claude", "sonnet[effort=low]") is None


def _model_probe(models_out: str, reject: str = ""):
    """Stub for both token-free model checks: `models`, then the argv probe."""
    def probe(argv, *, env, cwd, timeout):
        if argv[1:] == ["models"]:
            return 0, models_out, ""
        return 1, "", reject or "Error: No prompt provided for print mode"
    return probe


def test_cursor_model_error_passes_a_listed_id():
    probe = _model_probe("Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n")
    assert headless.cursor_model_error("cursor-agent", "auto", probe=probe) is None
    # The bracket suffix is stripped before the membership test.
    assert headless.cursor_model_error(
        "cursor-agent", "gpt-5.2[effort=low,fast=false]", probe=probe
    ) is None


def test_cursor_model_error_passes_an_unlisted_id_the_cli_accepts():
    """`cursor-agent models` is incomplete: grok-4.5 works and is not in it.

    Verified 2026-08-11. A membership test alone would have hard-failed the very
    id warn_cursor_claude_model recommends, so absence from the list only
    triggers the second, authoritative check.
    """
    probe = _model_probe("Available models\n\nauto - Auto\ncursor-grok-4.5-high - Grok\n")
    assert headless.cursor_model_error("cursor-agent", "grok-4.5", probe=probe) is None


def test_cursor_model_error_rejects_what_the_cli_rejects():
    probe = _model_probe(
        "Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n",
        reject="Cannot use this model: bogus-xyz. Available models: auto, gpt-5.2",
    )
    err = headless.cursor_model_error("cursor-agent", "bogus-xyz", probe=probe)
    assert err and "bogus-xyz" in err
    assert "Known ids: auto, gpt-5.2" in err


def test_cursor_model_error_rejects_bogus_brackets_on_a_listed_id():
    """A listed base id must not short-circuit past a rejected ``[effort=…]`` suffix."""
    probe = _model_probe(
        "Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n",
        reject="Cannot use this model: gpt-5.2[effort=bogus]. Available models: auto, gpt-5.2",
    )
    err = headless.cursor_model_error(
        "cursor-agent", "gpt-5.2[effort=bogus]", probe=probe
    )
    assert err and "gpt-5.2[effort=bogus]" in err
    # Valid brackets on a listed id still pass (probe returns the no-prompt error).
    assert headless.cursor_model_error(
        "cursor-agent",
        "gpt-5.2[effort=low,fast=false]",
        probe=_model_probe("Available models\n\ngpt-5.2 - GPT-5.2\n"),
    ) is None


def test_cursor_model_error_fails_open():
    """Validation must never be the thing that blocks a working wave."""
    def dead(argv, *, env, cwd, timeout):
        raise OSError("cursor-agent vanished")

    assert headless.cursor_model_error("cursor-agent", "grok-4.5", probe=dead) is None

    def timing_out(argv, *, env, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    assert headless.cursor_model_error("cursor-agent", "grok-4.5", probe=timing_out) is None
    # `models` unavailable, probe says something unrecognised → still pass.
    def confused(argv, *, env, cwd, timeout):
        return 127, "", "command not found"

    assert headless.cursor_model_error("cursor-agent", "grok-4.5", probe=confused) is None
    assert headless.cursor_model_error("cursor-agent", "", probe=_exploding_prober) is None


def test_wave_blocks_on_a_bad_cursor_model_with_zero_spawns(tmp_path: Path):
    """One message instead of one dead process per job."""
    out = tmp_path / "draft.txt"

    def probe(argv, *, env, cwd, timeout):
        if argv[1:] == ["status", "--format", "json"]:
            return 0, json.dumps(_CURSOR_AUTHENTICATED), ""
        if argv[1:] == ["models"]:
            return 0, "Available models\n\nauto - Auto\n", ""
        return 1, "", "Cannot use this model: nope. Available models: auto"

    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(out)}],
        model="nope",
        concurrency=1,
        cli="cursor",
        runner=_exploding_prober,  # any job invocation is a failure
        prober=probe,
    )
    assert "error" in result
    assert "model preflight failed" in result["error"]
    assert result["counts"] == {"wrote": 0, "failed": 0, "todo": 0}
    assert not out.exists()


# ---------------------------------------------------------------------------
# Progress hook and hoisted preflight
#
# Both exist for callers that are not a terminal: `run_headless_wave` returns
# only when the whole wave ends (minutes), and `fanout(estimate=True)` returns
# before the launcher's own preflight ever runs.
# ---------------------------------------------------------------------------


def test_on_job_done_fires_once_per_job_with_a_running_count(tmp_path: Path):
    seen: list[dict] = []
    headless.run_headless_wave(
        _jobs(tmp_path, 3),
        model="sonnet",
        concurrency=3,
        runner=lambda *a, **k: (0, "prose", ""),
        warm_first=False,
        on_job_done=seen.append,
    )
    assert len(seen) == 3
    assert {r["id"] for r in seen} == {"c0", "c1", "c2"}
    assert all(r["ok"] and r["error"] is None and r["total"] == 3 for r in seen)
    assert sorted(r["done"] for r in seen) == [1, 2, 3]


def test_on_job_done_reports_a_failed_job_too(tmp_path: Path):
    seen: list[dict] = []
    headless.run_headless_wave(
        _jobs(tmp_path, 1),
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (1, "Credit balance is too low", ""),
        on_job_done=seen.append,
    )
    assert len(seen) == 1 and seen[0]["ok"] is False
    assert "Credit balance" in seen[0]["error"]


def test_a_raising_progress_callback_does_not_fail_the_wave(tmp_path: Path):
    """A dead SSE queue must not be able to kill a wave that is spending tokens."""
    def boom(_record):
        raise RuntimeError("browser went away")

    result = headless.run_headless_wave(
        _jobs(tmp_path, 2),
        model="sonnet",
        concurrency=2,
        runner=lambda *a, **k: (0, "prose", ""),
        warm_first=False,
        on_job_done=boom,
    )
    assert result["counts"]["wrote"] == 2


def test_preflight_error_fails_closed_on_a_missing_binary(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda _name: None)
    err = headless.preflight_error("cursor")
    assert err and "cursor-agent not found" in err
    assert "cursor-agent login" in err


def test_preflight_error_fails_closed_on_an_unparseable_probe(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        headless, "_default_auth_prober", _prober("not json at all")
    )
    err = headless.preflight_error("claude")
    assert err and "could not parse" in err


def test_preflight_error_passes_a_subscription_login(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        headless, "_default_auth_prober", _prober(_AUTH_SUBSCRIPTION)
    )
    assert headless.preflight_error("claude") is None


def test_preflight_error_rejects_an_unknown_family():
    err = headless.preflight_error("gemini")
    assert err and "unsupported headless cli" in err


def test_worker_model_suggestions_claude_are_the_subscription_aliases():
    assert headless.worker_model_suggestions("claude") == [
        "fable", "haiku", "opus", "sonnet",
    ]


def test_worker_model_suggestions_cursor_include_the_selected_model(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        headless, "_cursor_known_models", lambda *a, **k: {"auto", "gpt-5.2"}
    )
    monkeypatch.setattr(headless, "cursor_default_model", lambda: "grok-4.5[effort=high]")
    assert headless.worker_model_suggestions("cursor") == [
        "auto", "gpt-5.2", "grok-4.5[effort=high]",
    ]


def test_worker_model_suggestions_survive_a_missing_cursor_cli(monkeypatch):
    """A model list is a nicety; failing to read one must never raise."""
    monkeypatch.setattr(headless.shutil, "which", lambda _name: None)
    monkeypatch.setattr(headless, "cursor_default_model", lambda: "auto")
    assert headless.worker_model_suggestions("cursor") == ["auto"]


def test_wave_model_preflight_is_cursor_only(tmp_path: Path):
    """A Claude wave must not pay for a Cursor-shaped check."""
    seen: list[list[str]] = []

    def probe(argv, *, env, cwd, timeout):
        seen.append(list(argv))
        return 0, json.dumps(_AUTH_SUBSCRIPTION), ""

    result = headless.run_headless_wave(
        [{"id": "c0", "input_text": "x", "output_path": str(tmp_path / "d.txt")}],
        model="sonnet",
        concurrency=1,
        runner=lambda *a, **k: (0, "prose", ""),
        prober=probe,
    )
    assert result["counts"]["wrote"] == 1
    assert [a[1:] for a in seen] == [["auth", "status", "--json"]]
