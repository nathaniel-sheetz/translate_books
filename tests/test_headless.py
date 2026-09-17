"""Unit tests for the shared headless CLI launcher (claude + cursor profiles)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
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


class _FakeProc:
    """A stand-in for ``Popen``: the launcher drives it, never ``subprocess.run``.

    ``run`` enforced its timeout by killing only the direct child and then
    draining the pipes unbounded, which a wrapper's surviving grandchild turned
    into a 2-3x overrun. The launcher therefore owns the ``Popen`` itself.
    """

    returncode = 0
    stdin = stdout = stderr = None

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's own name
        return "ok", ""


def test_default_claude_runner_passes_scrubbed_env_to_subprocess(monkeypatch):
    """The regression test for the actual bug: no `env=` meant full inheritance."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        seen["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(headless.subprocess, "Popen", fake_popen)
    rc, out, err = headless.default_claude_runner(
        ["claude", "-p"], input_text="x", cwd=Path(".")
    )
    assert (rc, out, err) == (0, "ok", "")
    env = seen["env"]
    assert env is not None, "the child must be given an explicit env"
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env, "the scrub is a denylist; ordinary runtime must survive"


def test_default_claude_runner_accepts_a_precomputed_env(monkeypatch):
    seen: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(headless.subprocess, "Popen", fake_popen)
    headless.default_claude_runner(
        ["claude", "-p"], input_text="x", cwd=Path("."), env={"PATH": "/usr/bin"}
    )
    assert seen["env"] == {"PATH": "/usr/bin"}


def test_a_running_spawn_is_registered_so_an_interrupt_can_reach_it(monkeypatch):
    """``KeyboardInterrupt`` lands on the main thread; the Popen is in a worker.

    Without the registry the interrupting thread holds no handle on the children
    it has to kill, which is exactly why the ``except BaseException`` guarding a
    spawn never fired for an operator's Ctrl-C.
    """
    live_during: list[int] = []

    class _RegisteredProc(_FakeProc):
        pid = 4242

        def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's name
            live_during.append(len(headless._live_procs))
            return "ok", ""

    monkeypatch.setattr(
        headless.subprocess, "Popen", lambda cmd, **kwargs: _RegisteredProc()
    )
    headless.default_claude_runner(["claude", "-p"], input_text="x", cwd=Path("."))

    assert live_during == [1], "the spawn must be registered while it runs"
    assert not headless._live_procs, "and discarded the moment it returns"


def test_kill_live_processes_tree_kills_everything_registered(monkeypatch):
    killed: list[object] = []
    monkeypatch.setattr(headless, "_kill_process_tree", killed.append)

    proc = _FakeProc()
    with headless._tracked(proc):
        assert headless._kill_live_processes() == 1
    assert killed == [proc]
    assert not headless._live_procs


def test_the_auth_prober_bounds_its_own_drain(tmp_path: Path):
    """The preflight spawns hit the same stdlib defect the workers did.

    ``subprocess.run`` kills only the direct child and then, on Windows, drains
    with no timeout — so ``subscription_auth_error``'s ``TimeoutExpired`` handler
    was unreachable and the 30 s ceiling it documents was not real. It must still
    *raise*, so that handler keeps failing closed.
    """
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        headless._default_auth_prober(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            cwd=tmp_path,
            timeout=0.5,
        )
    assert time.monotonic() - started < headless._DRAIN_TIMEOUT_S + 5


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


def _distinct_jobs(tmp_path: Path, n: int) -> list[dict]:
    """Like ``_jobs`` but each body identifies its job, so a stub runner can tell them apart."""
    return [
        {"id": f"c{i}", "input_text": f"body-{i}", "output_path": str(tmp_path / f"d{i}.txt")}
        for i in range(n)
    ]


def test_warm_first_runs_job_one_alone_then_fans_out(tmp_path: Path):
    """The warm-up only warms anything if it finishes before its siblings start."""
    lock = threading.Lock()
    in_flight: list[str] = []
    overlapped_the_warm_job: list[str] = []
    peak = 0

    def runner(cmd, *, input_text, cwd):
        nonlocal peak
        with lock:
            in_flight.append(input_text)
            peak = max(peak, len(in_flight))
            if "body-0" in in_flight and len(in_flight) > 1:
                overlapped_the_warm_job.append(input_text)
        time.sleep(0.02)
        with lock:
            in_flight.remove(input_text)
        return 0, _envelope("ok"), ""

    result = headless.run_headless_wave(
        _distinct_jobs(tmp_path, 8), model="sonnet", concurrency=5, runner=runner,
    )
    assert result["counts"]["wrote"] == 8
    assert overlapped_the_warm_job == []  # job 0 had the machine to itself
    assert peak <= 5  # and the pool never exceeded the requested width


def test_a_slow_job_does_not_hold_up_the_rest(tmp_path: Path):
    """The rolling-pool win. Fixed batches made every job wait for the slowest in its group.

    Deadlocks (and so fails) on the old batching: with ``concurrency=3`` only the
    first three jobs could ever run, so the slow job's release condition — every
    *other* job having landed — was unreachable.
    """
    release = threading.Event()
    finished: list[str] = []
    lock = threading.Lock()

    def runner(cmd, *, input_text, cwd):
        if input_text == "body-1":
            assert release.wait(timeout=10), "the pool never rolled past the slow job"
        with lock:
            finished.append(input_text)
            if len(finished) == 7:  # everything except the slow job
                release.set()
        return 0, _envelope("ok"), ""

    result = headless.run_headless_wave(
        _distinct_jobs(tmp_path, 8), model="sonnet", concurrency=3,
        runner=runner, warm_first=False,
    )
    assert result["counts"]["wrote"] == 8
    assert finished[-1] == "body-1"


def test_an_empty_wave_is_a_no_op_not_a_crash(tmp_path: Path):
    """``max_workers=0`` is a ValueError, and an empty fan-out must stay idempotent."""
    result = headless.run_headless_wave(
        [], model="sonnet", concurrency=5, runner=lambda *a, **k: (0, "", ""),
    )
    assert result["counts"] == {"wrote": 0, "failed": 0, "todo": 0}
    assert "error" not in result


def test_warm_label_survives_a_serial_wave(tmp_path: Path):
    """At concurrency 1 the old batching still marked job 0 warm.

    Nothing is serialized for its benefit there, but ``usage.jsonl`` is an A/B
    corpus and a silently relabelled row is a corrupted one. This case was
    unpinned before the rolling pool replaced the batching.
    """
    log = tmp_path / "usage.jsonl"
    headless.run_headless_wave(
        _jobs(tmp_path, 3), model="sonnet", concurrency=1,
        runner=lambda *a, **k: (0, _envelope("ok"), ""), usage_log=log,
    )
    rows = {
        json.loads(line)["id"]: json.loads(line)["warm"]
        for line in log.read_text(encoding="utf-8").splitlines()
    }
    assert rows == {"c0": True, "c1": False, "c2": False}


# ---------------------------------------------------------------------------
# Per-worker Cursor config directories
#
# Concurrent cursor-agent processes raced ~/.cursor/cli-config.json, failing
# ~3% of jobs at widths 2-5 with EPERM on the rename. Each worker now gets its
# own CURSOR_CONFIG_DIR.
# ---------------------------------------------------------------------------


@pytest.fixture
def slot_source(tmp_path: Path, monkeypatch) -> Path:
    """A seeded Cursor config dir, with the module's slot state isolated.

    The free list, the poisoned set and the tally are all module-level, so
    without this a test that takes a slot changes what the next test sees.
    """
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    monkeypatch.setattr(headless, "_slot_free", [])
    monkeypatch.setattr(headless, "_slot_high", 0)
    monkeypatch.setattr(headless, "_slot_poisoned", set())
    monkeypatch.setattr(headless, "_slot_stats", {"seeded": 0, "unseeded": 0})
    monkeypatch.setattr(headless, "_slot_first_error", None)
    source = tmp_path / "cursor-home"
    source.mkdir()
    (source / "cli-config.json").write_text('{"selectedModel": {}}', encoding="utf-8")
    return source


def test_slot_dir_mode_never_restricts_the_acl_on_windows(monkeypatch):
    """A restrictive mode on Windows is a lockout, not a permission.

    Python turns ``0o700`` into a protected descriptor granting only OWNER
    RIGHTS, SYSTEM and Administrators -- no ACE for the user's own SID. A
    scheduled task creates files owned by ``BUILTIN\\Administrators``, which an
    interactive token holds deny-only, so the operator lost every right to the
    nightly's ``cli-config.json`` including ``READ_CONTROL``. POSIX keeps
    ``0o700``, where the mode means what it says.
    """
    monkeypatch.setattr(headless.os, "name", "nt")
    assert headless._slot_dir_mode() == 0o777  # Path.mkdir's default: a no-op
    monkeypatch.setattr(headless.os, "name", "posix")
    assert headless._slot_dir_mode() == 0o700


def test_seed_creates_the_slot_with_that_mode(
    slot_source: Path, tmp_path: Path, monkeypatch
):
    """A mode nothing passes to ``mkdir`` is a comment, not a behavior.

    Recorded for the slot itself only: ``parents=True`` makes ``Path.mkdir``
    recurse into missing parents *without* forwarding the mode, so an unfiltered
    spy sees those calls too and says nothing about the directory under test.
    The mode is read positionally as well as by keyword, because the retry that
    follows creating the parents passes it positionally.

    Compared against a **literal**, never against ``_slot_dir_mode()``: asserting
    the call site equals the function it calls is a tautology that holds however
    both change, so re-hardcoding ``mode=0o700`` would still pass everywhere the
    ACL test is skipped -- which is every POSIX CI runner.
    """
    slot = tmp_path / "slots" / "slot-0"
    expected = 0o777 if os.name == "nt" else 0o700
    seen: list[int] = []
    real_mkdir = Path.mkdir

    def spy(self: Path, *args, **kwargs):
        if self == slot:
            seen.append(kwargs.get("mode", args[0] if args else 0o777))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy)
    assert headless._seed_slot_config(slot, slot_source)
    assert seen and all(mode == expected for mode in seen)


def _slot_dacl_or_skip(path: Path) -> str:
    """``path``'s DACL, as SDDL with the leading ``D:`` stripped.

    Splitting on ``D:`` is the load-bearing part, not tidiness.
    ``Get-Acl().Sddl`` leads with the ``O:`` **owner** field, and a ``0o700``
    directory is owned by the very user a per-user-ACE assertion would search
    for -- so testing against the whole descriptor passes with the bug fully
    present. Only the DACL distinguishes the two cases.

    Skips rather than fails when the descriptor cannot be read at all: an
    absent PowerShell or a blocking ExecutionPolicy says nothing about the code
    under test.
    """
    try:
        probe = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop';"
                "(Get-Acl -LiteralPath $env:SLOT_ACL_PROBE).Sddl",
            ],
            capture_output=True, text=True, timeout=120,
            # Through the environment, not interpolated: a quote or a space in
            # the temp path would otherwise break the PowerShell literal.
            env={**os.environ, "SLOT_ACL_PROBE": str(path)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # no PowerShell here
        pytest.skip(f"cannot read an ACL in this environment: {exc}")
    if probe.returncode != 0:  # ExecutionPolicy, or Get-Acl refused
        pytest.skip(f"Get-Acl unavailable: {probe.stderr.strip()[:200]}")
    sddl = probe.stdout.strip()
    assert "D:" in sddl, f"no DACL in descriptor: {sddl!r}"
    return sddl.split("D:", 1)[1]


@pytest.mark.skipif(os.name != "nt", reason="ACL inheritance is Windows-only")
def test_a_seeded_slot_does_not_block_acl_inheritance(
    slot_source: Path, tmp_path: Path
):
    """The slot's DACL must not be *protected* -- that flag is the whole defect.

    Asserted on ``D:P`` rather than on a per-user ACE, and on the DACL alone
    rather than on the whole SDDL. Both traps are real, and both were measured
    rather than reasoned about:

    - ``Get-Acl().Sddl`` leads with the ``O:`` **owner** field, and a ``0o700``
      directory is owned by the very user whose SID you would search for -- so
      ``sid in sddl`` returns ``True`` with the bug fully present. A test
      written that way passes either way and pins nothing.
    - The inherited ACE is no better a witness here. ``tmp_path`` lives under
      pytest's basetemp, which pytest itself creates ``0o700``, so a
      correctly-inheriting slot inherits only ``OW`` (owner rights) and carries
      no per-user ACE at all.

    ``D:P`` is the one thing that actually differs: present under ``0o700``,
    absent when inheritance is left alone.
    """
    slot = tmp_path / "slots" / "slot-0"
    assert headless._seed_slot_config(slot, slot_source)
    dacl = _slot_dacl_or_skip(slot)
    assert not dacl.startswith("P"), f"slot DACL blocks inheritance: D:{dacl}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows has the ACL test")
def test_a_seeded_slot_is_private_on_posix(slot_source: Path, tmp_path: Path):
    """``0o700`` must reach the filesystem, not merely be returned.

    The literal assertions above pin what :func:`_slot_dir_mode` *returns*; this
    pins the directory it produces. It matters most on exactly the platform
    where the Windows ACL test is skipped -- every POSIX CI runner --
    because ``cli-config.json`` carries ``authInfo.email`` and ``userId`` and a
    group- or world-readable slot would widen both.
    """
    slot = tmp_path / "slots" / "slot-0"
    assert headless._seed_slot_config(slot, slot_source)
    assert slot.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "nt", reason="protected DACLs are Windows-only")
def test_an_existing_protected_slot_is_not_repaired(
    slot_source: Path, tmp_path: Path
):
    """A known limitation, pinned so it cannot change unnoticed.

    ``mkdir(exist_ok=True)`` ignores the mode for a directory that already
    exists, so dropping the restrictive mode fixes slots created *from now on*
    and leaves every existing root exactly as it was. That is why upgrading
    alone changes nothing on a machine that already has ``~/.cursor-slots``: it
    must be cleared once, with elevation, because a scheduled task owns what is
    inside it.

    Asserted rather than merely documented because it cuts both ways. If this
    test starts failing, seeding has begun repairing ACLs by itself -- which
    would be welcome, but it would also make the manual cleanup step in
    ``docs/LLM_PROVIDERS.md`` and the CHANGELOG wrong.
    """
    slot = tmp_path / "slots" / "slot-0"
    slot.mkdir(parents=True, mode=0o700)  # a slot created before the fix
    assert headless._seed_slot_config(slot, slot_source)
    assert _slot_dacl_or_skip(slot).startswith("P"), (
        "an existing slot's protected DACL was repaired; the documented "
        "one-time cleanup is now stale"
    )


def test_concurrent_slots_are_distinct_and_seeded(tmp_path: Path, slot_source: Path):
    """Four genuinely overlapping workers must get four different directories."""
    seen: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        with headless._cursor_slot(slot_source):
            env = headless._slot_env("cursor", {"PATH": "/x"})
            barrier.wait(timeout=10)  # hold every slot at once
            with lock:
                seen.append(env["CURSOR_CONFIG_DIR"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(set(seen)) == 4
    for path in seen:
        # Only the one small file is seeded -- never a recursive copy of
        # ~/.cursor, and no torn .tmp left behind.
        assert sorted(p.name for p in Path(path).iterdir()) == ["cli-config.json"]


def test_slots_are_reused_across_waves(slot_source: Path, tmp_path: Path):
    """A monotonic counter would leak a directory per wave; the free-list must not."""
    for _ in range(5):
        with headless._cursor_slot(slot_source):
            pass
    assert [p.name for p in (tmp_path / "slots").iterdir()] == ["slot-0"]


def test_a_failed_seed_leaves_the_config_dir_alone(slot_source: Path, tmp_path: Path):
    """Fail open means today's behavior, NOT an empty config dir.

    ``cli-config.json`` carries ``permissions.allow``/``deny`` and
    ``privacyCache``, so pointing a worker at an unseeded directory would
    silently change its permissions and data-retention posture.
    """
    with headless._cursor_slot(tmp_path / "no-such-config-dir"):
        assert headless._slot_env("cursor", {"PATH": "/x"}) == {"PATH": "/x"}


def test_a_poisoned_slot_is_retired_not_recycled(
    slot_source: Path, tmp_path: Path, monkeypatch
):
    """A slot that cannot be seeded must never come back around.

    The nightly task runs under another identity and leaves ``slot-0`` with an
    ACL this user can neither read nor delete. Returning that index to the free
    list would make every job of every later wave retry it.
    """
    bad = tmp_path / "slots" / "slot-0"
    real_seed = headless._seed_slot_config

    def seed(slot_dir: Path, source_dir: Path) -> bool:
        return False if slot_dir == bad else real_seed(slot_dir, source_dir)

    monkeypatch.setattr(headless, "_seed_slot_config", seed)

    seen: list[str] = []
    for _ in range(3):
        with headless._cursor_slot(slot_source):
            seen.append(headless._slot_env("cursor", {})["CURSOR_CONFIG_DIR"])

    assert "slot-0" not in {Path(p).name for p in seen}
    assert 0 in headless._slot_poisoned
    # Retired once: the free list then serves the same good index every time.
    assert len(set(seen)) == 1
    assert headless._slot_tally() == "3/3"


def test_giving_up_on_isolation_is_counted_and_logged(
    slot_source: Path, tmp_path: Path, caplog
):
    """Failing open is right; failing open *silently* is the defect.

    A wave with isolation working and one with it disabled used to be
    byte-identical from the outside, which is what made the poisoned-directory
    and long-path failures so expensive to diagnose.
    """
    with caplog.at_level(logging.WARNING, logger="src.harness.headless"):
        with headless._cursor_slot(tmp_path / "no-such-config-dir"):
            assert headless._slot_env("cursor", {"PATH": "/x"}) == {"PATH": "/x"}

    assert headless._slot_tally() == "0/1"
    assert "could not be seeded" in caplog.text
    # A missing *source* is not a poisoned slot: advancing cannot help when the
    # file is absent at the same path every time, so no index is burned...
    assert headless._slot_poisoned == set()
    # ...and the warning names the file that is actually missing, rather than a
    # destination slot that was never at fault.
    assert "no-such-config-dir" in caplog.text
    assert "cli-config.json" in caplog.text
    assert "slot-" not in caplog.text


def test_the_slot_root_is_short_and_per_user(monkeypatch):
    """Two unrelated properties, both load-bearing -- see ``_slot_root``."""
    monkeypatch.delenv(headless._SLOT_ROOT_VAR, raising=False)
    root = headless._slot_root()
    assert root.parent == Path.home()  # per-user by construction
    assert headless._slot_path_error(root) is None  # and inside the budget

    monkeypatch.setenv(headless._SLOT_ROOT_VAR, "C:/ct")
    assert headless._slot_root() == Path("C:/ct").expanduser().resolve()


def test_an_over_budget_slot_root_is_refused_before_anything_spawns(
    monkeypatch, tmp_path: Path
):
    """A 261-character root killed 4 of 4 long jobs with rc=124 and a *Cursor
    endpoint* reconnect message, which reads exactly like a provider outage. It
    must never reach the jobs."""
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv(headless._SLOT_ROOT_VAR, "C:/" + "x" * 130)
    seeded: list[tuple] = []
    monkeypatch.setattr(
        headless, "_seed_slot_config", lambda *args: seeded.append(args) or True
    )

    result = headless.run_headless_wave(
        _jobs(tmp_path, 2), model="grok-4.6", concurrency=2, cli="cursor",
    )

    assert "over the 120-character budget" in result["error"]
    assert headless._SLOT_ROOT_VAR in result["error"]  # names the way out
    assert result["wrote"] == [] and result["failed"] == []
    assert seeded == []


def test_a_relative_slot_root_is_measured_after_resolve(tmp_path: Path, monkeypatch):
    """A relative override would otherwise pass the budget as a few characters."""
    pad = "p" * max(0, headless._SLOT_PATH_BUDGET + 1 - len(str(tmp_path)))
    deep = tmp_path / pad if pad else tmp_path
    deep.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(deep)
    monkeypatch.setenv(headless._SLOT_ROOT_VAR, "slots")
    resolved = headless._slot_root()
    assert resolved.is_absolute()
    err = headless._slot_path_error(resolved)
    assert err is not None
    assert "over the 120-character budget" in err


def test_a_timed_out_worker_dies_with_its_whole_tree(tmp_path: Path, monkeypatch):
    """The ceiling is not real until the post-kill drain is bounded.

    ``subprocess.run`` kills only the direct child and then drains the pipes with
    **no** timeout, so a surviving grandchild held the wave open for two to three
    times the job budget -- 1253, 1293 and 1977 s against a 900 s ceiling.
    """
    killed: list[int] = []
    real_kill = headless._kill_process_tree

    def spy(proc):
        killed.append(proc.pid)
        real_kill(proc)

    monkeypatch.setattr(headless, "_kill_process_tree", spy)
    started = time.monotonic()
    rc, _out, err = headless.default_claude_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        input_text="",
        cwd=tmp_path,
        timeout=0.5,
        env=dict(os.environ),
    )
    elapsed = time.monotonic() - started

    assert rc == 124
    assert killed, "the tree killer must run"
    assert "timeout after 0.5s" in err
    assert elapsed < headless._DRAIN_TIMEOUT_S + 5  # nowhere near the child's 30 s


def test_timed_out_is_recorded_only_when_true():
    """``usage.jsonl`` is an A/B corpus; every row it already holds keeps shape."""
    base = dict(job_id="j", cli="cursor", model="m", prompt_sent=1, wall_s=1.0, rc=0)
    assert "timed_out" not in usage.job_record(**base)
    assert usage.job_record(**base, timed_out=True)["timed_out"] is True


def test_slots_seeded_reaches_the_usage_rollup(tmp_path: Path, monkeypatch):
    """A wave that lost isolation must not read like one that kept it.

    This is the signal whose absence made the other slot defects expensive: the
    rollup is already rendered by every caller and by the dashboard, so the
    count rides along with no call-site changes.
    """
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    monkeypatch.setattr(headless, "_slot_free", [])
    monkeypatch.setattr(headless, "_slot_high", 0)
    monkeypatch.setattr(headless, "_slot_poisoned", set())
    monkeypatch.setattr(headless, "_slot_stats", {"seeded": 0, "unseeded": 0})
    monkeypatch.setattr(headless, "_slot_first_error", None)

    source = tmp_path / "cursor-home"
    source.mkdir()
    (source / "cli-config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(headless, "_cursor_config_dir", lambda env: source)
    monkeypatch.setattr(headless, "subscription_auth_error", lambda *a, **k: None)
    monkeypatch.setattr(headless, "cursor_model_error", lambda *a, **k: None)
    monkeypatch.setattr(
        headless,
        "default_claude_runner",
        lambda cmd, **kw: (0, _envelope("ok"), ""),
    )

    result = headless.run_headless_wave(
        _jobs(tmp_path, 3), model="grok-4.6", concurrency=2, cli="cursor",
    )

    assert result["counts"]["wrote"] == 3
    # Three workers, three slots. The preflight takes one of its own and is
    # excluded on purpose -- it is not a worker, and counting it would make a
    # clean wave report 4/4 against 3 jobs.
    assert result["usage"]["slots_seeded"] == "3/3"


def test_slots_seeded_survives_a_wave_where_every_job_failed(
    tmp_path: Path, monkeypatch
):
    """The tally must not vanish in the one failure shape it exists to expose.

    ``rollup`` returns ``None`` when no job reported tokens -- an all-timed-out
    wave, which reports nothing at all -- and that used to take ``slots_seeded``
    down with it, precisely when isolation was most worth checking.
    """
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    monkeypatch.setattr(headless, "_slot_free", [])
    monkeypatch.setattr(headless, "_slot_high", 0)
    monkeypatch.setattr(headless, "_slot_poisoned", set())
    monkeypatch.setattr(headless, "_slot_stats", {"seeded": 0, "unseeded": 0})
    monkeypatch.setattr(headless, "_slot_first_error", None)

    source = tmp_path / "cursor-home"
    source.mkdir()
    (source / "cli-config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(headless, "_cursor_config_dir", lambda env: source)
    monkeypatch.setattr(headless, "subscription_auth_error", lambda *a, **k: None)
    monkeypatch.setattr(headless, "cursor_model_error", lambda *a, **k: None)
    # rc=124 and no JSON: what a killed cursor-agent actually leaves behind.
    monkeypatch.setattr(
        headless,
        "default_claude_runner",
        lambda cmd, **kw: (124, "", "timeout after 900s"),
    )

    result = headless.run_headless_wave(
        _jobs(tmp_path, 2), model="grok-4.6", concurrency=2, cli="cursor",
    )

    assert result["counts"]["failed"] == 2
    assert result["usage"]["slots_seeded"] == "2/2"


def test_claude_never_gets_a_cursor_config_dir(slot_source: Path):
    with headless._cursor_slot(slot_source):
        assert "CURSOR_CONFIG_DIR" not in headless._slot_env("claude", {"PATH": "/x"})


def test_slot_env_preserves_the_credential_scrub(slot_source: Path):
    """The per-slot env must be derived from the scrubbed wave env, never os.environ."""
    scrubbed = headless.subscription_env(
        "cursor", base={"ANTHROPIC_API_KEY": "sk-x", "CURSOR_API_KEY": "c", "PATH": "/x"}
    )
    with headless._cursor_slot(slot_source):
        env = headless._slot_env("cursor", scrubbed)
    assert "ANTHROPIC_API_KEY" not in env and "CURSOR_API_KEY" not in env
    assert env["PATH"] == "/x" and env["CURSOR_CONFIG_DIR"]


def test_a_newer_operator_config_is_re_seeded(slot_source: Path):
    """The operator changed their model picker mid-run; slots must not pin the old one."""
    with headless._cursor_slot(slot_source):
        slot = Path(headless._slot_env("cursor", {})["CURSOR_CONFIG_DIR"])
    assert json.loads((slot / "cli-config.json").read_text(encoding="utf-8")) == {
        "selectedModel": {}
    }

    config = slot_source / "cli-config.json"
    config.write_text('{"selectedModel": {"modelId": "grok-4.6"}}', encoding="utf-8")
    later = time.time() + 10
    os.utime(config, (later, later))
    with headless._cursor_slot(slot_source):
        pass
    assert json.loads((slot / "cli-config.json").read_text(encoding="utf-8")) == {
        "selectedModel": {"modelId": "grok-4.6"}
    }


def test_cursor_config_dir_follows_the_clis_own_precedence(tmp_path: Path):
    """Verified against the 2026.09.10-fd3934a bundle's own resolver."""
    assert headless._cursor_config_dir({"CURSOR_CONFIG_DIR": str(tmp_path)}) == tmp_path
    assert (
        headless._cursor_config_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "cursor"
    )
    assert headless._cursor_config_dir({}) == Path.home() / ".cursor"
    # An operator's explicit relocation wins over XDG, and blank is not a choice.
    assert headless._cursor_config_dir(
        {"CURSOR_CONFIG_DIR": str(tmp_path), "XDG_CONFIG_HOME": "/other"}
    ) == tmp_path
    assert headless._cursor_config_dir({"CURSOR_CONFIG_DIR": "   "}) == Path.home() / ".cursor"


def test_an_empty_cursor_wave_seeds_no_slot(tmp_path: Path, monkeypatch):
    """An empty fan-out is an idempotent no-op, down to not creating a directory.

    Uses the real-runner path (no ``runner=``), which is the only one that seeds
    at all; nothing spawns because both the preflight and the pool are guarded
    on there being jobs.
    """
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    seeded: list[tuple] = []
    monkeypatch.setattr(
        headless, "_seed_slot_config", lambda *args: seeded.append(args) or True
    )
    result = headless.run_headless_wave(
        [], model="grok-4.6", concurrency=3, cli="cursor",
    )
    assert result["counts"] == {"wrote": 0, "failed": 0, "todo": 0}
    assert seeded == []


def test_a_stub_runner_never_touches_the_cursor_config(tmp_path: Path, monkeypatch):
    """Unit tests must never reach the operator's ~/.cursor or seed a slot."""
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    seeded: list[tuple] = []
    monkeypatch.setattr(
        headless, "_seed_slot_config", lambda *args: seeded.append(args) or True
    )
    result = headless.run_headless_wave(
        _jobs(tmp_path, 3), model="grok-4.6", concurrency=2, cli="cursor",
        runner=lambda *a, **k: (0, _envelope("ok"), ""),
    )
    assert result["counts"]["wrote"] == 3
    assert seeded == []
    assert not (tmp_path / "slots").exists()


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


def test_cache_off_skips_the_warm_up(tmp_path: Path):
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


def _cursor_preflight_prober(models_out: str, reject: str = ""):
    """One prober for all three cursor probes: status, `models`, the model argv."""
    def probe(argv, *, env, cwd, timeout):
        if "models" in argv:
            return 0, models_out, ""
        if "status" in argv:
            return 0, json.dumps(_CURSOR_AUTHENTICATED), ""
        return 1, "", reject or "Error: No prompt provided for print mode"
    return probe


def test_preflight_error_rejects_a_model_the_cursor_cli_will_not_run(monkeypatch):
    """`run_headless_wave` applies this gate too, but only from inside the job —
    i.e. after the destructive `prepare`. Hoisted, a bogus id stops the estimate
    from ever going green instead of killing the wave one step later."""
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_default_auth_prober", _cursor_preflight_prober(
        "Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n",
        reject="Cannot use this model: bogus-xyz. Available models: auto, gpt-5.2",
    ))
    err = headless.preflight_error("cursor", model="bogus-xyz")
    assert err and "bogus-xyz" in err


def test_preflight_error_passes_a_model_the_cursor_cli_accepts(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_default_auth_prober", _cursor_preflight_prober(
        "Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n"
    ))
    assert headless.preflight_error("cursor", model="gpt-5.2") is None
    # And a model is never the reason a login failure gets misreported.
    assert headless.preflight_error("cursor") is None


def test_preflight_error_never_probes_a_model_on_claude(monkeypatch):
    """`--model` validation is a cursor-agent behaviour; asking claude would be a
    spawn (and a 30 s timeout) for a check it does not have."""
    seen: list[list[str]] = []

    def probe(argv, *, env, cwd, timeout):
        seen.append(list(argv))
        return 0, json.dumps(_AUTH_SUBSCRIPTION), ""

    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_default_auth_prober", probe)
    assert headless.preflight_error("claude", model="no-such-model") is None
    assert len(seen) == 1 and "--model" not in seen[0]


def test_preflight_error_reports_a_login_failure_before_a_model_one(monkeypatch):
    """Order matters: a logged-out CLI must read as logged out, not as a model
    problem it was never asked about."""
    def probe(argv, *, env, cwd, timeout):
        if "models" in argv:
            raise AssertionError("must not reach the model gate")
        return 0, json.dumps({**_CURSOR_AUTHENTICATED,
                              "status": "expired", "isAuthenticated": False}), ""

    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_default_auth_prober", probe)
    err = headless.preflight_error("cursor", model="bogus-xyz")
    assert err and "bogus-xyz" not in err


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


def test_the_model_probes_never_touch_the_operators_cursor_config(
    tmp_path: Path, monkeypatch
):
    """Both model probes spawn a worker-shaped ``-p --model`` argv.

    Run against the real ``~/.cursor``, either can rewrite ``selectedModel`` --
    the exact mutation per-worker config dirs exist to prevent. ``preflight_error``
    is the dashboard's path to them, so it has to isolate them too.
    """
    monkeypatch.setattr(headless.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(headless, "_slot_root", lambda: tmp_path / "slots")
    monkeypatch.setattr(headless, "_slot_free", [])
    monkeypatch.setattr(headless, "_slot_high", 0)
    monkeypatch.setattr(headless, "_slot_poisoned", set())
    monkeypatch.setattr(headless, "_slot_stats", {"seeded": 0, "unseeded": 0})
    monkeypatch.setattr(headless, "_slot_first_error", None)

    source = tmp_path / "cursor-home"
    source.mkdir()
    (source / "cli-config.json").write_text('{"selectedModel": {}}', encoding="utf-8")
    monkeypatch.setattr(headless, "_cursor_config_dir", lambda env: source)

    calls: list[tuple[list[str], dict]] = []

    def probe(argv, *, env, cwd, timeout):
        calls.append((list(argv), dict(env)))
        if "models" in argv:
            return 0, "Available models\n\nauto - Auto\n", ""
        if "status" in argv:
            return 0, json.dumps(_CURSOR_AUTHENTICATED), ""
        return 1, "", "Error: No prompt provided for print mode"

    monkeypatch.setattr(headless, "_default_auth_prober", probe)
    assert headless.preflight_error("cursor", model="grok-4.5") is None

    model_envs = [
        env for argv, env in calls if "models" in argv or "--model" in argv
    ]
    assert model_envs, "the model gate must actually have probed"
    slot_root = str(tmp_path / "slots")
    for env in model_envs:
        assert env.get("CURSOR_CONFIG_DIR", "").startswith(slot_root)
        assert str(source) != env.get("CURSOR_CONFIG_DIR")


def test_an_interrupt_still_books_the_jobs_that_finished(tmp_path: Path):
    """A killed wave must not punch holes in ``usage.jsonl``.

    The pool queues the whole wave up front, so on an interrupt the futures that
    had already landed were dropped from ``wrote``/``failed`` and from the log,
    even though their drafts were sitting on disk.
    """
    log = tmp_path / "usage.jsonl"
    calls: list[int] = []
    lock = threading.Lock()

    def runner(cmd, *, input_text, cwd):
        with lock:
            calls.append(1)
            nth = len(calls)
        if nth >= 3:
            raise KeyboardInterrupt("simulated Ctrl-C")
        return 0, _envelope("ok"), ""

    with pytest.raises(KeyboardInterrupt):
        headless.run_headless_wave(
            _jobs(tmp_path, 6),
            model="sonnet",
            concurrency=2,
            runner=runner,
            usage_log=log,
            warm_first=False,
        )

    rows = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) >= 2, "jobs that finished before the interrupt must be logged"
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "an interrupt must not double-book a finished job"
    assert all(row["rc"] == 0 for row in rows)


def test_an_interrupt_during_warm_up_still_kills_live_children(
    tmp_path: Path, monkeypatch
):
    """Warm-up used to sit outside the ``try``, so Ctrl-C on job 1 skipped cleanup."""
    killed: list[int] = []
    monkeypatch.setattr(headless, "_kill_live_processes", lambda: killed.append(1) or 0)

    def runner(cmd, *, input_text, cwd):
        raise KeyboardInterrupt("simulated Ctrl-C")

    with pytest.raises(KeyboardInterrupt):
        headless.run_headless_wave(
            _jobs(tmp_path, 4),
            model="sonnet",
            concurrency=3,
            runner=runner,
        )
    assert killed, "Ctrl-C during warm-up must still tree-kill live children"


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
