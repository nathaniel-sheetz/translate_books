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
    # Well over the splitter's 100-character minimum so the chapter is kept.
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


def test_setup_returns_heading_hint_keys_null_on_no_url_path(tmp_path: Path):
    """The hint keys must always be present; on the no-URL path there is no HTML
    to read headings from, so they come back null."""
    proj = tmp_path / "newbook"
    proj.mkdir()
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")

    result = flow.setup(str(proj), url="", target_language="Spanish", locale="mx")

    assert "suggested_pattern" in result and result["suggested_pattern"] is None
    assert "chapter_report" in result and result["chapter_report"] is None


def test_setup_derives_folder_from_title_when_project_omitted(tmp_path: Path, monkeypatch):
    """With no --project, the folder is named from the (slugified) title (#22)."""
    monkeypatch.setattr(state, "REPO_ROOT", tmp_path)
    # Stand in for the dir the URL path would create; available_project_dir hands it
    # back so the no-URL ingest branch finds source.txt and the run completes.
    proj = tmp_path / "projects" / "understood-betsy"
    proj.mkdir(parents=True)
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")
    captured = {}

    def fake_available_project_dir(slug: str) -> Path:
        captured["slug"] = slug
        return proj

    monkeypatch.setattr(state, "available_project_dir", fake_available_project_dir)

    result = flow.setup(url="", title="Understood Betsy", target_language="Spanish")

    assert captured["slug"] == "understood-betsy"  # real slugify ran on the title
    assert Path(result["project_dir"]).name == "understood-betsy"
    assert result["chapter_count"] == 2


def test_setup_requires_project_or_title():
    """Neither --project nor --title is an actionable error, not a cryptic folder."""
    with pytest.raises(ValueError, match="--title"):
        flow.setup(url="")


# ── split review beat ───────────────────────────────────────────────────────

def _front_back_source() -> str:
    """A book with a declared front-matter heading and an auto-detectable
    back-matter heading bracketing two roman chapters."""
    return (
        "To the Teacher\n\n" + _chapter_body("A note") + "\n\n"
        "Chapter I\n\n" + _chapter_body("Old Thomas") + "\n\n"
        "Chapter II\n\n" + _chapter_body("Young Betsy") + "\n\n"
        "Afterword\n\n" + _chapter_body("The end") + "\n"
    )


def test_split_preview_tags_matter_and_writes_nothing(tmp_path: Path):
    proj = tmp_path / "book"
    proj.mkdir()
    (proj / "source.txt").write_text(_front_back_source(), encoding="utf-8")

    result = flow.split_preview(
        str(proj), pattern_type="roman",
        front_matter_titles=["To the Teacher"], back_matter_titles=["Afterword"],
    )

    assert [s["kind"] for s in result["sections"]] == [
        "front_matter", "chapter", "chapter", "back_matter",
    ]
    assert result["counts"] == {"front_matter": 1, "chapter": 2, "back_matter": 1}
    assert result["files_written"] is False
    assert not (proj / "chapters").exists()  # dry run writes nothing
    assert "dropped" in result  # boilerplate-stripping report always present
    assert isinstance(result["dropped"], list)


def test_split_preview_boilerplate_reported_in_dropped(tmp_path: Path):
    """Boilerplate sections detected by auto-strip appear in result['dropped']."""
    proj = tmp_path / "book"
    proj.mkdir()
    boilerplate = (
        "Contents\n\n" + "A " * 200 + "\n\n"  # short ToC-like section
        "CHAPTER I\n\n" + _chapter_body("Old Thomas") + "\n"
    )
    (proj / "source.txt").write_text(boilerplate, encoding="utf-8")

    result = flow.split_preview(str(proj), pattern_type="roman")

    assert "dropped" in result
    # Whether or not "Contents" is stripped, the key must be a list of dicts
    assert all(isinstance(d, dict) for d in result["dropped"])


def test_split_apply_writes_files_and_clears_stale(tmp_path: Path):
    proj = tmp_path / "book"
    proj.mkdir()
    (proj / "source.txt").write_text(_front_back_source(), encoding="utf-8")
    chapters_dir = proj / "chapters"
    chapters_dir.mkdir()
    (chapters_dir / "chapter_99.txt").write_text("stale", encoding="utf-8")  # from a prior split

    result = flow.split_apply(
        str(proj), pattern_type="roman",
        front_matter_titles=["To the Teacher"], back_matter_titles=["Afterword"],
    )

    assert result["chapter_count"] == 4
    assert result["files_written"] is True  # paired-API symmetry with split_preview
    assert result["counts"] == {"front_matter": 1, "chapter": 2, "back_matter": 1}
    written = sorted(p.name for p in chapters_dir.glob("chapter_*.txt"))
    assert written == [f"chapter_0{i}.txt" for i in range(1, 5)]
    assert not (chapters_dir / "chapter_99.txt").exists()  # stale orphan cleared
    assert "dropped" in result  # boilerplate-stripping report always present
    assert isinstance(result["dropped"], list)


def test_split_apply_min_chapter_size_filters_short_sections(tmp_path: Path):
    """A stray chapter whose body is below the threshold is dropped, not written."""
    proj = tmp_path / "book"
    proj.mkdir()
    source = (
        "Chapter I\n\n" + _chapter_body("Old Thomas") + "\n\n"
        "Chapter II\n\nToo short.\n"
    )
    (proj / "source.txt").write_text(source, encoding="utf-8")

    result = flow.split_apply(str(proj), pattern_type="roman", min_chapter_size=500)

    assert result["chapter_count"] == 1
    assert [s["number"] for s in result["sections"]] == [1]


# ── style guide beat ───────────────────────────────────────────────────────

def test_style_guide_beat_writes_valid_style_json(project: Path):
    prep = flow.style_guide_prepare_questions(str(project))
    assert prep["questions"], "expected at least the fixed questions"
    assert Path(prep["answers_path"]).parent.name == ".harness"

    # Each option is surfaced as an {id, label} pair so the agent can answer by id.
    first_q = prep["questions"][0]
    assert first_q["options"], "expected the question to carry options"
    assert all({"id", "label"} <= opt.keys() for opt in first_q["options"])

    # Answer the first question by its option id; leave the rest unanswered.
    chosen = first_q["options"][0]
    Path(prep["answers_path"]).write_text(
        json.dumps({first_q["id"]: chosen["id"]}), encoding="utf-8"
    )

    draft = flow.style_guide_prepare_draft(str(project))
    assert Path(draft["prompt_path"]).exists()

    # The answer resolved to the intended option (not silently demoted to custom).
    resolved = {r["id"]: r for r in draft["resolved_answers"]}
    assert resolved[first_q["id"]]["source"] == "option"
    assert resolved[first_q["id"]]["answer"] == chosen["label"]
    # Every other question is reported unanswered.
    assert first_q["id"] not in draft["unanswered"]
    assert len(draft["unanswered"]) == len(prep["questions"]) - 1

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
    new_opts = out["new_questions"][0]["options"]
    assert [o["id"] for o in new_opts] == ["em_dashes", "quotation_marks"]
    merged = json.loads((project / ".harness" / "style_questions.json").read_text(encoding="utf-8"))
    assert len(merged) == base_count + 1


def test_prepare_questions_prefills_dialect_from_locale(project: Path):
    # The project fixture inherits the default locale ("mx").
    prep = flow.style_guide_prepare_questions(str(project))
    dialect = next(q for q in prep["questions"] if q["id"] == "dialect")
    assert dialect["prefilled"] == "mexican_spanish"
    assert "mx" in dialect["prefilled_reason"]
    assert "prefilled" in prep["instructions"]


def test_prepare_questions_no_prefill_for_unmapped_locale(project: Path):
    cfg = state.load_config(project)
    cfg["locale"] = "fr-FR"
    state.save_config(project, cfg)

    prep = flow.style_guide_prepare_questions(str(project))
    dialect = next(q for q in prep["questions"] if q["id"] == "dialect")
    assert "prefilled" not in dialect
    assert "prefilled" not in prep["instructions"]


def test_prepare_draft_fills_dialect_from_locale_when_omitted(project: Path):
    flow.style_guide_prepare_questions(str(project))
    # Agent confirms the prefilled dialect by default and writes NO dialect answer.
    Path(project / ".harness" / "style_answers.json").write_text(
        json.dumps({}), encoding="utf-8"
    )

    draft = flow.style_guide_prepare_draft(str(project))
    resolved = {r["id"]: r for r in draft["resolved_answers"]}
    assert "dialect" in resolved
    assert resolved["dialect"]["source"] == "option"
    assert resolved["dialect"]["answer"] == "Mexican Spanish"
    assert "dialect" not in draft["unanswered"]


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
    # A correct draft (carries "Tomás") raises no accent-stripping warning (#21).
    assert out["warnings"] == []
    # File validates against the model (belt-and-suspenders the flow already ran).
    from src.harness_guard import validate_glossary_file
    assert len(validate_glossary_file(project / "glossary.json").terms) == 2


def test_glossary_commit_warns_on_ascii_folded_spanish(project: Path):
    # An all-ASCII Spanish glossary commits, but surfaces a non-blocking accent-stripping
    # warning the approval gate shows the user (#21).
    state.ensure_harness_dir(project)
    draft = project / ".harness" / "glossary_draft.json"
    draft.write_text(json.dumps([
        {"english": f"e{i}", "translation": w}
        for i, w in enumerate(["senor", "lenera", "manana", "Tia", "Dia", "nino", "arbol", "cancion"])
    ]), encoding="utf-8")

    out = flow.glossary_commit(str(project))
    assert out["term_count"] == 8
    assert out["warnings"] and "accent" in out["warnings"][0].lower()


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


# ── per-chapter chunk sizing ────────────────────────────────────────────────

def test_chunk_per_chapter_without_difficulty_raises(project: Path, monkeypatch):
    """--per-chapter needs a prior difficulty run; fail loudly before any spend."""
    called = []
    monkeypatch.setattr(flow, "_run_script", lambda cmd: called.append(cmd))
    with pytest.raises(FileNotFoundError) as exc:
        flow.chunk(str(project), size=1800, per_chapter=True)
    assert "difficulty" in str(exc.value)
    assert not called, "_run_script must not be reached before the guard raises"
    assert not (project / ".harness" / "chunk_sizes.json").exists()


def test_chunk_per_chapter_writes_sizes_map(project: Path, monkeypatch):
    """After difficulty, --per-chapter writes the per-chapter map and passes it down."""
    flow.difficulty(str(project))  # writes difficulty.json (per-chapter suggestions)

    captured: dict = {}

    def _fake_run(cmd):
        captured["cmd"] = cmd
        return 0, None  # (returncode, captured HARNESS_RESULT summary)

    monkeypatch.setattr(flow, "_run_script", _fake_run)

    result = flow.chunk(str(project), size=1800, per_chapter=True)
    # Streaming commands now return a fresh dict carrying the exit code (friction-log #18).
    assert result["command"] == "chunk" and result["exit_code"] == 0

    sizes_path = project / ".harness" / "chunk_sizes.json"
    assert sizes_path.exists()
    sizes = json.loads(sizes_path.read_text(encoding="utf-8"))
    assert set(sizes) == {"chapter_01", "chapter_02"}
    assert all(isinstance(v, int) and v > 0 for v in sizes.values())

    cmd = captured["cmd"]
    assert "--chunk-sizes" in cmd and str(sizes_path) in cmd
    assert "--chunk-size" in cmd and "1800" in cmd  # fallback still passed


def test_chunk_without_per_chapter_omits_sizes_map(project: Path, monkeypatch):
    """The uniform path passes no --chunk-sizes and writes no map."""
    captured: dict = {}
    monkeypatch.setattr(flow, "_run_script", lambda cmd: (captured.update(cmd=cmd), (0, None))[1])

    flow.chunk(str(project), size=1800)
    assert "--chunk-sizes" not in captured["cmd"]
    assert not (project / ".harness" / "chunk_sizes.json").exists()


# ── fresh, self-documenting last_output.json (friction-log #18, #19) ─────────

def test_streaming_command_refreshes_last_output(project: Path, monkeypatch):
    """Friction-log #18: a streaming command (chunk/cost/translate/epub) must overwrite
    last_output.json with ITS OWN fresh result — never leave the previous command's behind."""
    import scripts.harness as harness

    artifact = project / ".harness" / "last_output.json"
    # Plant a STALE prior result, as a preceding `difficulty` run would have left it.
    artifact.write_text(
        json.dumps({"book_difficulty": 0.42, "suggested_target_size": 1800, "chapters": []}),
        encoding="utf-8",
    )

    # Stub the wrapped subprocess: no spend, a fresh chunk summary via the sentinel.
    monkeypatch.setattr(flow, "_run_script", lambda cmd: (0, {
        "stage": "cost-estimate", "total_chunks_in_scope": 7, "chunks_needing_translation": 7,
    }))
    monkeypatch.setattr(
        sys, "argv", ["harness.py", "chunk", "--project", str(project), "--size", "1500"])

    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 0  # the wrapped exit code is propagated (cost gate intact)

    fresh = json.loads(artifact.read_text(encoding="utf-8"))
    assert fresh["command"] == "chunk" and fresh["exit_code"] == 0
    assert fresh["total_chunks_in_scope"] == 7
    assert "book_difficulty" not in fresh, "the stale difficulty result must be gone"
    assert "total_chunks_in_scope" in fresh["_schema"]  # self-documents its keys (#19)


def test_streaming_refusal_still_refreshes_artifact(project: Path, monkeypatch):
    """Even a pre-flight refusal (translate without --yes -> bare int 2) leaves a fresh
    minimal artifact, so a prior result can't be mistaken for this command (friction-log #18)."""
    import scripts.harness as harness

    artifact = project / ".harness" / "last_output.json"
    artifact.write_text(json.dumps({"book_difficulty": 0.42}), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["harness.py", "translate", "--project", str(project)])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 2

    fresh = json.loads(artifact.read_text(encoding="utf-8"))
    assert fresh["command"] == "translate" and fresh["exit_code"] == 2
    assert "book_difficulty" not in fresh


def test_dict_command_artifact_carries_schema(project: Path, monkeypatch):
    """Friction-log #19: every artifact self-documents its keys under _schema, so the agent
    reads the schema instead of guessing field names."""
    import scripts.harness as harness

    monkeypatch.setattr(sys, "argv", ["harness.py", "difficulty", "--project", str(project)])
    harness.main()  # a dict command returns normally (no SystemExit)

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["difficulty"]
    for key in ("book_difficulty", "suggested_target_size", "chapters"):
        assert key in out and key in out["_schema"]


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
