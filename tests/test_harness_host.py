"""Host detection: which coding agent is driving the harness.

The signal set is deliberately narrow, and the tests that matter most are the
*negative* ones — the 2026-08-11 session that motivated this ran Claude Code
inside Cursor's integrated terminal, so every editor-shaped signal pointed at
Cursor while the host was Claude Code.
"""

from __future__ import annotations

from src.harness.host import (
    HOST_CLAUDE_CODE,
    HOST_CURSOR,
    HOST_OVERRIDE_ENV,
    HOST_UNKNOWN,
    detect_host,
    host_cli,
)

# The real environment of a Claude Code session running in Cursor's terminal,
# captured 2026-08-11. Every Cursor-looking key here is a decoy.
_CLAUDE_CODE_INSIDE_CURSOR = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "AI_AGENT": "claude-code_2-1-227_agent",
    "TERM_PROGRAM": "vscode",
    "TERM_PROGRAM_VERSION": "3.15.6",
    "VSCODE_GIT_ASKPASS_NODE": r"C:\Users\x\AppData\Local\Programs\cursor\Cursor.exe",
    "GIT_ASKPASS": r"c:\Users\x\AppData\Local\Programs\cursor\resources\app\...",
    "PATH": r"C:\Users\x\AppData\Local\Programs\cursor\resources\app\bin;C:\Users\x\AppData\Local\cursor-agent",
}


def test_detects_claude_code_from_its_own_markers():
    assert detect_host({"CLAUDECODE": "1"}) == HOST_CLAUDE_CODE
    assert detect_host({"CLAUDE_CODE_ENTRYPOINT": "cli"}) == HOST_CLAUDE_CODE


def test_detects_cursor_from_each_agent_marker_alone():
    for marker in (
        "CURSOR_AGENT",
        "CURSOR_CONVERSATION_ID",
        "CURSOR_INVOKED_AS",
        "CURSOR_AGENT_STORE",
    ):
        assert detect_host({marker: "1"}) == HOST_CURSOR, marker


def test_editor_and_path_signals_are_not_host_signals():
    """The decoy case: Claude Code hosted inside Cursor's terminal.

    TERM_PROGRAM=vscode, a Cursor.exe askpass, and cursor-agent on PATH all say
    "Cursor" and all are wrong. Detection must read per-process agent markers
    only — and with the real marker removed the answer is `unknown`, NOT
    `cursor`, because guessing here picks the wrong subscription.
    """
    assert detect_host(_CLAUDE_CODE_INSIDE_CURSOR) == HOST_CLAUDE_CODE

    decoys_only = {
        k: v for k, v in _CLAUDE_CODE_INSIDE_CURSOR.items()
        if not k.startswith("CLAUDECODE") and not k.startswith("CLAUDE_CODE")
    }
    assert detect_host(decoys_only) == HOST_UNKNOWN


def test_cursor_api_key_is_a_credential_not_a_host_signal():
    """Anyone can export CURSOR_API_KEY in any shell; it says nothing about who runs."""
    assert detect_host({"CURSOR_API_KEY": "sk-whatever"}) == HOST_UNKNOWN


def test_claude_marker_wins_when_a_worker_inherited_both():
    """A cursor-agent worker spawned by a Claude Code parent carries both families.

    CLAUDECODE survives the credential scrub on purpose, so this shape is real —
    but only inside a *worker*, which must never ask (see the module docstring
    and tests/test_spawn_boundary.py). Resolving to the parent is the
    conservative answer if anyone ever does.
    """
    both = {"CLAUDECODE": "1", "CURSOR_AGENT": "1"}
    assert detect_host(both) == HOST_CLAUDE_CODE


def test_override_wins_and_a_bogus_override_is_ignored():
    env = {"CLAUDECODE": "1", HOST_OVERRIDE_ENV: "cursor"}
    assert detect_host(env) == HOST_CURSOR

    # A typo must degrade to real detection, never to a wrong host.
    assert detect_host({"CLAUDECODE": "1", HOST_OVERRIDE_ENV: "cusror"}) == HOST_CLAUDE_CODE
    assert detect_host({HOST_OVERRIDE_ENV: "cusror"}) == HOST_UNKNOWN


def test_blank_values_count_as_absent():
    assert detect_host({"CLAUDECODE": ""}) == HOST_UNKNOWN
    assert detect_host({"CURSOR_AGENT": "   "}) == HOST_UNKNOWN


def test_empty_environment_is_unknown():
    assert detect_host({}) == HOST_UNKNOWN


def test_never_raises_on_a_hostile_environment():
    class Hostile(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    assert detect_host(Hostile()) == HOST_UNKNOWN


def test_host_cli_maps_only_the_two_known_hosts():
    assert host_cli(HOST_CLAUDE_CODE) == "claude"
    assert host_cli(HOST_CURSOR) == "cursor"
    # `None` means "detection has no opinion" so the caller falls back to its own
    # default rather than to a coin flip.
    assert host_cli(HOST_UNKNOWN) is None
    assert host_cli("") is None
    assert host_cli("emacs") is None
