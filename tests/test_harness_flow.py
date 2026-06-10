"""Offline tests for the harness orchestration layer (``src/harness/flow.py``).

The old SKILL.md drove the style-guide and glossary beats through ~nine inline-Python
heredocs that no test could exercise — the orchestration lived in markdown. Moving it
into ``flow`` makes the whole prepare -> (agent draft) -> commit contract testable with
a stubbed "agent draft": we write the draft files a real agent would write, then assert
the harness produces artifacts that validate against the Pydantic models, and that a
malformed draft fails loudly with a re-draft message instead of poisoning the run.

    FIXTURE SOURCE ─► setup ─► style-guide prepare/commit ─► glossary prepare/commit
                       │            │ (canned drafts)            │ (canned proposals)
                       ▼            ▼                            ▼
                  chapters/*     style.json                  glossary.json
                  (no chunks)    validates                   validates

The single LLM seam (translation) is covered by test_harness_pipeline.py; the paid
``translate`` wrapper is checked here only for its fail-closed-without-``--yes`` guard.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.harness import flow, state
from src.harness_guard import HarnessValidationError
from src.models import StyleGuide
from src.utils.file_io import save_style_guide

REPO_ROOT = Path(__file__).resolve().parents[1]


def _chapter_body(seed: str) -> str:
    # ~320 words so the splitter's 100-word minimum keeps the chapter.
    sentence = f"{seed} walked through the quiet village past the old well and the great oak tree. "
    return (sentence * 20).strip()


FIXTURE_SOURCE = (
    "CHAPTER I\n\n" + _chapter_body("Old Thomas") + "\n\n"
    "CHAPTER II\n\n" + _chapter_body("Young Betsy") + "\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project dir with source.txt + chapters/, the way setup leaves it."""
    proj = tmp_path / "fixturebook"
    proj.mkdir()
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")
    (proj / "chapters").mkdir()
    (proj / "chapters" / "chapter_01.txt").write_text(_chapter_body("Old Thomas"), encoding="utf-8")
    (proj / "chapters" / "chapter_02.txt").write_text(_chapter_body("Young Betsy"), encoding="utf-8")
    state.save_config(proj, {})  # write defaults so .harness/config.json exists
    return proj


# ── setup ──────────────────────────────────────────────────────────────────

def test_setup_runs_ingest_split_and_persists_config(tmp_path: Path):
    proj = tmp_path / "newbook"
    proj.mkdir()
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")

    result = flow.setup(str(proj), url="", target_language="Spanish", locale="mx",
                        model="claude-test", title="The Book", author="An Author")

    assert result["chapter_count"] == 2
    assert result["chunks_dir_exists"] is False  # chunking deferred to Step 3
    cfg = state.load_config(proj)
    assert cfg["model"] == "claude-test"
    assert cfg["title"] == "The Book"
    assert (proj / ".harness" / "config.json").exists()


# ── style guide beat ───────────────────────────────────────────────────────

def test_style_guide_beat_writes_valid_style_json(project: Path):
    prep = flow.style_guide_prepare_questions(str(project))
    assert prep["questions"], "expected at least the fixed questions"
    assert Path(prep["answers_path"]).parent.name == ".harness"

    # Agent answers nothing controversial (empty dict is valid: unanswered Qs are skipped).
    Path(prep["answers_path"]).write_text(json.dumps({}), encoding="utf-8")

    draft = flow.style_guide_prepare_draft(str(project))
    assert Path(draft["prompt_path"]).exists()

    # Agent drafts the style-guide prose.
    Path(draft["draft_path"]).write_text("TONE: warm, plain, period-faithful Mexican Spanish.",
                                         encoding="utf-8")
    committed = flow.style_guide_commit(str(project))

    assert committed["chars"] > 0
    guide = StyleGuide.model_validate_json((project / "style.json").read_text(encoding="utf-8"))
    assert "TONE" in guide.content


def test_commit_followups_merges_into_question_set(project: Path):
    prep = flow.style_guide_prepare_questions(str(project))
    base_count = len(prep["questions"])
    Path(prep["answers_path"]).write_text(json.dumps({}), encoding="utf-8")

    fup = flow.style_guide_prepare_followups(str(project))
    # Agent drafts one extra question.
    Path(fup["draft_path"]).write_text(json.dumps([
        {"id": "extra_dialogue", "question": "How should dialogue be punctuated?",
         "options": [{"label": "Em dashes"}, {"label": "Quotation marks"}]}
    ]), encoding="utf-8")

    out = flow.style_guide_commit_followups(str(project))
    assert len(out["new_questions"]) == 1
    merged = json.loads((project / ".harness" / "style_questions.json").read_text(encoding="utf-8"))
    assert len(merged) == base_count + 1


# ── glossary beat ──────────────────────────────────────────────────────────

def test_glossary_commit_writes_valid_glossary(project: Path):
    save_style_guide(StyleGuide(content="Keep proper names; warm register."),
                     project / "style.json")
    prep = flow.glossary_prepare(str(project))
    assert prep["style_guide_loaded"] is True
    assert Path(prep["prompt_path"]).exists()

    Path(prep["draft_path"]).write_text(json.dumps([
        {"english": "Thomas", "translation": "Tomás", "type": "character", "context": "the gardener"},
        {"english": "oak tree", "translation": "roble", "type": "other", "context": ""},
    ]), encoding="utf-8")

    out = flow.glossary_commit(str(project))
    assert out["term_count"] == 2
    assert {t["english"] for t in out["terms"]} == {"Thomas", "oak tree"}
    # File validates against the model (belt-and-suspenders the flow already ran).
    from src.harness_guard import validate_glossary_file
    assert len(validate_glossary_file(project / "glossary.json").terms) == 2


def test_glossary_commit_rejects_malformed_draft(project: Path):
    state.ensure_harness_dir(project)
    draft = project / ".harness" / "glossary_draft.json"
    draft.write_text(json.dumps([
        {"translation": "Tomás"},                       # missing 'english'
        {"english": "well", "type": "other"},           # missing translation
    ]), encoding="utf-8")

    with pytest.raises(HarnessValidationError) as exc:
        flow.glossary_commit(str(project))
    msg = str(exc.value)
    assert "english" in msg and "well" in msg          # names every problem to re-draft
    assert not (project / "glossary.json").exists()     # nothing poisoned


# ── difficulty ─────────────────────────────────────────────────────────────

def test_difficulty_returns_suggested_size(project: Path):
    out = flow.difficulty(str(project))
    assert isinstance(out["suggested_target_size"], int)
    assert out["suggested_target_size"] > 0
    assert len(out["chapters"]) == 2


# ── cost gate (the one paid step) ──────────────────────────────────────────

def test_translate_fails_closed_without_yes(project: Path):
    # Returns the non-zero refusal code WITHOUT invoking the paid subprocess.
    assert flow.translate(str(project), yes=False) == 2


def test_cli_translate_without_yes_exits_nonzero(project: Path):
    result = subprocess.run(
        [sys.executable, "scripts/harness.py", "translate", "--project", str(project)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "without --yes" in result.stderr
