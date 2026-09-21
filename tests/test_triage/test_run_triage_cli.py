"""Tests for ``scripts/run_triage.py``'s exit codes.

The script is the only surface the ``/triage-review`` skill drives, and the skill
branches on the shell's answer rather than on the JSON: a clean scope is the
normal state of a book that has been triaged once, and mistaking it for a broken
CLI is what sends an operator looking for a fault that is not there. So the one
contract worth pinning here is the difference the skill's own gotcha names --
``prepare`` exits 1 on a clean scope, ``status`` exits 0 -- and that a book that
does not exist is 1 rather than the 0 a ``SystemExit`` carrying a string would
have produced.

Driven through ``main(argv)`` rather than a subprocess: the return value *is* the
exit code (``__main__`` only wraps it in ``SystemExit``), and a subprocess here
would load the native spellchecker for no reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_triage import main


@pytest.fixture(autouse=True)
def no_cli_probe(monkeypatch):
    """``status`` preflights the CLI by default; nothing here may spawn one."""
    import src.harness.headless as headless

    monkeypatch.setattr(headless, "preflight_error", lambda cli, **kw: None)


@pytest.fixture
def clean(tmp_path: Path) -> Path:
    """A book with an alignment and nothing left to triage."""
    project = tmp_path / "clean"
    (project / "alignments").mkdir(parents=True)
    return project


def test_a_clean_scope_is_success_for_status_and_failure_for_prepare(clean, capsys):
    assert main(["status", "--project", str(clean)]) == 0
    assert json.loads(capsys.readouterr().out)["triageable"] == 0

    assert main(["prepare", "--project", str(clean)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "nothing_to_triage"


def test_status_answers_in_the_documented_schema(clean, capsys):
    """What the skill and the dashboard both read, so it is not free to drift."""
    assert main(["status", "--project", str(clean)]) == 0
    out = json.loads(capsys.readouterr().out)

    import src.triage.pass_ as tp

    assert set(tp._STATUS_SCHEMA) <= set(out)


def test_a_book_that_does_not_exist_exits_1(tmp_path: Path, capsys):
    """A ``SystemExit`` carrying a string prints it and exits **0**, which would
    make a missing book indistinguishable from a successful run."""
    assert main(["status", "--project", str(tmp_path / "nope")]) == 1
    assert json.loads(capsys.readouterr().out).get("error")
