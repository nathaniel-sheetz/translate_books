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
