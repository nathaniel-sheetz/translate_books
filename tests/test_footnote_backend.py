"""Footnote-translation backend parity (api / headless / subagent).

Footnote bodies now translate on the *same* backend the user chose for the chapters
(carried forward via ``resolve_backend``). This covers:

- ``resolve_backend`` — explicit override, the ``backend`` run-log beat, the legacy
  ``subagent`` + ``fanout_mode: headless`` pairing, and the config-inference fallback.
- the **headless** one-shot engine (render batches → ``claude -p`` wave → commit),
  exercised through the ``run_headless_wave`` ``runner`` seam so no real ``claude`` runs.
- the **subagent** prepare/commit seam (Task workers write the drafts).
- the **api** fail-closed guard and the subagent guidance (neither spends here).
"""

import json
import re
import sys
from pathlib import Path

import pytest

from src.harness import flow, state
from src.footnote_import import (
    FootnoteRecord, write_footnotes_sidecar, load_footnotes_sidecar,
)


def _pending_note(n: int, body: str) -> FootnoteRecord:
    return FootnoteRecord(number=n, ref_marker=f"[{n}]", source_body=body, detected="backlink")


def _translated_note(n: int, body: str, translated: str) -> FootnoteRecord:
    return FootnoteRecord(number=n, ref_marker=f"[{n}]", source_body=body,
                          detected="backlink", translated_body=translated)


def _project_with_notes(tmp_path: Path, notes, cfg=None) -> Path:
    proj = tmp_path / "notesbook"
    proj.mkdir()
    write_footnotes_sidecar(proj, notes)
    state.save_config(proj, cfg or {})
    return proj


def _bodies(proj: Path) -> dict:
    return {n["number"]: n.get("translated_body") for n in load_footnotes_sidecar(proj)}


# ── resolve_backend ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("explicit", ["api", "subagent", "headless"])
def test_resolve_backend_explicit_wins(tmp_path, explicit):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "x")])
    assert flow.resolve_backend(proj, explicit) == explicit


def _patch_events(monkeypatch, events):
    monkeypatch.setattr("src.utils.run_logger.read_run_events", lambda **kw: list(events))


@pytest.mark.parametrize("value", ["api", "subagent", "headless"])
def test_resolve_backend_reads_last_backend_beat(tmp_path, monkeypatch, value):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "x")])
    _patch_events(monkeypatch, [
        {"event": "backend", "backend": "api"},
        {"event": "backend", "backend": value},  # last write wins
    ])
    assert flow.resolve_backend(proj) == value


def test_resolve_backend_legacy_subagent_plus_headless_fanout(tmp_path, monkeypatch):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "x")])
    _patch_events(monkeypatch, [
        {"event": "backend", "backend": "subagent"},
        {"event": "fanout_mode", "mode": "headless"},
    ])
    assert flow.resolve_backend(proj) == "headless"


def test_resolve_backend_infers_subagent_from_config(tmp_path, monkeypatch):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "x")], cfg={"worker_model": "sonnet"})
    _patch_events(monkeypatch, [])
    assert flow.resolve_backend(proj) == "subagent"


def test_resolve_backend_defaults_to_api(tmp_path, monkeypatch):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "x")])
    _patch_events(monkeypatch, [])
    assert flow.resolve_backend(proj) == "api"


# ── headless one-shot engine ────────────────────────────────────────────────

def _echo_runner(cmd, *, input_text, cwd):
    """Stub ``claude -p``: echo ``N| ES-N`` for each source note in the prompt."""
    out, in_notes = [], False
    for line in input_text.splitlines():
        if line.strip() == "FOOTNOTES:":
            in_notes = True
            continue
        if in_notes:
            m = re.match(r"^(\d+)\|", line)
            if m:
                out.append(f"{m.group(1)}| ES-{m.group(1)}")
    return 0, "\n".join(out), ""


def test_footnotes_headless_fills_pending_only(tmp_path):
    proj = _project_with_notes(tmp_path, [
        _pending_note(1, "First note."),
        _translated_note(2, "Second.", "Ya."),
    ])
    result = flow.footnotes_translate(str(proj), backend="headless", runner=_echo_runner)
    assert result["backend"] == "headless"
    assert result["committed"] == [1]
    bodies = _bodies(proj)
    assert bodies[1] == "ES-1"
    assert bodies[2] == "Ya."  # already-translated note untouched


def test_footnotes_headless_no_yes_required(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "A note.")])
    result = flow.footnotes_translate(str(proj), backend="headless", yes=False, runner=_echo_runner)
    assert result.get("exit_code") == 0
    assert _bodies(proj)[1] == "ES-1"


def test_footnotes_headless_idempotent_noop(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "A note.")])
    flow.footnotes_translate(str(proj), backend="headless", runner=_echo_runner)

    def _boom(*a, **k):
        raise AssertionError("runner must not run when nothing is pending")

    result = flow.footnotes_translate(str(proj), backend="headless", runner=_boom)
    assert "already translated" in result.get("note", "")


def test_footnotes_headless_retranslate_refills(tmp_path):
    proj = _project_with_notes(tmp_path, [_translated_note(1, "A note.", "Old.")])
    result = flow.footnotes_translate(str(proj), backend="headless",
                                      retranslate=True, runner=_echo_runner)
    assert result["committed"] == [1]
    assert _bodies(proj)[1] == "ES-1"


def test_footnotes_headless_missing_claude_reports_error_no_spend(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "A note.")])
    # runner=None + a bogus binary -> run_headless_wave fails fast, nothing translated.
    result = flow.footnotes_translate(str(proj), backend="headless",
                                      claude_bin="definitely-not-a-real-binary-xyz")
    assert "error" in result and "not found" in result["error"].lower()
    assert result["exit_code"] == 1  # a hard failure, not a silent success
    assert _bodies(proj)[1] in (None, "")


# ── subagent prepare / commit seam ──────────────────────────────────────────

def test_footnotes_prepare_renders_batches_and_manifest(tmp_path):
    proj = _project_with_notes(tmp_path, [
        _pending_note(1, "First."),
        _translated_note(2, "Second.", "Ya."),
    ])
    result = flow.footnotes_translate_prepare(str(proj))
    assert result["exit_code"] == 0
    assert result["pending"] == 1 and result["total"] == 2
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["numbers"] == [1]
    assert Path(entry["prompt_path"]).exists()
    manifest = json.loads((proj / ".harness" / "footnotes" / "manifest.json").read_text("utf-8"))
    assert manifest["entries"][0]["numbers"] == [1]


def test_footnotes_commit_parses_drafts_into_sidecar(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First."), _pending_note(2, "Second.")])
    prep = flow.footnotes_translate_prepare(str(proj))
    for entry in prep["entries"]:  # simulate translator subagents writing drafts
        lines = "\n".join(f"{num}| Nota {num}." for num in entry["numbers"])
        Path(entry["draft_path"]).write_text(lines + "\n", encoding="utf-8")
    result = flow.footnotes_translate_commit(str(proj))
    assert result["exit_code"] == 0
    assert result["committed"] == [1, 2]
    bodies = _bodies(proj)
    assert bodies[1] == "Nota 1." and bodies[2] == "Nota 2."


def test_footnotes_commit_missing_draft_leaves_pending(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    flow.footnotes_translate_prepare(str(proj))  # no draft written
    result = flow.footnotes_translate_commit(str(proj))
    assert result["pending"] == [1] and result["committed"] == []
    assert _bodies(proj)[1] in (None, "")


def test_footnotes_commit_without_manifest_errors(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    result = flow.footnotes_translate_commit(str(proj))
    assert "error" in result
    assert result["exit_code"] == 1  # a real precondition failure, not a no-op


def test_prepare_commit_returns_carry_exit_code(tmp_path):
    """`footnotes` is a streaming command, so `main()` unconditionally reads
    ``result["exit_code"]``. Every prepare/commit return branch must carry it, or
    the CLI dies with ``KeyError: 'exit_code'`` before printing anything."""
    empty = tmp_path / "empty"
    empty.mkdir()
    state.save_config(empty, {})
    # prepare with no sidecar (the no-op branch) still carries exit_code 0.
    noop = flow.footnotes_translate_prepare(str(empty))
    assert noop["exit_code"] == 0 and noop["command"] == "footnotes"

    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    prep = flow.footnotes_translate_prepare(str(proj))
    assert prep["exit_code"] == 0 and prep["command"] == "footnotes"
    # commit on an unreadable manifest is a hard failure -> exit 1.
    (proj / ".harness" / "footnotes" / "manifest.json").write_text("{ not json", "utf-8")
    bad = flow.footnotes_translate_commit(str(proj))
    assert bad["exit_code"] == 1 and "unreadable" in bad["error"]


def _run_cli(monkeypatch, *argv) -> int:
    """Drive the real ``scripts.harness.main()`` streaming branch and return the exit
    code. This is the layer the flow-level tests bypass — where the missing
    ``exit_code`` key crashed with ``KeyError`` before printing anything."""
    import scripts.harness as harness
    monkeypatch.setattr(sys, "argv", ["harness.py", *argv])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    code = exc.value.code
    return 0 if code is None else int(code)


def test_footnotes_prepare_commit_end_to_end_via_main(tmp_path, monkeypatch):
    """The subagent seam driven through ``main()`` (not the flow functions directly):
    prepare -> workers write drafts -> commit, each exiting 0 and leaving a fresh
    ``last_output.json`` carrying ``command``/``exit_code``."""
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First."), _pending_note(2, "Second.")])
    artifact = proj / ".harness" / "last_output.json"

    assert _run_cli(monkeypatch, "footnotes", "translate-prepare", "--project", str(proj)) == 0
    out = json.loads(artifact.read_text(encoding="utf-8"))
    assert out["command"] == "footnotes" and out["exit_code"] == 0

    manifest = json.loads((proj / ".harness" / "footnotes" / "manifest.json").read_text("utf-8"))
    for entry in manifest["entries"]:  # simulate translator subagents writing drafts
        lines = "\n".join(f"{num}| Nota {num}." for num in entry["numbers"])
        Path(entry["draft_path"]).write_text(lines + "\n", encoding="utf-8")

    assert _run_cli(monkeypatch, "footnotes", "translate-commit", "--project", str(proj)) == 0
    out = json.loads(artifact.read_text(encoding="utf-8"))
    assert out["command"] == "footnotes" and out["exit_code"] == 0
    assert out["committed"] == [1, 2]
    assert _bodies(proj) == {1: "Nota 1.", 2: "Nota 2."}


def test_footnotes_commit_without_manifest_exits_1_via_main(tmp_path, monkeypatch):
    """The regressed path: ``footnotes translate-commit`` with no manifest used to
    crash with ``KeyError: 'exit_code'`` in ``main()``. It must now exit 1 cleanly."""
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    assert _run_cli(monkeypatch, "footnotes", "translate-commit", "--project", str(proj)) == 1
    out = json.loads((proj / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["command"] == "footnotes" and out["exit_code"] == 1
    assert "error" in out


# ── guards ──────────────────────────────────────────────────────────────────

def test_footnotes_translate_subagent_returns_guidance_no_spend(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    result = flow.footnotes_translate(str(proj), backend="subagent")
    assert result["backend"] == "subagent"
    assert result["action_required"] == "prepare-commit"
    assert _bodies(proj)[1] in (None, "")  # nothing translated here


def test_footnotes_translate_api_fails_closed_without_yes(tmp_path):
    proj = _project_with_notes(tmp_path, [_pending_note(1, "First.")])
    assert flow.footnotes_translate(str(proj), backend="api", yes=False) == 2


def test_footnotes_translate_noop_without_sidecar(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    state.save_config(proj, {})
    result = flow.footnotes_translate(str(proj), backend="headless")
    assert result["total"] == 0
    assert "no footnotes.json" in result.get("note", "")
