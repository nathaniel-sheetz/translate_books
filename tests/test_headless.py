"""Unit tests for the shared headless CLI launcher (claude + cursor profiles)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.harness import headless


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
    assert "--output-format" in cmd and "text" in cmd
    assert "--model" in cmd and "sonnet" in cmd


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


def test_extract_output_cursor_unwraps_json_result_envelope():
    raw = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "  final answer  ",
        "session_id": "abc",
    })
    assert headless._extract_output("cursor", raw) == "final answer"
    assert headless._extract_output("cursor", "  plain prose  ") == "plain prose"
    assert headless._extract_output("claude", raw) == raw.strip()


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


def test_subscription_auth_error_rejects_oauth_token_without_env():
    err = headless.subscription_auth_error(
        "claude", "claude", {}, cwd=".", prober=_prober(_AUTH_OAUTH_TOKEN)
    )
    assert err and "could not confirm a subscription login" in err


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
