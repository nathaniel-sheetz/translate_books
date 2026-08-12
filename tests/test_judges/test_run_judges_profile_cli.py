"""Tests for ``run_judges.py profile`` — "what would this wave run as?".

The command exists so the CLI/preset question can be asked *at* the gate. Before
it, the first command that revealed the resolved worker model was ``prepare`` —
which had already written that model into the manifest and cleared the drafts,
so correcting it meant a destructive re-``prepare``. Two of the three 2026-08-11
judge-review friction logs are that loop.

The load-bearing property is that it is inert: read-only, no renders, no
subprocess.
"""

from __future__ import annotations

import json

import pytest

from src.harness import state as hstate

run_judges = pytest.importorskip("scripts.run_judges")


@pytest.fixture
def project(tmp_path):
    """A minimal book: one translated chunk and a config, nothing judged."""
    proj = tmp_path / "projects" / "profiletest"
    (proj / "chunks").mkdir(parents=True)
    (proj / "chapters").mkdir(parents=True)
    (proj / "chapters" / "chapter_01.txt").write_text("El gato.", encoding="utf-8")
    (proj / "chunks" / "chapter_01_chunk_000.json").write_text(
        json.dumps({
            "id": "chapter_01_chunk_000", "chapter_id": "chapter_01", "position": 0,
            "source_text": "The black cat.", "translated_text": "El gato negro.",
        }),
        encoding="utf-8",
    )
    hstate.save_config(proj, dict(hstate.DEFAULTS))
    return proj


def _run(capsys, *args) -> dict:
    rc = run_judges.main(["profile", *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_prints_the_effective_block_and_touches_nothing(project, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    out = _run(capsys, "--project", str(project))

    assert out["status"] == "ok"
    assert out["command"] == "judges"
    assert out["host"] == "claude-code"
    eff = out["effective"]
    assert eff["cli"] == "claude" and eff["cli_source"] == "host:claude-code"
    assert eff["worker_model"] == "sonnet"
    assert eff["effort"] == "medium" and eff["effort_channel"] == "argv"
    assert eff["baseline_tokens"] == 3900

    # Inert: no manifest, no prompts, no drafts — nothing under .harness/judges.
    assert not (project / ".harness" / "judges").exists()


def test_the_same_book_resolves_differently_per_host(project, capsys, monkeypatch):
    """The whole point: the harness follows the agent that is driving it."""
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    claude = _run(capsys, "--project", str(project))["effective"]

    monkeypatch.setenv("HARNESS_HOST", "cursor")
    cursor = _run(capsys, "--project", str(project), "--cli", "cursor")["effective"]

    assert claude["cli"] == "claude" and cursor["cli"] == "cursor"
    assert claude["baseline_tokens"] != cursor["baseline_tokens"]
    assert cursor["worker_model"] != "sonnet"


def test_previews_exactly_what_prepare_would_resolve(project, capsys, monkeypatch):
    """A preview that disagreed with the real thing would be worse than none."""
    monkeypatch.setenv("HARNESS_HOST", "cursor")
    from src.harness.profile import resolve_profile

    previewed = _run(
        capsys, "--project", str(project), "--cli", "cursor", "--effort", "high"
    )["effective"]
    resolved = resolve_profile(
        project, command="judges", cli="cursor", effort="high", check_binary=False
    ).to_payload()

    # check_binary aside (the preview reports a fallback the resolver was told to
    # skip), every knob the operator consents to must match.
    for key in ("worker_model", "effort", "effort_channel", "baseline_tokens"):
        assert previewed[key] == resolved[key], key


def test_candidate_flags_do_not_persist_anything(project, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    _run(capsys, "--project", str(project), "--cli", "cursor", "--effort", "xhigh")
    assert hstate.load_config(project)["headless_cli"] == "auto"
    assert hstate.load_config(project)["headless_effort_judges"] == "auto"


def test_each_wave_type_is_resolvable(project, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    for command in ("judges", "annotations", "translate", "footnotes"):
        out = _run(capsys, "--project", str(project), "--command", command)
        assert out["command"] == command
        assert out["effective"]["command"] == command
    # translate/footnotes run at a different band than the review-shaped waves.
    translate = _run(capsys, "--project", str(project), "--command", "translate")
    assert translate["effective"]["effort"] == "high"


def test_command_flag_does_not_shadow_the_subcommand(project, capsys, monkeypatch):
    """`--command` must not overwrite argparse's own subcommand dest."""
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    out = _run(capsys, "--project", str(project), "--command", "translate")
    assert out["status"] == "ok"


def test_schema_is_opt_in(project, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_HOST", "claude-code")
    out = _run(capsys, "--project", str(project))
    assert "_schema" not in out and "_schema_hint" in out

    run_judges.main(["profile", "--project", str(project), "--schema"])
    with_schema = json.loads(capsys.readouterr().out)
    assert "effective" in with_schema["_schema"]
