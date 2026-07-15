"""Token-free integration test for the translate-harness pipeline spine.

D7 (eng review 2026-06-05): the harness orchestration lives in SKILL.md (agent
behavior) and cannot be unit-tested without an LLM. What CAN be tested offline is the
deterministic spine the agent drives. This test stubs the single LLM seam and runs
chunk -> translate -> combine -> epub on a tiny fixture, asserting an EPUB is produced
and every intermediate artifact validates against the Pydantic models.

Deliberately omitted:
  - ingest/split: replaced by pre-placing a chapter file (no Gutenberg fetch).
  - align: loads a sentence-transformers embedding model — too heavy for an offline CI
    test. The reader-mode alignment is covered by its own suite (test_sentence_aligner).
  - agent approval gates / draft quality: SKILL.md prose, verified by manual dogfood.

    FIXTURE TEXT ─► chunk ─► translate(stubbed) ─► combine ─► epub
                     │           │                    │          │
                     ▼           ▼                    ▼          ▼
                  chunks/*    translated_text     chapters/    *.epub
                  validate    set, no API         *.txt        valid zip
"""

import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.translate_book as tb
import src.api_translator as api
from src.harness_guard import (
    validate_chunk_file,
    validate_glossary_file,
    validate_style_guide_file,
)
from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    Glossary,
    GlossaryTerm,
    StyleGuide,
)
from src.utils.file_io import load_chunk, save_chunk, save_glossary, save_style_guide

FIXTURE_TEXT = """The sun rose over the quiet village.

Old Thomas walked to the well every morning. He greeted his neighbors warmly.

The children played near the old oak tree until dusk. It was a peaceful place.
"""


def _fake_translate(chunk, **kwargs):
    """Stand in for translate_chunk_realtime — mark the chunk translated, no API call."""
    chunk.translated_text = "[ES] " + chunk.source_text
    chunk.status = ChunkStatus.TRANSLATED
    chunk.translated_at = datetime.now()
    return chunk


def _fake_draft(source_text: str) -> str:
    """Fake translated prose for subagent tests.

    Each English token is prefixed with 'es_' so Jaccard overlap with the
    source is ~0 and the near-verbatim echo guard does not fire, while the
    word count stays the same so the length evaluator passes.
    """
    return " ".join(f"es_{w.lower().rstrip('.,!?;:')}" for w in source_text.split())


_FAKE_COST_USD = 1.23  # non-zero so the cost-gate/confirmation path is exercised


def _fake_estimate_cost(chunks, provider, model, **kwargs):
    """Stand in for estimate_cost — avoid coupling to llm_config pricing tables."""
    n = len(chunks)
    return {
        "input_tokens": 100 * n,
        "output_tokens_estimate": 100 * n,
        "cost_usd": _FAKE_COST_USD,
        "cost_per_chunk_usd": _FAKE_COST_USD / n if n else 0.0,
        "batch_discount_applied": False,
    }


def _args() -> SimpleNamespace:
    # Mirrors the non-interactive approved args the SKILL.md must pass after its own
    # AskUserQuestion approval gate (T2 — no agent deadlock).
    return SimpleNamespace(
        chunk_size=2000, overlap_paragraphs=0, min_overlap_words=0,
        provider="anthropic", model="claude-sonnet-4-6",
        chapters=None, cost_only=False, yes=True,
        project_name="Test Book", author="Tester",
        target_lang="Spanish", source_lang="English",
        target_lang_code="es", source_lang_code="en",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "chapter_01.txt").write_text(FIXTURE_TEXT, encoding="utf-8")
    # Agent-drafted artifacts the translate stage will load.
    save_glossary(Glossary(terms=[GlossaryTerm(english="Thomas", spanish="Tomás")]),
                  tmp_path / "glossary.json")
    save_style_guide(StyleGuide(content="TONE: warm and simple"), tmp_path / "style.json")
    # Stub the one LLM seam + the pricing-coupled estimator.
    monkeypatch.setattr(api, "translate_chunk_realtime", _fake_translate)
    monkeypatch.setattr(api, "estimate_cost", _fake_estimate_cost)
    return tmp_path


def test_pipeline_spine_produces_valid_epub(project: Path):
    args = _args()
    state: dict = {}
    for stage in ("chunk", "translate", "combine", "epub"):
        state = tb.STAGE_FUNCTIONS[stage](args, project, state)

    # 1. Agent-drafted inputs pass the guard.
    validate_glossary_file(project / "glossary.json")
    validate_style_guide_file(project / "style.json")

    # 2. Every chunk validates against the model AND is translated.
    chunk_files = list((project / "chunks").glob("*_chunk_*.json"))
    assert chunk_files, "no chunks produced"
    for cf in chunk_files:
        chunk = validate_chunk_file(cf)
        assert chunk.has_translation, f"{cf.name} not translated"

    # 3. Combined chapter text was written.
    combined = project / "chapters" / "chapter_01.txt"
    assert combined.exists() and combined.read_text(encoding="utf-8").strip()

    # 4. EPUB produced and is a structurally valid zip with rendered content.
    epubs = list(project.rglob("*.epub"))
    assert epubs, "no EPUB produced"
    with zipfile.ZipFile(epubs[0]) as z:
        bad = z.testzip()
        assert bad is None, f"corrupt entry in EPUB: {bad}"
        names = z.namelist()
        assert "mimetype" in names
        assert any(n.endswith((".xhtml", ".html")) for n in names), "no rendered chapter in EPUB"

    # 5. The pipeline recorded completion.
    assert state["stage_completed"] == "epub"


def test_stage_chunk_applies_per_chapter_sizes(tmp_path: Path):
    """stage_chunk reads --chunk-sizes and sizes each chapter independently,
    falling back to --chunk-size for chapters absent from the map."""
    import json as _json

    chapters = tmp_path / "chapters"
    chapters.mkdir()
    # ~1560 words / 6 paragraphs: one chunk at target 2000, several at 400.
    para = ("The travelers crossed the wide green valley before the long rains came "
            "and rested by the river where the tall reeds bent in the steady wind. ") * 10
    big_chapter = "\n\n".join(para.strip() for _ in range(6))
    (chapters / "chapter_01.txt").write_text(big_chapter, encoding="utf-8")
    (chapters / "chapter_02.txt").write_text(big_chapter, encoding="utf-8")

    sizes_path = tmp_path / "chunk_sizes.json"
    sizes_path.write_text(_json.dumps({"chapter_01": 400}), encoding="utf-8")  # ch2 omitted

    args = SimpleNamespace(chunk_size=2000, overlap_paragraphs=0,
                           min_overlap_words=0, chunk_sizes=str(sizes_path))
    tb.STAGE_FUNCTIONS["chunk"](args, tmp_path, {})

    ch1 = list((tmp_path / "chunks").glob("chapter_01_chunk_*.json"))
    ch2 = list((tmp_path / "chunks").glob("chapter_02_chunk_*.json"))
    assert len(ch1) > 1, "chapter_01 (target 400) should split into multiple chunks"
    assert len(ch2) == 1, "chapter_02 (fallback target 2000) should stay one chunk"


def test_translate_stage_never_blocks_on_input(project: Path, monkeypatch):
    """T2 guard: with explicit approval, stage_translate must not call input()."""
    def _boom(*_a, **_k):
        raise AssertionError("stage_translate called input() — would deadlock an agent")

    monkeypatch.setattr("builtins.input", _boom)
    args = _args()
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)
    state = tb.STAGE_FUNCTIONS["translate"](args, project, state)  # must not raise
    assert state["stage_completed"] == "translate"


def test_cost_only_exits_before_confirmation(project: Path, monkeypatch):
    """The pure estimator path never prompts and never translates."""
    def _boom(*_a, **_k):
        raise AssertionError("stage_translate called input() in --cost-only mode")

    monkeypatch.setattr("builtins.input", _boom)
    args = _args()
    args.cost_only = True
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    with pytest.raises(SystemExit) as exc:
        tb.STAGE_FUNCTIONS["translate"](args, project, state)

    assert exc.value.code == 0


def test_cost_only_estimate_is_backend_neutral(project: Path, capsys):
    """The estimator path must not present the dollar figure as *the* cost.

    On the subagent backend there is no metered API spend (friction-log #9), and the
    backend isn't chosen yet when `chunk`/`cost` run — so the estimate frames the API
    price as conditional and notes the subagent backend is free, rather than printing
    an unconditional "Estimated cost: $...".
    """
    args = _args()
    args.cost_only = True
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    with pytest.raises(SystemExit) as exc:
        tb.STAGE_FUNCTIONS["translate"](args, project, state)
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "If translated via the metered API" in out
    assert "Subagent backend uses your subscription" in out
    # The unconditional API-cost lead-in belongs only to the paid translate run.
    assert "Estimated cost:" not in out


def test_unapproved_noninteractive_translate_exits_without_prompt(project: Path, monkeypatch):
    """A non-interactive paid run must fail closed unless --yes was supplied."""
    def _boom(*_a, **_k):
        raise AssertionError("stage_translate called input() in non-interactive mode")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = _args()
    args.yes = False
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    with pytest.raises(SystemExit) as exc:
        tb.STAGE_FUNCTIONS["translate"](args, project, state)

    assert exc.value.code == 1


def test_unapproved_noninteractive_translate_prints_recovery(project: Path, monkeypatch, capsys):
    """The fail-closed path should tell agents exactly how to proceed after approval."""
    def _boom(*_a, **_k):
        raise AssertionError("stage_translate called input() in non-interactive mode")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = _args()
    args.yes = False
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    with pytest.raises(SystemExit) as exc:
        tb.STAGE_FUNCTIONS["translate"](args, project, state)

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "re-run with --yes" in out


@pytest.mark.parametrize("response", ["n", ""])
def test_interactive_rejection_exits_without_translating(project: Path, monkeypatch, response: str):
    """Human CLI users can decline the spend estimate without mutating chunks."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: response)
    args = _args()
    args.yes = False
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    with pytest.raises(SystemExit) as exc:
        tb.STAGE_FUNCTIONS["translate"](args, project, state)

    assert exc.value.code == 0
    for cf in (project / "chunks").glob("*_chunk_*.json"):
        assert not validate_chunk_file(cf).has_translation


def test_interactive_approval_translates(project: Path, monkeypatch):
    """Human CLI users can approve the spend estimate and proceed."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")
    args = _args()
    args.yes = False
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)

    state = tb.STAGE_FUNCTIONS["translate"](args, project, state)

    assert state["stage_completed"] == "translate"
    for cf in (project / "chunks").glob("*_chunk_*.json"):
        assert validate_chunk_file(cf).has_translation


def test_translate_book_cli_rejects_removed_cost_limit(tmp_path: Path):
    """The old threshold flag should not silently come back into the harness path."""
    project_dir = tmp_path / "book"
    project_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "scripts/translate_book.py",
            "--project-dir",
            str(project_dir),
            "--start-stage",
            "translate",
            "--cost-only",
            "--cost-limit",
            "999999",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
    assert "--cost-limit" in result.stderr


def test_harness_stdout_is_utf8_and_writes_artifact(tmp_path: Path):
    """Friction-log #4: harness stdout must be UTF-8 (not the Windows cp1252 default) and the
    result must also land in .harness/last_output.json so the agent never has to parse stdout."""
    from src.harness import state as hstate

    # Minimal project: a config (creates .harness/) plus an agent glossary draft whose
    # translation carries accents/ñ — the bytes that broke cp1252 stdout in the dogfood run.
    hstate.save_config(tmp_path, {})
    draft = [{"english": "queen", "translation": "la reina pequeña",
              "type": "noun", "context": "la niña"}]
    (tmp_path / ".harness" / "glossary_draft.json").write_text(
        json.dumps(draft), encoding="utf-8")

    # capture_output WITHOUT text=True -> raw bytes, so we control the decode ourselves.
    result = subprocess.run(
        [sys.executable, "scripts/harness.py", "glossary", "commit", "--project", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    # stdout decodes as UTF-8 and the accented translation survives round-trip.
    payload = json.loads(result.stdout.decode("utf-8"))
    assert any(t["translation"] == "la reina pequeña" for t in payload["terms"])

    # The artifact mirror exists and matches (this is what the agent should Read).
    artifact = tmp_path / ".harness" / "last_output.json"
    assert artifact.exists(), "last_output.json artifact was not written"
    mirrored = json.loads(artifact.read_text(encoding="utf-8"))
    assert mirrored == payload
    assert b"OUTPUT_JSON:" in result.stderr


def test_run_script_forces_utf8_in_child_env(monkeypatch):
    """Friction-log #4: reconfiguring the parent's stdout doesn't reach the wrapped
    chunk/cost/translate/epub subprocess, so _run_script must hand the child PYTHONUTF8=1
    /PYTHONIOENCODING=utf-8 on top of the inherited environment."""
    from src.harness import flow

    captured = {}

    class FakeProc:
        stdout = iter(())  # no output to stream

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(flow.subprocess, "Popen", fake_popen)

    rc, summary = flow._run_script(["scripts/translate_book.py", "chunk"])
    assert rc == 0
    assert summary is None  # no HARNESS_RESULT sentinel was emitted

    env = captured["env"]
    assert env is not None, "_run_script must pass an explicit env to the child"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    # The forced vars are layered on top of the inherited environment, not a bare dict.
    assert env.get("PATH") == os.environ.get("PATH")


def test_run_script_captures_and_hides_harness_result_sentinel(monkeypatch, capsys):
    """Friction-log #18: _run_script tees the child's stdout — passing human output
    through live — but lifts the single HARNESS_RESULT: line out as the parsed summary
    and never echoes that machine-only line to the human stream."""
    from src.harness import flow, state

    sentinel = (
        f'{state.HARNESS_RESULT_PREFIX} '
        '{"stage": "cost-estimate", "total_chunks_in_scope": 7}\n'
    )

    class FakeProc:
        stdout = iter(("Chunking the book...\n", sentinel, "Estimate ready.\n"))

        def wait(self):
            return 0

    monkeypatch.setattr(flow.subprocess, "Popen", lambda cmd, **k: FakeProc())

    rc, summary = flow._run_script(["scripts/translate_book.py", "chunk"])
    assert rc == 0
    assert summary == {"stage": "cost-estimate", "total_chunks_in_scope": 7}

    out = capsys.readouterr().out
    assert "Chunking the book..." in out and "Estimate ready." in out
    assert state.HARNESS_RESULT_PREFIX not in out  # machine-only line stays hidden


def test_run_script_malformed_sentinel_falls_back_to_none(monkeypatch, capsys):
    """A HARNESS_RESULT: line with invalid JSON is silently swallowed (summary=None).

    The malformed line must still be hidden from the human stream; the returncode
    and any surrounding human-readable output must pass through normally.
    """
    from src.harness import flow, state

    bad_sentinel = f"{state.HARNESS_RESULT_PREFIX} {{not valid json\n"

    class FakeProc:
        stdout = iter(("Progress line.\n", bad_sentinel, "Done.\n"))

        def wait(self):
            return 0

    monkeypatch.setattr(flow.subprocess, "Popen", lambda cmd, **k: FakeProc())

    rc, summary = flow._run_script(["scripts/translate_book.py", "chunk"])
    assert rc == 0
    assert summary is None  # malformed JSON -> graceful fallback, not a crash

    out = capsys.readouterr().out
    assert "Progress line." in out and "Done." in out
    assert state.HARNESS_RESULT_PREFIX not in out  # still hidden even when malformed


# ===========================================================================
# Subagent backend spine (Phase B): prepare -> (worker writes prose) -> commit
# ===========================================================================
#
# The API-path spine above stubs translate_chunk_realtime. The subagent path
# never calls it — it goes prepare -> worker writes prose to draft_path ->
# commit (guard + stamp). This twin proves that spine offline: fake worker
# prose -> commit -> combine -> epub, with provenance + idempotent resume.


def test_subagent_spine_prepare_commit_produces_valid_epub(project: Path):
    from src.harness import flow

    # Chunk the fixture (deterministic), then prepare per-chunk prompts.
    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project), worker_model="sonnet")
    assert prep["manifest"], "prepare produced no work"
    assert prep["usage_summary"]["worker_model"] == "sonnet"
    assert prep["usage_summary"]["chunks"] == len(prep["manifest"])

    # Simulate workers: each writes prose (not an echo) to its draft_path.
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")

    res = flow.translate_commit(str(project), worker_model="sonnet")
    assert res["counts"]["committed"] == len(prep["manifest"])
    assert res["counts"]["failed"] == 0 and res["counts"]["missing"] == 0

    # Chunks are translated AND carry provenance.
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        assert chunk.has_translation, f"{entry['chunk_id']} not stamped"
        assert chunk.last_llm_log, "subagent commit must stamp provenance (last_llm_log)"
        eval_path = project / "evaluations" / f"{entry['chunk_id']}.json"
        assert eval_path.exists(), f"missing evaluation JSON for {entry['chunk_id']}"
        doc = json.loads(eval_path.read_text(encoding="utf-8"))
        assert "aggregated" in doc and isinstance(doc["aggregated"], dict)

    # Idempotent: re-commit touches nothing (resume safety).
    eval_mtimes = {
        entry["chunk_id"]: (project / "evaluations" / f"{entry['chunk_id']}.json").stat().st_mtime
        for entry in prep["manifest"]
    }
    res2 = flow.translate_commit(str(project), worker_model="sonnet")
    assert res2["counts"]["committed"] == 0
    assert res2["counts"]["skipped"] == len(prep["manifest"])
    for cid, before in eval_mtimes.items():
        after = (project / "evaluations" / f"{cid}.json").stat().st_mtime
        assert after == before, f"skipped chunk {cid} should not rewrite its evaluation JSON"

    # combine + epub build from the subagent-translated chunks.
    state: dict = {}
    for stage in ("combine", "epub"):
        state = tb.STAGE_FUNCTIONS[stage](_args(), project, state)
    epubs = list(project.rglob("*.epub"))
    assert epubs, "no EPUB produced from subagent-translated chunks"
    with zipfile.ZipFile(epubs[0]) as z:
        assert z.testzip() is None
        assert any(n.endswith((".xhtml", ".html")) for n in z.namelist())


def test_stage_evaluate_persists_dashboard_json(project: Path):
    """Stage evaluate should run the full coded suite and persist Review-tab JSON."""
    args = _args()
    state: dict = {}
    state = tb.STAGE_FUNCTIONS["chunk"](args, project, state)
    state = tb.STAGE_FUNCTIONS["translate"](args, project, state)
    state = tb.STAGE_FUNCTIONS["evaluate"](args, project, state)

    chunk_files = list((project / "chunks").glob("*_chunk_*.json"))
    assert chunk_files, "no chunks to evaluate"
    for cf in chunk_files:
        chunk = validate_chunk_file(cf)
        assert chunk.has_translation
        eval_path = project / "evaluations" / f"{chunk.id}.json"
        assert eval_path.exists(), f"missing evaluation JSON for {chunk.id}"
        doc = json.loads(eval_path.read_text(encoding="utf-8"))
        assert "aggregated" in doc and isinstance(doc["aggregated"], dict)
        assert "enabled_evals" in doc and isinstance(doc["enabled_evals"], list)
        assert "length" in doc["enabled_evals"]


def test_translate_commit_reports_evaluated_count(project: Path):
    """A fresh commit evaluates + persists every committed chunk and reports the count."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project), worker_model="sonnet")
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")

    res = flow.translate_commit(str(project), worker_model="sonnet")
    assert res["counts"]["committed"] == len(prep["manifest"])
    assert res["counts"]["evaluated"] == len(prep["manifest"])
    assert res["evaluated"] == len(prep["manifest"])


def test_translate_commit_survives_evaluation_failure(project: Path, monkeypatch):
    """Evaluator persistence is best-effort: a raising evaluate_and_persist_chunk
    must not fail the commit, and the chunk is still stamped even though no
    evaluations/*.json is written and the evaluated count stays 0."""
    from src.harness import flow
    import web_ui.evaluations as evaluations

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project), worker_model="sonnet")
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr(evaluations, "evaluate_and_persist_chunk", _boom)

    res = flow.translate_commit(str(project), worker_model="sonnet")

    # Commit succeeds despite the evaluator blowing up ...
    assert res["counts"]["committed"] == len(prep["manifest"])
    assert res["counts"]["failed"] == 0
    # ... but nothing was evaluated or persisted.
    assert res["counts"]["evaluated"] == 0
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        assert chunk.has_translation, "chunk must still be committed despite eval failure"
        assert not (project / "evaluations" / f"{entry['chunk_id']}.json").exists()


def test_show_translation_returns_committed_text(project: Path):
    """Read-back surface for review (friction-log #7): show-translation returns the
    committed translated_text/source_text from chunks/*.json, not the consumed drafts."""
    from src.harness import flow

    # No chunks yet -> graceful error dict, never a traceback.
    assert "error" in flow.show_translation(str(project))

    # Translate via the subagent spine so chunks carry translated_text.
    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project), worker_model="sonnet")
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")
    flow.translate_commit(str(project), worker_model="sonnet")

    res = flow.show_translation(str(project))
    assert res["chapters"], "no chapters returned"
    assert res["fields"] == {"translation": "translated_text", "source": "source_text"}
    rows = [r for ch in res["chapters"] for r in ch["chunks"]]
    assert rows, "no chunk rows returned"
    for row in rows:
        assert row["translated_text"], f"{row['id']} has no translated_text"
        assert row["has_translation"] is True
        # The returned text matches what was committed to the chunk file.
        committed = load_chunk(project / "chunks" / f"{row['id']}.json")
        assert row["translated_text"] == committed.translated_text
        assert row["source_text"] == committed.source_text  # source on by default

    # include_source=False drops source_text.
    lean = flow.show_translation(str(project), include_source=False)
    assert all("source_text" not in r for ch in lean["chapters"] for r in ch["chunks"])

    # max_chunks caps the sample and flags truncation when chunks remain.
    total = res["total_chunks"]
    if total > 1:
        capped = flow.show_translation(str(project), max_chunks=1)
        assert capped["shown_chunks"] == 1
        assert capped["truncated"] is True
        assert capped["total_chunks"] == total

    # An out-of-range chapter spec returns available_chapters, not a crash.
    miss = flow.show_translation(str(project), chapters="999")
    assert miss["chapters"] == [] and miss["available_chapters"]

    # An unparseable chapter spec is caught and reported, not raised.
    bad = flow.show_translation(str(project), chapters="abc")
    assert "error" in bad and bad["chapters"] == []


def test_translate_commit_flags_bad_worker_output(project: Path):
    """A draft that fails the guard is reported, never stamped."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project))
    # Worker echoes the English source verbatim -> echo guard fails.
    for entry in prep["manifest"]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(chunk.source_text, encoding="utf-8")

    res = flow.translate_commit(str(project))
    assert res["counts"]["committed"] == 0
    assert res["counts"]["failed"] == len(prep["manifest"])
    assert all(any("verbatim copy" in p for p in f["problems"]) for f in res["failed"])
    # Nothing was stamped.
    for entry in prep["manifest"]:
        assert not validate_chunk_file(Path(entry["chunk_path"])).has_translation


def test_translate_commit_allow_problem_waives_false_positive(project: Path):
    """--allow-problem waives a named guard problem; other guards stay enforced (friction #15)."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project))
    assert prep["manifest"], "need at least one chunk"
    entry = prep["manifest"][0]
    cid = entry["chunk_id"]
    draft_path = Path(entry["draft_path"])
    chunk_path = Path(entry["chunk_path"])
    source = validate_chunk_file(chunk_path).source_text

    # Selectivity: a real defect (echo) is NOT waived by an unrelated substring.
    draft_path.write_text(source, encoding="utf-8")  # verbatim echo
    res_echo = flow.translate_commit(str(project), allow_problems=["XXX"])
    assert cid in {f["chunk_id"] for f in res_echo["failed"]}
    assert res_echo["waived"] == {}
    assert not validate_chunk_file(chunk_path).has_translation

    # Now the draft trips ONLY the placeholder guard (XXX followed by prose).
    draft_path.write_text(_fake_draft(source) + " XXX pendiente.", encoding="utf-8")

    res = flow.translate_commit(str(project))  # no waive -> placeholder blocks
    fails = {f["chunk_id"]: f["problems"] for f in res["failed"]}
    assert cid in fails and any("XXX" in p for p in fails[cid])

    res_nomatch = flow.translate_commit(str(project), allow_problems=["NOPE"])
    assert cid in {f["chunk_id"] for f in res_nomatch["failed"]}
    assert res_nomatch["waived"] == {}

    res_waive = flow.translate_commit(str(project), allow_problems=["XXX"])
    assert cid in res_waive["waived"] and any("XXX" in p for p in res_waive["waived"][cid])
    assert cid in res_waive["committed"]
    stamped = validate_chunk_file(chunk_path)
    assert stamped.has_translation and stamped.last_llm_log


def test_translate_prepare_chapter_scope(project: Path):
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})

    only1 = flow.translate_prepare(str(project), chapters="1")
    assert only1["manifest"], "chapter 1 should have work"
    assert all(e["chapter_id"] == "chapter_01" for e in only1["manifest"])

    none = flow.translate_prepare(str(project), chapters="9")
    assert none["manifest"] == []
    assert "no matching chapters" in none.get("note", "")


# ===========================================================================
# Edge-case / error paths for prepare + commit
# ===========================================================================


def test_translate_prepare_no_chunks_dir_returns_error(tmp_path: Path):
    """If chunks/ has not been created yet, prepare returns an error, not an exception."""
    from src.harness import flow, state as hstate

    # Minimal project: harness config exists but no chunks/ dir.
    hstate.save_config(tmp_path, {})
    result = flow.translate_prepare(str(tmp_path))
    assert "error" in result
    assert "chunk" in result["error"]
    assert result["manifest"] == []


def test_translate_prepare_all_already_translated_returns_empty_manifest(project: Path):
    """If every chunk is already translated, the manifest is empty and the
    instructions say 'Nothing to translate'."""
    from src.harness import flow

    # Chunk, then fake-translate all chunks in place.
    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    tb.STAGE_FUNCTIONS["translate"](_args(), project, {})

    prep = flow.translate_prepare(str(project))
    assert prep["manifest"] == []
    assert "Nothing to translate" in prep["instructions"]


def test_translate_commit_no_manifest_returns_error(tmp_path: Path):
    """Calling commit before prepare returns an error dict, not an exception."""
    from src.harness import flow, state as hstate

    hstate.save_config(tmp_path, {})
    result = flow.translate_commit(str(tmp_path))
    assert "error" in result
    assert "translate-prepare" in result["error"]
    assert result["committed"] == []


def test_translate_commit_missing_draft_reported_not_stamped(project: Path):
    """Chunks whose draft file was never written appear in 'missing', are never stamped."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project))
    assert prep["manifest"], "need at least one entry"

    # Write drafts for all but the first entry — first stays missing.
    for entry in prep["manifest"][1:]:
        chunk = validate_chunk_file(Path(entry["chunk_path"]))
        Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")

    res = flow.translate_commit(str(project))
    assert res["counts"]["missing"] >= 1
    assert prep["manifest"][0]["chunk_id"] in res["missing"]
    # The missing chunk must not be stamped.
    first_chunk = validate_chunk_file(Path(prep["manifest"][0]["chunk_path"]))
    assert not first_chunk.has_translation


def test_translate_prepare_malformed_chapters_returns_error(project: Path):
    """An invalid --chapters spec (e.g. 'abc') returns an error dict, not an exception."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    result = flow.translate_prepare(str(project), chapters="abc")
    assert "error" in result
    assert result["manifest"] == []


def test_translate_commit_corrupted_manifest_returns_error(project: Path):
    """A truncated/invalid manifest.json returns an error dict instead of crashing."""
    from src.harness import flow, state as hstate

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    # Create a harness dir with a corrupt manifest.
    hdir = hstate.ensure_harness_dir(project)
    (hdir / "translate").mkdir(parents=True, exist_ok=True)
    (hdir / "translate" / "manifest.json").write_text('{"incomplete":', encoding="utf-8")

    result = flow.translate_commit(str(project))
    assert "error" in result
    assert result["committed"] == []


def test_translate_commit_missing_prompt_file_still_commits(project: Path):
    """If the prompt file is absent, commit still stamps the chunk (prose = '')."""
    from src.harness import flow

    tb.STAGE_FUNCTIONS["chunk"](_args(), project, {})
    prep = flow.translate_prepare(str(project))

    # Write valid prose but delete the prompt file for the first entry.
    entry = prep["manifest"][0]
    chunk = validate_chunk_file(Path(entry["chunk_path"]))
    Path(entry["draft_path"]).write_text(_fake_draft(chunk.source_text), encoding="utf-8")
    prompt_file = Path(entry["prompt_path"])
    if prompt_file.exists():
        prompt_file.unlink()

    # Write normal drafts for the rest so commit can fully run.
    for e in prep["manifest"][1:]:
        c = validate_chunk_file(Path(e["chunk_path"]))
        Path(e["draft_path"]).write_text(_fake_draft(c.source_text), encoding="utf-8")

    res = flow.translate_commit(str(project))
    # The first chunk should still be committed even without its prompt file.
    assert entry["chunk_id"] in res["committed"]


# ===========================================================================
# Spawn modes (Step 4B): EN+ES context, config persistence, and align
# ===========================================================================


def _save_chunks(chunks_dir: Path, chapter_id: str, sources: list[str],
                 translations: list | None = None) -> list[Path]:
    """Write a chapter as N chunks (optionally pre-translated); return their paths."""
    translations = translations or [None] * len(sources)
    paths: list[Path] = []
    for pos, (src, tr) in enumerate(zip(sources, translations)):
        chunk = Chunk(
            id=f"{chapter_id}_chunk_{pos:03d}",
            chapter_id=chapter_id,
            position=pos,
            source_text=src,
            translated_text=tr,
            metadata=ChunkMetadata(
                char_start=0, char_end=len(src), overlap_start=0, overlap_end=0,
                paragraph_count=src.count("\n\n") + 1, word_count=len(src.split()),
            ),
            status=ChunkStatus.TRANSLATED if tr else ChunkStatus.PENDING,
        )
        cp = chunks_dir / f"{chapter_id}_chunk_{pos:03d}.json"
        save_chunk(chunk, cp)
        paths.append(cp)
    return paths


def _prompt_for(prep: dict, chunk_id: str) -> str:
    entry = next(e for e in prep["manifest"] if e["chunk_id"] == chunk_id)
    return Path(entry["prompt_path"]).read_text(encoding="utf-8")


def test_translate_prepare_injects_committed_translation_context(tmp_path: Path):
    """A chunk whose predecessor is committed gets the predecessor's EN+ES; an
    uncommitted predecessor yields source-only context (never blocking)."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(
        chunks_dir, "chapter_01",
        sources=["UNIQUESOURCEMARKER alpha beta gamma.", "Second chunk body text."],
    )

    # Pass 1: predecessor (chunk_000) untranslated -> chunk_001 gets source only.
    prep1 = flow.translate_prepare(str(tmp_path))
    p1 = _prompt_for(prep1, "chapter_01_chunk_001")
    assert "UNIQUESOURCEMARKER" in p1, "predecessor source tail must be present"
    assert "Spanish Translation" not in p1, "no Spanish yet — must not fabricate it"

    # Commit chunk_000 with a distinctive Spanish translation.
    c0 = load_chunk(chunks_dir / "chapter_01_chunk_000.json")
    c0.translated_text = "UNIQUETRANSMARKER alfa beta gama."
    c0.status = ChunkStatus.TRANSLATED
    save_chunk(c0, chunks_dir / "chapter_01_chunk_000.json")

    # Pass 2: re-prepare -> chunk_001 now sees the predecessor's Spanish too.
    prep2 = flow.translate_prepare(str(tmp_path))
    assert [e["chunk_id"] for e in prep2["manifest"]] == ["chapter_01_chunk_001"]
    p2 = _prompt_for(prep2, "chapter_01_chunk_001")
    assert "UNIQUESOURCEMARKER" in p2 and "UNIQUETRANSMARKER" in p2
    assert "Spanish Translation" in p2


def test_translate_prepare_persists_spawn_mode(tmp_path: Path):
    """--parallelism/--window round-trip through project config and echo back."""
    from src.harness import flow, state

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["Only one chunk here."])

    prep = flow.translate_prepare(str(tmp_path), parallelism="all", window=3)
    assert prep["spawn_plan"] == {"parallelism": "all", "window": 3, "batch_size": 3}
    assert prep["usage_summary"]["parallelism"] == "all"

    # Persisted: a later prepare with no spawn args reuses the saved choice.
    prep2 = flow.translate_prepare(str(tmp_path))
    assert prep2["spawn_plan"] == {"parallelism": "all", "window": 3, "batch_size": 3}

    # window persists in isolation (no parallelism passed) without disturbing the
    # previously-saved parallelism. window > batch_size is clamped to batch_size.
    prep3 = flow.translate_prepare(str(tmp_path), window=5)
    assert prep3["spawn_plan"] == {"parallelism": "all", "window": 3, "batch_size": 3}
    assert state.load_config(state.resolve_project_dir(str(tmp_path)))["parallel_window"] == 3

    # batch_size persists in isolation too, and is echoed in the usage summary.
    prep4 = flow.translate_prepare(str(tmp_path), batch_size=3)
    assert prep4["spawn_plan"] == {"parallelism": "all", "window": 3, "batch_size": 3}
    assert prep4["usage_summary"]["batch_size"] == 3

    # A single-chunk-per-chapter book makes the spawn-mode choice moot.
    assert prep4["spawn_mode_moot"] is True

    # Invalid batch_size is reported, not raised, and must not corrupt the saved config.
    bad_bs = flow.translate_prepare(str(tmp_path), batch_size=0)
    assert "error" in bad_bs and bad_bs["manifest"] == []
    from src.harness import state as _state
    assert _state.load_config(_state.resolve_project_dir(str(tmp_path)))["batch_size"] == 3

    # Invalid mode is reported, not raised, and must not corrupt the saved config.
    from src.harness import state
    bad = flow.translate_prepare(str(tmp_path), parallelism="bogus")
    assert "error" in bad and bad["manifest"] == []
    assert state.load_config(state.resolve_project_dir(str(tmp_path)))["parallelism"] == "all"


def test_translate_prepare_emits_byte_identical_preamble_and_body(tmp_path: Path):
    """Shared preamble + per-chunk body round-trip to prompt.txt; paths land on the manifest.

    With always_include_dialogue forced on, the cacheable prefix is stable across a
    dialogue chunk and a plain chunk — the headless fan-out precondition.
    """
    from src.api_translator import build_translation_prompt
    from src.harness import flow, state as hstate
    from src.utils.file_io import load_chunk

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(
        chunks_dir, "chapter_01",
        sources=[
            'She said, "Hello there," and waved.',
            "The meadow was quiet in the afternoon sun.",
        ],
    )
    hstate.save_config(tmp_path, {"always_include_dialogue": True})

    prep = flow.translate_prepare(str(tmp_path))
    assert len(prep["manifest"]) == 2
    preamble_paths = {e.get("preamble_path") for e in prep["manifest"]}
    assert len(preamble_paths) == 1 and None not in preamble_paths
    preamble_path = Path(next(iter(preamble_paths)))
    assert preamble_path.name == "preamble.txt"
    preamble = preamble_path.read_text(encoding="utf-8")
    assert preamble, "preamble must be non-empty"

    for entry in prep["manifest"]:
        assert "body_path" in entry and "preamble_path" in entry
        body = Path(entry["body_path"]).read_text(encoding="utf-8")
        prompt = Path(entry["prompt_path"]).read_text(encoding="utf-8")
        assert preamble + body == prompt

    # First chunk has no predecessor context — full prompt must match the API builder.
    first = prep["manifest"][0]
    chunk0 = load_chunk(Path(first["chunk_path"]))
    expected = build_translation_prompt(
        chunk0,
        project_name=tmp_path.name,
        source_language="English",
        target_language="Spanish",
        always_include_dialogue=True,
        always_include_image_instructions=False,
    )
    assert Path(first["prompt_path"]).read_text(encoding="utf-8") == expected


def test_translate_prepare_omits_split_paths_when_prefix_diverges(tmp_path: Path):
    """Without always_include_dialogue, a dialogue chunk and a plain chunk diverge —
    only matching entries keep preamble_path/body_path."""
    from src.harness import flow, state as hstate

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(
        chunks_dir, "chapter_01",
        sources=[
            'She said, "Hello there," and waved.',
            "The meadow was quiet in the afternoon sun.",
        ],
    )
    hstate.save_config(tmp_path, {"always_include_dialogue": False})

    prep = flow.translate_prepare(str(tmp_path))
    assert len(prep["manifest"]) == 2
    # First chunk establishes the preamble; the second diverges and must omit paths.
    first, second = prep["manifest"][0], prep["manifest"][1]
    assert "preamble_path" in first and "body_path" in first
    assert "preamble_path" not in second and "body_path" not in second
    # Full prompt.txt still written for both.
    assert Path(first["prompt_path"]).exists()
    assert Path(second["prompt_path"]).exists()


def test_translate_fanout_writes_drafts_via_runner_seam(tmp_path: Path):
    """translate-fanout invokes the runner with --system-prompt-file when split paths exist."""
    from src.harness import flow, state as hstate

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["One short sentence for a draft."])
    hstate.save_config(tmp_path, {"always_include_dialogue": True, "batch_size": 2})

    prep = flow.translate_prepare(str(tmp_path))
    entry = prep["manifest"][0]
    assert "preamble_path" in entry and "body_path" in entry

    seen_cmds: list[list[str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen_cmds.append(list(cmd))
        assert "--system-prompt-file" in cmd
        assert entry["preamble_path"] in cmd
        assert input_text == Path(entry["body_path"]).read_text(encoding="utf-8")
        assert Path(cwd).name == "claude-headless-empty"
        return 0, "es_one es_short es_sentence es_for es_a es_draft", ""

    out = flow.translate_fanout(str(tmp_path), runner=fake_runner)
    assert out["counts"]["wrote"] == 1
    assert entry["chunk_id"] in out["wrote"]
    draft = Path(entry["draft_path"]).read_text(encoding="utf-8").strip()
    assert draft.startswith("es_one")
    assert seen_cmds and "--tools" in seen_cmds[0]

    # Idempotent: existing draft is skipped.
    out2 = flow.translate_fanout(str(tmp_path), runner=fake_runner)
    assert out2["counts"]["skipped"] == 1
    assert out2["counts"]["wrote"] == 0


def test_translate_fanout_falls_back_when_split_mismatches_prompt(tmp_path: Path):
    """Stale preamble+body that no longer equals prompt.txt → full prompt, no system file."""
    from src.harness import flow, state as hstate

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["One short sentence for a draft."])
    hstate.save_config(tmp_path, {"always_include_dialogue": True, "batch_size": 2})

    prep = flow.translate_prepare(str(tmp_path))
    entry = prep["manifest"][0]
    assert "preamble_path" in entry and "body_path" in entry
    # Corrupt the body so the split no longer matches prompt.txt.
    Path(entry["body_path"]).write_text("STALE BODY\n", encoding="utf-8")

    seen_cmds: list[list[str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen_cmds.append(list(cmd))
        assert "--system-prompt-file" not in cmd
        assert input_text == Path(entry["prompt_path"]).read_text(encoding="utf-8")
        return 0, "es_fallback draft prose here", ""

    out = flow.translate_fanout(str(tmp_path), runner=fake_runner)
    assert out["counts"]["wrote"] == 1
    assert entry["chunk_id"] in out["wrote"]
    assert seen_cmds and "--system-prompt-file" not in seen_cmds[0]


def test_translate_fanout_isolates_runner_exceptions(tmp_path: Path):
    """A raising runner becomes a per-chunk failed entry, not a wave-aborting crash."""
    from src.harness import flow, state as hstate

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(
        chunks_dir, "chapter_01",
        sources=["First sentence here.", "Second sentence here."],
    )
    hstate.save_config(tmp_path, {"always_include_dialogue": True, "batch_size": 2})
    prep = flow.translate_prepare(str(tmp_path))
    assert len(prep["manifest"]) == 2

    def boom_runner(cmd, *, input_text, cwd):
        raise RuntimeError("simulated launch failure")

    out = flow.translate_fanout(str(tmp_path), runner=boom_runner)
    assert "error" not in out
    assert out["counts"]["wrote"] == 0
    assert out["counts"]["failed"] == 2
    assert all("RuntimeError" in f["error"] for f in out["failed"])


def test_translate_fanout_fails_fast_when_claude_missing(tmp_path: Path, monkeypatch):
    """Missing claude binary → one top-level error, no per-chunk wave."""
    from src.harness import flow, state as hstate

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["One short sentence for a draft."])
    hstate.save_config(tmp_path, {"always_include_dialogue": True})
    flow.translate_prepare(str(tmp_path))

    import src.harness.headless as headless

    monkeypatch.setattr(headless.shutil, "which", lambda _name: None)
    out = flow.translate_fanout(str(tmp_path), claude_bin="claude-not-installed")
    assert "error" in out
    assert "claude not found" in out["error"]
    assert out["counts"]["todo"] == 0
    assert out["counts"]["failed"] == 0


def test_translate_prepare_never_wipes_or_strands_uncommitted_drafts(tmp_path: Path):
    """A narrower re-prepare must not wipe (or orphan) a finished wave's drafts.

    Regression for the Pollyanna hiccup #1 draft wipe: a wave's drafts were written for
    chunks outside the *last* prepare manifest, then a re-prepare whose scope re-covered
    them destroyed the drafts (unconditional unlink) while the manifest-only rescue never
    saw them. The fix is two-fold — prepare keeps non-empty drafts, and rescue scans the
    drafts on disk — so both an in-scope draft (protected from the unlink) and an
    out-of-scope draft (rescued into the manifest) survive and stay committable.
    """
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    sources = {
        "chapter_01": "First chapter opening line here.",
        "chapter_02": "Second chapter body text here.",
        "chapter_03": "Third chapter body text here.",
        "chapter_04": "Fourth chapter body text here.",
    }
    for chap, src in sources.items():
        _save_chunks(chunks_dir, chap, sources=[src])

    # Prior manifest is narrow (ch01 only) — it does NOT list ch02/03/04, exactly like
    # the friction-log state when the stranded drafts were written off a stale manifest.
    prep_a = flow.translate_prepare(str(tmp_path), chapters="1", parallelism="chapter")
    translate_dir = Path(prep_a["manifest"][0]["draft_path"]).parent

    def _draft_path(chap: str) -> Path:
        return translate_dir / f"{chap}_chunk_000.draft.txt"

    # A finished wave lands drafts for chunks absent from the prior (ch01) manifest.
    expected_drafts = {}
    for chap in ("chapter_02", "chapter_03", "chapter_04"):
        expected_drafts[chap] = _fake_draft(sources[chap])
        _draft_path(chap).write_text(expected_drafts[chap], encoding="utf-8")

    # Re-prepare a scope that re-covers ch02/03 (in scope) but not ch04 (out of scope).
    prep_b = flow.translate_prepare(str(tmp_path), chapters="1-3", parallelism="chapter")

    # Fix A: in-scope non-empty drafts are kept, not unlinked, and content is unchanged.
    for chap in ("chapter_02", "chapter_03"):
        assert _draft_path(chap).exists(), f"{chap} draft was wiped"
        assert _draft_path(chap).read_text(encoding="utf-8") == expected_drafts[chap]
    # Fix B: the out-of-scope draft survives on disk, unchanged, and is rescued into the manifest.
    assert _draft_path("chapter_04").exists(), "out-of-scope ch04 draft was lost"
    assert _draft_path("chapter_04").read_text(encoding="utf-8") == expected_drafts["chapter_04"]
    assert prep_b["rescued_prior_drafts"] == 1  # only ch04 (ch02/03 are in-scope)

    manifest_ids = {e["chunk_id"] for e in prep_b["manifest"]}
    assert {"chapter_02_chunk_000", "chapter_03_chunk_000",
            "chapter_04_chunk_000"} <= manifest_ids

    # All three drafted chunks commit — none are lost or reported missing.
    _draft_path("chapter_01").write_text(_fake_draft(sources["chapter_01"]), encoding="utf-8")
    res = flow.translate_commit(str(tmp_path), worker_model="sonnet")
    assert res["counts"]["missing"] == 0
    assert {"chapter_02_chunk_000", "chapter_03_chunk_000",
            "chapter_04_chunk_000"} <= set(res["committed"])


def test_translate_prepare_unlinks_whitespace_only_in_scope_draft(tmp_path: Path):
    """Whitespace-only in-scope drafts are cleared so they cannot masquerade as work."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["Only one chunk here."])

    prep = flow.translate_prepare(str(tmp_path), chapters="1")
    draft_path = Path(prep["manifest"][0]["draft_path"])
    draft_path.write_text("   \n\t  ", encoding="utf-8")

    prep2 = flow.translate_prepare(str(tmp_path), chapters="1")
    assert "error" not in prep2
    assert not draft_path.exists()


def test_translate_prepare_rescues_despite_stale_prior_chunk_path(tmp_path: Path):
    """A prior-manifest chunk_path that does not match the draft id falls back to id lookup."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    sources = {
        "chapter_01": "First chapter opening line here.",
        "chapter_02": "Second chapter body text here.",
        "chapter_03": "Third chapter body text here.",
    }
    paths = {}
    for chap, src in sources.items():
        paths[chap] = _save_chunks(chunks_dir, chap, sources=[src])[0]

    prep_a = flow.translate_prepare(str(tmp_path), chapters="1-3")
    translate_dir = Path(prep_a["manifest"][0]["draft_path"]).parent
    draft_path = translate_dir / "chapter_03_chunk_000.draft.txt"
    draft_path.write_text(_fake_draft(sources["chapter_03"]), encoding="utf-8")

    # Corrupt the saved manifest: point ch03's chunk_path at ch01's file.
    manifest_path = translate_dir / "manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in doc["entries"]:
        if entry["chunk_id"] == "chapter_03_chunk_000":
            entry["chunk_path"] = str(paths["chapter_01"])
            break
    manifest_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    prep_b = flow.translate_prepare(str(tmp_path), chapters="1-2")
    assert prep_b["rescued_prior_drafts"] == 1
    manifest_ids = {e["chunk_id"] for e in prep_b["manifest"]}
    assert "chapter_03_chunk_000" in manifest_ids
    rescued = next(e for e in prep_b["manifest"] if e["chunk_id"] == "chapter_03_chunk_000")
    assert Path(rescued["chunk_path"]) == paths["chapter_03"]


def test_translate_prepare_skips_unreadable_draft_without_crashing(tmp_path: Path):
    """Binary/corrupt draft files are skipped; prepare must not raise."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    sources = {
        "chapter_01": "First chapter opening line here.",
        "chapter_02": "Second chapter body text here.",
        "chapter_03": "Third chapter body text here.",
    }
    for chap, src in sources.items():
        _save_chunks(chunks_dir, chap, sources=[src])

    prep_a = flow.translate_prepare(str(tmp_path), chapters="1")
    translate_dir = Path(prep_a["manifest"][0]["draft_path"]).parent
    corrupt = translate_dir / "chapter_03_chunk_000.draft.txt"
    corrupt.write_bytes(b"\xff\xfe\xfd")

    prep_b = flow.translate_prepare(str(tmp_path), chapters="1-2")
    assert "error" not in prep_b
    assert corrupt.exists(), "unreadable draft should be left on disk"
    assert prep_b["rescued_prior_drafts"] == 0
    assert "chapter_03_chunk_000" not in {e["chunk_id"] for e in prep_b["manifest"]}


def test_translate_prepare_rescues_multiple_out_of_scope_drafts(tmp_path: Path):
    """Several out-of-scope drafts are all rescued and counted."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    sources = {
        f"chapter_{i:02d}": f"Chapter {i} body text here."
        for i in range(1, 6)
    }
    for chap, src in sources.items():
        _save_chunks(chunks_dir, chap, sources=[src])

    prep_a = flow.translate_prepare(str(tmp_path), chapters="1")
    translate_dir = Path(prep_a["manifest"][0]["draft_path"]).parent

    for chap in ("chapter_03", "chapter_04", "chapter_05"):
        (translate_dir / f"{chap}_chunk_000.draft.txt").write_text(
            _fake_draft(sources[chap]), encoding="utf-8"
        )

    prep_b = flow.translate_prepare(str(tmp_path), chapters="1-2")
    assert prep_b["rescued_prior_drafts"] == 3
    manifest_ids = {e["chunk_id"] for e in prep_b["manifest"]}
    assert {"chapter_03_chunk_000", "chapter_04_chunk_000",
            "chapter_05_chunk_000"} <= manifest_ids


def test_translate_prepare_persists_worker_thinking(tmp_path: Path):
    """--worker-thinking round-trips through config; a non-thinking worker forces it off.

    The subagent analog of the GUI: extended thinking is OFF by default, an explicit
    opt-in persists (reused by the "translate the rest" batch), and pinning a
    non-thinking worker (fable, always-on) can never leave the worker flagged on.
    """
    from src.harness import flow, state

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["Only one chunk here."])

    # Fresh config, no flag → thinking off (the default; analog of the unchecked box).
    default = flow.translate_prepare(str(tmp_path))
    assert default["usage_summary"]["worker_thinking"] is False
    assert default["worker_thinking"] is False

    # Opt in on a thinking-capable worker → reported on, persisted, and in the manifest.
    on = flow.translate_prepare(str(tmp_path), worker_model="sonnet", worker_thinking=True)
    assert on["usage_summary"]["worker_thinking"] is True
    assert on["worker_thinking"] is True
    manifest = json.loads(Path(on["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["worker_thinking"] is True

    # Persisted: a later prepare with no thinking arg still reports True.
    again = flow.translate_prepare(str(tmp_path))
    assert again["usage_summary"]["worker_thinking"] is True

    # Pinning a non-thinking worker (fable, always-on) forces it back to False even
    # though the saved preference is still True (gate on model support).
    fable = flow.translate_prepare(str(tmp_path), worker_model="fable")
    assert fable["usage_summary"]["worker_thinking"] is False
    assert state.load_config(state.resolve_project_dir(str(tmp_path)))["worker_thinking"] is True
    # Flipping back to a thinking worker restores the honored preference.
    back = flow.translate_prepare(str(tmp_path), worker_model="sonnet")
    assert back["usage_summary"]["worker_thinking"] is True

    # Explicit opt-out persists too.
    off = flow.translate_prepare(str(tmp_path), worker_thinking=False)
    assert off["usage_summary"]["worker_thinking"] is False
    assert flow.translate_prepare(str(tmp_path))["usage_summary"]["worker_thinking"] is False


@pytest.mark.parametrize("enable_thinking,expected_flag", [
    (True, "--thinking"),
    (False, "--no-thinking"),
    (None, None),
])
def test_translate_threads_thinking_flag_to_subprocess(tmp_path: Path, monkeypatch,
                                                       enable_thinking, expected_flag):
    """flow.translate emits --thinking/--no-thinking/neither to the wrapped CLI.

    None must leave BOTH flags off so translate_book.py falls back to the
    TRANSLATE_THINKING env default rather than being forced off — the same
    tri-state contract the API-path CLI tests assert one layer down.
    """
    from src.harness import flow

    captured: dict = {}

    def fake_run_script(cmd):
        captured["cmd"] = cmd
        return 0, None

    monkeypatch.setattr(flow, "_run_script", fake_run_script)

    flow.translate(str(tmp_path), yes=True, model="claude-sonnet-5",
                   provider="anthropic", enable_thinking=enable_thinking)

    cmd = captured["cmd"]
    if expected_flag is None:
        assert "--thinking" not in cmd and "--no-thinking" not in cmd
    else:
        assert expected_flag in cmd
        # Exactly one of the mutually exclusive flags is present.
        other = "--no-thinking" if expected_flag == "--thinking" else "--thinking"
        assert other not in cmd


@pytest.mark.parametrize("worker_model,expected", [
    # Full model ids (non-aliases) resolve via api_translator.model_supports_thinking,
    # not the alias set — the fallback branch the persistence test doesn't cover.
    ("claude-sonnet-5", True),
    ("claude-opus-4-8", True),
    ("claude-fable-5-20250101", False),   # full fable id: rejected by the fallback, not the alias short-circuit
    ("claude-3-5-sonnet-latest", False),  # legacy: no toggleable thinking
    ("", False),                          # empty guard
])
def test_worker_supports_thinking_full_id_fallback(worker_model, expected):
    """_worker_supports_thinking delegates non-alias ids to model_supports_thinking."""
    from src.harness import flow

    assert flow._worker_supports_thinking(worker_model) is expected


def test_translate_prepare_clamps_window_to_batch_size(tmp_path: Path):
    """--window above batch_size is clamped down and persisted."""
    from src.harness import flow, state

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["Only one chunk here."])

    prep = flow.translate_prepare(str(tmp_path), window=8, batch_size=3)
    assert prep["spawn_plan"]["window"] == 3
    assert prep["spawn_plan"]["batch_size"] == 3
    cfg = state.load_config(state.resolve_project_dir(str(tmp_path)))
    assert cfg["parallel_window"] == 3
    assert cfg["batch_size"] == 3


def test_translate_prepare_rejects_nonpositive_window(tmp_path: Path):
    """--window below 1 is reported as an error and not persisted to config."""
    from src.harness import flow, state

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["Only one chunk here."])

    for bad_window in (0, -5):
        res = flow.translate_prepare(str(tmp_path), window=bad_window)
        assert "error" in res and res["manifest"] == []
        assert "parallel_window" not in state.load_config(
            state.resolve_project_dir(str(tmp_path))
        )


def test_status_reports_progress_artifacts_and_spawn_plan(tmp_path: Path):
    """status answers 'where is this project / what's left?' without a chunk-file loop."""
    from src.harness import flow

    # Before chunking: pre-chunk stage, no crash, artifacts reflect the empty project.
    pre = flow.status(str(tmp_path))
    assert pre["stage"] == "pre-chunk"
    assert pre["artifacts"]["chunks"] is False
    assert pre["spawn_mode_moot"] is None

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    # ch1 fully translated; ch2 one-of-two translated; ch3 untranslated.
    _save_chunks(chunks_dir, "chapter_01", sources=["A."], translations=["es A."])
    _save_chunks(chunks_dir, "chapter_02", sources=["B.", "C."],
                 translations=["es B.", None])
    _save_chunks(chunks_dir, "chapter_03", sources=["D."])

    st = flow.status(str(tmp_path))
    assert st["stage"] == "partial"
    assert st["totals"] == {
        "chapters": 3, "complete_chapters": 1,
        "total_chunks": 4, "translated_chunks": 2, "pending_chunks": 2,
    }
    assert st["pending_chapters"] == ["chapter_02", "chapter_03"]
    # ch2 has two chunks, so the continuity spawn modes are NOT moot here.
    assert st["spawn_mode_moot"] is False
    assert st["spawn_plan"] == {"parallelism": "chapter", "window": 3, "batch_size": 3}
    assert "translate-prepare" in st["next"]

    # Finishing every chunk flips the stage to fully-translated and points at epub.
    _save_chunks(chunks_dir, "chapter_02", sources=["B.", "C."],
                 translations=["es B.", "es C."])
    _save_chunks(chunks_dir, "chapter_03", sources=["D."], translations=["es D."])
    done = flow.status(str(tmp_path))
    assert done["stage"] == "fully-translated"
    assert done["totals"]["pending_chunks"] == 0
    # moot reflects chunk STRUCTURE, not translation state: ch2 still has 2 chunks.
    assert done["spawn_mode_moot"] is False
    assert "epub" in done["next"]


def test_status_single_chunk_per_chapter_is_moot(tmp_path: Path):
    """A book that is one chunk per chapter reports spawn_mode_moot True."""
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["A."])
    _save_chunks(chunks_dir, "chapter_02", sources=["B."])
    assert flow.status(str(tmp_path))["spawn_mode_moot"] is True


def test_runs_summarizes_latest_run_from_log(tmp_path: Path, monkeypatch):
    """runs() reads the write-only run log back into a per-run retro (friction-log #11)."""
    from src.harness import flow
    from src.utils import run_logger

    # Isolate the run log to a temp file so the test never touches the repo's log.
    monkeypatch.setattr(run_logger, "_RUNS_PATH", tmp_path / "harness_runs.jsonl")
    (tmp_path / "chunks").mkdir()  # make tmp_path resolve as a project dir
    slug = tmp_path.name

    # Two runs; the newer one (r2) carries a command + a qualitative beat.
    run_logger.log_run_event(run_id="r1", project=slug, event="command",
                             cmd="setup", status="ok", dur_s=1.0)
    run_logger.log_run_event(run_id="r2", project=slug, event="command",
                             cmd="translate-prepare", status="ok", dur_s=2.0)
    run_logger.log_run_event(run_id="r2", project=slug, event="approval",
                             beat="glossary", decision="approved_first_pass")
    # A different project's event must not leak into this project's summary.
    run_logger.log_run_event(run_id="rX", project="someone_else", event="command",
                             cmd="setup", status="ok", dur_s=9.0)

    res = flow.runs(str(tmp_path))
    assert res["run_id"] == "r2"  # most recent by default
    assert res["available_run_ids"] == ["r1", "r2"]
    assert res["command_count"] == 1 and res["beat_count"] == 1
    assert res["total_command_seconds"] == 2.0
    assert res["status_counts"] == {"ok": 1}
    assert res["beats"][0]["decision"] == "approved_first_pass"

    # An explicit run id selects an earlier run instead of the latest.
    r1 = flow.runs(str(tmp_path), run_id="r1")
    assert r1["command_count"] == 1 and r1["beat_count"] == 0

    # A project with no logged events returns a note, not a crash.
    other = tmp_path.parent / f"{tmp_path.name}_empty"
    other.mkdir()
    (other / "chunks").mkdir()
    empty = flow.runs(str(other))
    assert empty["run_id"] is None and "no run-log events" in empty["note"]


def test_align_command_aligns_ready_chapters_and_links(tmp_path: Path, monkeypatch):
    """align processes only fully-translated chapters and returns a reader link."""
    import src.sentence_aligner as aligner
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["A.", "B."],
                 translations=["es A.", "es B."])
    _save_chunks(chunks_dir, "chapter_02", sources=["C."])  # untranslated -> skipped

    calls: list[str] = []

    def fake_align(chunk_paths, project_id, chapter_id, source_lang="en",
                   target_lang="es", output_path=None):
        calls.append(chapter_id)
        if output_path:
            Path(output_path).write_text(
                json.dumps({"chapter_id": chapter_id, "alignments": []}), encoding="utf-8")
        return {"chapter_id": chapter_id, "es_count": 2, "high_confidence_pct": 100.0}

    monkeypatch.setattr(aligner, "align_chapter_chunks", fake_align)

    res = flow.align(str(tmp_path))
    assert calls == ["chapter_01"], "only fully-translated chapters are aligned"
    assert [a["chapter_id"] for a in res["aligned"]] == ["chapter_01"]
    assert [s["chapter_id"] for s in res["skipped"]] == ["chapter_02"]
    assert res["reader_first"].endswith(f"/read/{tmp_path.name}/chapter_01")
    assert (tmp_path / "alignments" / "chapter_01.json").exists()


def test_align_command_reports_aligner_failure_without_crashing(tmp_path: Path, monkeypatch):
    """An aligner exception is reported (error + skipped), not propagated."""
    import src.sentence_aligner as aligner
    from src.harness import flow

    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    _save_chunks(chunks_dir, "chapter_01", sources=["A."], translations=["es A."])

    def boom(*args, **kwargs):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(aligner, "align_chapter_chunks", boom)

    res = flow.align(str(tmp_path))
    assert res["aligned"] == []
    assert "error" in res and "embedding model unavailable" in res["error"]
    assert [s["chapter_id"] for s in res["skipped"]] == ["chapter_01"]
    assert res["reader_first"] is None
