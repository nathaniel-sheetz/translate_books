"""Architectural guard: headless CLIs may only be spawned by one module.

Headless fan-out must always bill the user's Claude subscription, never metered
API credit. That guarantee lives in ``src/harness/headless.py``, which scrubs
every provider credential out of the child environment and runs an auth
preflight before any job starts. A ``subprocess`` call anywhere else that
launches ``claude`` / ``cursor-agent`` would bypass both and silently re-open
metered billing — which is exactly how the original leak happened.

These tests do not read intent; they pin the inventory. Adding a spawn means
editing ``_KNOWN_SPAWNS`` below, which is the point: the edit forces you to read
the rule.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from posixpath import splitext

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCANNED_ROOTS = ("src", "scripts", "web_ui")

# The one module allowed to launch a headless CLI.
_SANCTIONED = "src/harness/headless.py"

_SUBPROCESS_SPAWNS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput",
})
_OS_SPAWNS = frozenset({
    "system", "popen", "execv", "execve", "execvp", "execvpe",
    "spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "posix_spawn",
})

# Every process spawn in the scanned tree, with what it launches. A new entry is
# a deliberate act: if it launches a headless CLI it MUST go through
# `run_headless_wave` instead.
_KNOWN_SPAWNS: frozenset[tuple[str, str]] = frozenset({
    ("src/harness/flow.py", "subprocess.Popen"),                 # sys.executable, repo scripts
    ("src/harness/headless.py", "subprocess.run"),               # THE sanctioned CLI spawn + auth probe
    ("src/judges/runner.py", "subprocess.run"),                  # git rev-parse
    ("scripts/compare_models.py", "subprocess.check_output"),    # git rev-parse
})

# argv[0] stems that mean "this is a headless CLI launch".
_HEADLESS_CLI_STEMS = frozenset({"claude", "cursor-agent", "cursor"})
_EXE_SUFFIXES = (".cmd", ".exe", ".bat", ".ps1", ".sh")


def _is_tracked(path: Path) -> bool:
    """Skip gitignored / untracked local-only files so CI and local agree.

    ``scripts/run_eval_test.py`` and ``scripts/experiments/`` are gitignored;
    listing them in ``_KNOWN_SPAWNS`` passes locally and fails on CI (stale),
    while omitting them fails locally (new). Only inventory what the repo ships.
    """
    try:
        rc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        ).returncode
    except OSError:
        return True  # no git → scan everything
    return rc == 0


def _iter_spawn_calls():
    """Yield ``(rel_path, callee, lineno, node)`` for every process spawn."""
    for root in _SCANNED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if not _is_tracked(path):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            # utf-8-sig: src/difficulty_scorer.py carries a BOM, and a plain
            # utf-8 read makes ast.parse choke on U+FEFF.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                    continue
                module, attr = func.value.id, func.attr
                if module == "subprocess" and attr in _SUBPROCESS_SPAWNS:
                    yield rel, f"subprocess.{attr}", node.lineno, node
                elif module == "os" and attr in _OS_SPAWNS:
                    yield rel, f"os.{attr}", node.lineno, node


def _argv0_literal(node: ast.Call) -> str | None:
    """The first positional argument as a string literal, if it is one."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        if not first.elts:
            return None
        first = first.elts[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def test_no_new_process_spawns_outside_the_inventory():
    found = {(rel, callee) for rel, callee, _, _ in _iter_spawn_calls()}
    locations = {
        (rel, callee): lineno for rel, callee, lineno, _ in _iter_spawn_calls()
    }

    new = found - _KNOWN_SPAWNS
    assert not new, (
        "New process spawn(s): "
        + ", ".join(f"{rel}:{locations[(rel, callee)]} ({callee})" for rel, callee in sorted(new))
        + f". If it launches a headless CLI it MUST go through {_SANCTIONED} "
        "(run_headless_wave), which scrubs metered credentials from the child "
        "env and runs the subscription preflight. Otherwise add it to "
        "_KNOWN_SPAWNS in tests/test_spawn_boundary.py."
    )

    stale = _KNOWN_SPAWNS - found
    assert not stale, (
        f"_KNOWN_SPAWNS lists spawns that no longer exist: {sorted(stale)}. "
        "Remove them so the inventory keeps its teeth."
    )


def test_no_module_but_the_launcher_names_a_headless_cli():
    """Catch the literal regression: `subprocess.run(["claude", "-p", ...])`.

    Redundant with the inventory today, but it survives someone allowlisting a
    new spawn without thinking, and it produces the message that explains why.
    """
    offenders = []
    for rel, callee, lineno, node in _iter_spawn_calls():
        if rel == _SANCTIONED:
            continue
        argv0 = _argv0_literal(node)
        if argv0 is None:
            continue
        stem = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
        root, ext = splitext(stem)
        if ext in _EXE_SUFFIXES:
            stem = root
        if stem in _HEADLESS_CLI_STEMS:
            offenders.append(f"{rel}:{lineno} ({callee} -> {argv0!r})")

    assert not offenders, (
        "Headless CLI launched outside the sanctioned launcher: "
        + "; ".join(offenders)
        + f". Route it through {_SANCTIONED}::run_headless_wave — a direct spawn "
        "inherits ANTHROPIC_API_KEY from the parent and bills metered credit."
    )


def test_the_launcher_never_detects_a_host():
    """Host detection must stay out of the launcher's import graph.

    ``CLAUDECODE`` survives the credential scrub on purpose (``_SCRUB_KEEP``), so
    a ``cursor-agent`` worker spawned from a Claude Code parent inherits it — and
    a ``claude -p`` worker spawned from a Cursor host inherits ``CURSOR_*``.
    Anything asking "who is my host?" from inside a worker therefore gets its
    *parent's* answer, in both directions.

    The rule ("only the orchestrator may detect") is enforced structurally rather
    than by memory: :mod:`src.harness.headless` and :mod:`src.harness.usage` run
    in the spawning process, so they must not be able to reach
    :mod:`src.harness.host` at all. :mod:`src.harness.profile` is where detection
    is allowed to live, and nothing in the wave path imports it.
    """
    offenders: list[str] = []
    for rel in ("src/harness/headless.py", "src/harness/usage.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Name) and node.id == "detect_host":
                offenders.append(f"{rel}:{node.lineno} names detect_host")
                continue
            else:
                continue
            for name in names:
                if name.endswith("harness.host") or name.endswith("harness.profile"):
                    offenders.append(f"{rel}:{node.lineno} imports {name}")

    assert not offenders, (
        "The headless launcher must not detect a host: "
        + "; ".join(offenders)
        + ". A spawned worker inherits its parent's CLAUDECODE / CURSOR_* markers "
        "(CLAUDECODE is kept by the credential scrub deliberately), so detection "
        "inside the wave path answers for the wrong process. Resolve the profile "
        "in the orchestrator via src/harness/profile.py and pass the result down."
    )


def test_sanctioned_launcher_scrubs_and_preflights():
    """The inventory is only meaningful if the sanctioned module still enforces."""
    from src.harness import headless

    env = headless.subscription_env("claude", base={"ANTHROPIC_API_KEY": "sk-x", "PATH": "/x"})
    assert env == {"PATH": "/x"}
    assert callable(headless.subscription_auth_error)
