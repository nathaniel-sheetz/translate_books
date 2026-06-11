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
from src.models import ChunkStatus, Glossary, GlossaryTerm, StyleGuide
from src.utils.file_io import save_glossary, save_style_guide

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
        chunk_size=2000, overlap_paragraphs=1, min_overlap_words=50,
        provider="anthropic", model="claude-sonnet-4-20250514",
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

    # Idempotent: re-commit touches nothing (resume safety).
    res2 = flow.translate_commit(str(project), worker_model="sonnet")
    assert res2["counts"]["committed"] == 0
    assert res2["counts"]["skipped"] == len(prep["manifest"])

    # combine + epub build from the subagent-translated chunks.
    state: dict = {}
    for stage in ("combine", "epub"):
        state = tb.STAGE_FUNCTIONS[stage](_args(), project, state)
    epubs = list(project.rglob("*.epub"))
    assert epubs, "no EPUB produced from subagent-translated chunks"
    with zipfile.ZipFile(epubs[0]) as z:
        assert z.testzip() is None
        assert any(n.endswith((".xhtml", ".html")) for n in z.namelist())


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
