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


def test_setup_derives_heading_hints_on_no_url_path(tmp_path: Path):
    """On the local source.txt path the hints are now derived from the text
    (friction #1/#2): suggested_pattern + a per-chapter report + pattern_used,
    instead of the old nulls that gave the agent no under-split signal."""
    proj = tmp_path / "newbook"
    proj.mkdir()
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")

    result = flow.setup(str(proj), url="", target_language="Spanish", locale="mx")

    assert result["suggested_pattern"] is not None
    assert result["pattern_used"] == result["suggested_pattern"]  # auto honored the detection
    assert isinstance(result["chapter_report"], list) and len(result["chapter_report"]) == 2
    assert set(result["chapter_report"][0]) == {"number", "heading", "words", "chunks"}
    assert result["warnings"] == []  # 2 chapters, small source: nothing to flag


def test_setup_warns_when_forced_pattern_under_splits(tmp_path: Path):
    """A forced pattern that collapses a large source to one chapter must emit a
    warning AND still surface the pattern the text actually suggests."""
    proj = tmp_path / "bigbook"
    proj.mkdir()
    # ~32 KB source. One bare 'CHAPTER I' (which 'roman' matches) and one
    # same-line titled heading (which it does NOT — the numeral must end the
    # line), so 'roman' collapses everything into a single chapter. This is the
    # Photogen shape that under-split silently.
    body = "Lorem ipsum dolor sit amet consectetur. " * 400  # ~16 KB
    source = f"CHAPTER I\n\n{body}\n\nCHAPTER II. THE SECOND\n\n{body}"
    (proj / "source.txt").write_text(source, encoding="utf-8")

    result = flow.setup(str(proj), url="", chapter_pattern="roman",
                        target_language="Spanish", locale="mx")

    assert result["chapter_count"] == 1
    assert result["pattern_used"] == "roman"
    assert result["suggested_pattern"] == "chapter_roman_titled"
    assert result["warnings"] and "may be wrong" in result["warnings"][0]


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


def test_glossary_prepare_reads_clean_chapters_not_front_matter(project: Path):
    # A front-matter-only sentinel (fake publisher) repeated enough to clear
    # min_frequency, planted ONLY in source.txt. Because glossary_prepare now
    # extracts from the clean chapters/ text, the sentinel must NOT surface in
    # the prompt — proving front matter (TOC/copyright) is excluded (#27).
    front_matter = (
        "Published by Zorblatt and Zorblatt Press. "
        "Copyright Zorblatt. All rights reserved by Zorblatt. "
        "Printed for Zorblatt by Zorblatt House.\n\n"
    )
    (project / "source.txt").write_text(front_matter + FIXTURE_SOURCE, encoding="utf-8")

    prep = flow.glossary_prepare(str(project))

    # The fixture has chapters/ but no chunks/, so the clean loader uses chapters/.
    assert prep["source_kind"] == "chapters"
    prompt = Path(prep["prompt_path"]).read_text(encoding="utf-8")
    assert "Zorblatt" not in prompt
    # Control: a real character from the chapter body is still extracted.
    assert "Thomas" in prompt


def test_glossary_prepare_threads_max_candidates_through_to_extractor(project: Path):
    # Regression for the double-truncation bug: glossary_prepare used to extract
    # at the extractor's own default cap and then re-slice the (already-ranked)
    # result to max_candidates, so a caller could never see more than that
    # internal default. It now passes max_candidates straight into
    # extract_candidates, so the extractor ranks and truncates directly against
    # the requested cap.
    def name_body(i: int) -> str:
        sentence = f"Aldric{i} Thorne{i} walked through the quiet village past the old well. "
        return (sentence * 3).strip()

    names_text = "\n\n".join(
        f"CHAPTER {i + 1}\n\n{name_body(i)}" for i in range(210)
    )
    (project / "chapters" / "chapter_01.txt").write_text(names_text, encoding="utf-8")
    (project / "chapters" / "chapter_02.txt").write_text("", encoding="utf-8")

    # Default cap (500) must not silently behave like the old hardcoded 200 —
    # all 210 qualifying names should survive.
    prep_default = flow.glossary_prepare(str(project))
    assert prep_default["candidate_count"] == 210

    # An explicit cap below what's available must still be honored.
    prep_capped = flow.glossary_prepare(str(project), max_candidates=50)
    assert prep_capped["candidate_count"] == 50


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


def test_chunk_and_cost_pass_configured_model_and_provider(project: Path, monkeypatch):
    """chunk/cost must pass the persisted config model+provider to translate_book.py
    so the cost estimate reflects the configured model, not the CLI default."""
    state.save_config(project, {"model": "claude-sonnet-4-6", "provider": "anthropic"})
    captured: dict = {}

    def _capture(cmd):
        captured["cmd"] = cmd
        return 0, None

    monkeypatch.setattr(flow, "_run_script", _capture)

    flow.chunk(str(project), size=1500)
    cmd = captured["cmd"]
    assert "--model" in cmd and "claude-sonnet-4-6" in cmd
    assert "--provider" in cmd and "anthropic" in cmd

    flow.cost(str(project))
    cmd = captured["cmd"]
    assert "--model" in cmd and "claude-sonnet-4-6" in cmd
    assert "--provider" in cmd and "anthropic" in cmd


def test_append_always_include_flags_emits_expected_combinations():
    """_append_always_include_flags maps harness config to explicit translate_book.py
    flags. Both keys are tri-state: only True/False become a flag, None stays unset."""
    cmd: list[str] = []
    flow._append_always_include_flags(
        cmd, {"always_include_dialogue": True, "always_include_image_instructions": True}
    )
    assert cmd == ["--always-dialogue", "--always-images"]

    cmd = []
    flow._append_always_include_flags(
        cmd, {"always_include_dialogue": False, "always_include_image_instructions": False}
    )
    assert cmd == ["--no-always-dialogue", "--no-always-images"]

    # Absent / None → no flag at all for either key, so translate_book.py runs the
    # same auto-derivation translate_prepare does. Forcing an explicit --no- here
    # would pin the estimate to a prompt that will never be sent.
    cmd = []
    flow._append_always_include_flags(cmd, {})
    assert cmd == []

    cmd = []
    flow._append_always_include_flags(
        cmd,
        {"always_include_dialogue": None, "always_include_image_instructions": None},
    )
    assert cmd == []


def test_chunk_threads_always_include_flags_from_config(project: Path, monkeypatch):
    """chunk() must forward the flags too. It was the one wrapper that did not, so its
    preflight reported always_include_dialogue: false for every book regardless of
    config, and estimated against a prompt nobody would send."""
    state.save_config(
        project,
        {"always_include_dialogue": True, "always_include_image_instructions": False},
    )
    captured: dict = {}

    def _capture(cmd):
        captured["cmd"] = cmd
        return 0, None

    monkeypatch.setattr(flow, "_run_script", _capture)

    flow.chunk(str(project), size=2000)
    cmd = captured["cmd"]
    assert "--always-dialogue" in cmd
    assert "--no-always-images" in cmd


def test_chunk_hints_at_the_cache_fix_only_when_pinned_off(project: Path, monkeypatch):
    """A book that pins dialogue off AND has a mixed split runs uncached on headless.
    chunk is the beat that prints both numbers, so it carries the fix command."""
    def _mixed(_cmd):
        return 0, {"dialogue_chunk_count": 27, "total_chunks_in_scope": 28}

    monkeypatch.setattr(flow, "_run_script", _mixed)

    state.save_config(project, {"always_include_dialogue": False})
    hint = flow.chunk(str(project), size=2000).get("cache_prefix_hint")
    assert hint and "config-set" in hint and "always_include_dialogue" in hint

    # Auto (the default) already handles the mixed case — no hint to give.
    state.save_config(project, {"always_include_dialogue": None})
    assert "cache_prefix_hint" not in flow.chunk(str(project), size=2000)

    # Uniform book: the prefix is stable either way, so off is not a problem.
    def _uniform(_cmd):
        return 0, {"dialogue_chunk_count": 28, "total_chunks_in_scope": 28}

    monkeypatch.setattr(flow, "_run_script", _uniform)
    state.save_config(project, {"always_include_dialogue": False})
    assert "cache_prefix_hint" not in flow.chunk(str(project), size=2000)


def test_cost_threads_always_include_flags_from_config(project: Path, monkeypatch):
    """cost() must append the always-include flags so translate_book.py's preflight
    matches the book's saved caching preference."""
    state.save_config(
        project,
        {"always_include_dialogue": True, "always_include_image_instructions": False},
    )
    captured: dict = {}

    def _capture(cmd):
        captured["cmd"] = cmd
        return 0, None

    monkeypatch.setattr(flow, "_run_script", _capture)

    flow.cost(str(project))
    cmd = captured["cmd"]
    assert "--always-dialogue" in cmd
    assert "--no-always-images" in cmd


def test_harness_default_model_is_sonnet_5(tmp_path: Path):
    """Empty config.json inherits the Sonnet 5 default."""
    proj = tmp_path / "defaults"
    proj.mkdir()
    state.save_config(proj, {})
    assert state.load_config(proj)["model"] == "claude-sonnet-5"


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
    assert "_schema" not in fresh
    assert fresh["_schema_path"].endswith("last_output_schema.json")
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert "total_chunks_in_scope" in sidecar
    assert sidecar == flow.OUTPUT_SCHEMAS["chunk"]


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


def test_dict_command_artifact_uses_schema_sidecar(project: Path, monkeypatch):
    """Friction-log #19 / bambi #4: successful artifacts point at a schema sidecar
    instead of inlining ``_schema``, so a Read of last_output.json stays cheap."""
    import scripts.harness as harness

    monkeypatch.setattr(sys, "argv", ["harness.py", "difficulty", "--project", str(project)])
    harness.main()  # a dict command returns normally (no SystemExit)

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert "_schema" not in out
    # Pointer + key names first, so a stray tail lands on result fields.
    assert list(out.keys())[:2] == ["_schema_path", "_schema_keys"]
    assert out["_schema_path"].endswith("last_output_schema.json")
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert sidecar == flow.OUTPUT_SCHEMAS["difficulty"]
    for key in ("book_difficulty", "suggested_target_size", "chapters"):
        assert key in out and key in sidecar


def test_schema_keys_names_every_documented_key(project: Path, monkeypatch):
    """Bambi §3b: result keys differ per verb (manifest / chapters / aligned), and the
    agent guessed one rather than Read a whole schema file. The NAMES ride along free."""
    import scripts.harness as harness

    monkeypatch.setattr(sys, "argv", ["harness.py", "status", "--project", str(project)])
    harness.main()

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["_schema_keys"] == list(flow.OUTPUT_SCHEMAS["status"])
    # Descriptions stay in the sidecar — names only, or this re-inlines the schema.
    assert all(isinstance(k, str) for k in out["_schema_keys"])
    # Covers keys this payload does not carry, which is the part a payload can't self-report.
    assert set(out["_schema_keys"]) >= set(out) - {"_schema_path", "_schema_keys"}


def test_schema_flag_inlines_schema(project: Path, monkeypatch):
    """``--schema`` restores the old inline ``_schema`` block on success."""
    import scripts.harness as harness

    monkeypatch.setattr(
        sys, "argv",
        ["harness.py", "difficulty", "--project", str(project), "--schema"],
    )
    harness.main()

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["difficulty"]
    assert list(out.keys())[0] == "_schema", "inline meta must lead so a stray tail keeps results"
    assert "_schema_path" not in out
    # No _schema_keys either — the inline block already names every key.
    assert "_schema_keys" not in out
    # Sidecar is still written so a later Read of it stays valid.
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert sidecar == flow.OUTPUT_SCHEMAS["difficulty"]


def test_sidecar_write_failure_falls_back_to_inline_schema(project: Path, monkeypatch, capsys):
    """A pointer is only stamped when THIS call wrote the file it points at.

    Stamping `_schema_path` before the best-effort write meant a failed write left the
    payload naming a missing file — or the PREVIOUS command's sidecar, still on disk and
    describing a different verb, which an agent would Read and believe.
    """
    import scripts.harness as harness

    real_write_text = Path.write_text

    def flaky_write(self, *args, **kwargs):
        if self.name == "last_output_schema.json":
            raise OSError("no space left on device")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write)
    monkeypatch.setattr(sys, "argv", ["harness.py", "difficulty", "--project", str(project)])
    harness.main()

    assert not (project / ".harness" / "last_output_schema.json").exists()
    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert "_schema_path" not in out and "_schema_keys" not in out
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["difficulty"], "self-docs dropped entirely"
    assert list(out.keys())[0] == "_schema"
    assert out["book_difficulty"] is not None, "the command itself still succeeded"
    # The artifact wrote fine, so its stderr pointer must survive a schema-only failure.
    assert "OUTPUT_JSON:" in capsys.readouterr().err


def test_error_payload_inlines_schema_without_flag(project: Path, monkeypatch):
    """Soft-error dicts always carry ``_schema`` inline — never ask the agent to re-run."""
    import scripts.harness as harness

    monkeypatch.setattr(
        sys, "argv",
        ["harness.py", "translate-prepare", "--project", str(project)],
    )
    harness.main()

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out.get("error")
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["translate-prepare"]
    assert list(out.keys())[0] == "_schema", "inline meta must lead so a stray tail keeps results"
    assert "_schema_path" not in out and "_schema_keys" not in out


def test_nested_subparser_accepts_schema_flag(project: Path, monkeypatch):
    """Nested groups (address-map / footnotes / …) must accept ``--schema`` on the leaf."""
    import scripts.harness as harness

    monkeypatch.setattr(
        sys, "argv",
        ["harness.py", "address-map", "precheck", "--project", str(project), "--schema"],
    )
    harness.main()

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["address-map precheck"]


def test_streaming_nonzero_exit_inlines_schema(project: Path, monkeypatch):
    """A streaming command that fails still inlines ``_schema`` (shape needed most)."""
    import scripts.harness as harness

    monkeypatch.setattr(
        flow, "_run_script",
        lambda cmd: (1, None, "Template file not found: prompts/translation.txt"),
    )
    monkeypatch.setattr(
        sys, "argv", ["harness.py", "cost", "--project", str(project)],
    )
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 1

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["exit_code"] == 1
    assert out["_schema"] == flow.OUTPUT_SCHEMAS["cost"]
    assert "_schema_path" not in out


def test_stream_result_keeps_harness_authoritative_keys():
    """A wrapped script's HARNESS_RESULT sentinel must never overwrite the harness's
    own command name or the real process exit code recorded in last_output.json."""
    rogue = {"command": "spoofed", "exit_code": 99, "stage": "done"}
    result = flow._stream_result("translate", rc=0, summary=rogue)
    assert result["command"] == "translate"  # harness command wins, not "spoofed"
    assert result["exit_code"] == 0           # real exit code wins, not the sentinel's 99
    assert result["stage"] == "done"          # non-conflicting summary keys pass through


def test_stream_result_without_summary_is_minimal_dict():
    """No sentinel -> still a fresh dict carrying command + exit_code (never a bare int)."""
    assert flow._stream_result("epub", rc=1, summary=None) == {
        "command": "epub", "exit_code": 1}


# ── error capture (friction-log #6) ────────────────────────────────────────

def test_stream_result_fills_error_on_failure():
    """A failure must carry a readable cause: last_output.json used to record
    exit_code 1 with error: null, leaving the diagnosis only in unparsed stdout."""
    result = flow._stream_result(
        "cost", rc=1, summary=None,
        error="Template file not found: prompts/translation.txt")
    assert result["error"] == "Template file not found: prompts/translation.txt"


def test_stream_result_omits_error_on_success():
    """OUTPUT_SCHEMAS document error as present only on failure."""
    result = flow._stream_result("chunk", rc=0, summary=None, error="stale noise")
    assert "error" not in result


def test_stream_result_sentinel_error_beats_scraped_line():
    """A structured error from the sentinel is more specific than a scraped line."""
    result = flow._stream_result(
        "translate", rc=1, summary={"error": "specific cause"}, error="scraped line")
    assert result["error"] == "specific cause"


@pytest.mark.parametrize("line, expected", [
    ("  ERROR in translate: Template file not found: prompts/translation.txt",
     "Template file not found: prompts/translation.txt"),
    ("ERROR: something broke", "something broke"),
    ("ERROR in epub: missing metadata", "missing metadata"),
])
def test_script_error_regex_extracts_message(line, expected):
    assert flow._SCRIPT_ERROR_RE.match(line).group("msg") == expected


@pytest.mark.parametrize("line", [
    "  no error here",
    "The ERROR was recovered",  # not at line start: prose, not a failure report
    "",
])
def test_script_error_regex_ignores_non_errors(line):
    assert flow._SCRIPT_ERROR_RE.match(line) is None


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


# ── footnotes (imported Gutenberg reader footnotes) ─────────────────────────

def test_setup_threads_footnotes_and_surfaces_detection(tmp_path: Path, monkeypatch):
    """setup defaults to --footnotes import and threads it into stage_ingest, then surfaces
    the ingest's detection (footnotes_detected / footnotes_mode). Without this field the
    harness always dropped footnotes (friction #2)."""
    import scripts.translate_book as tb

    proj = tmp_path / "fnbook"
    proj.mkdir()
    seen: dict = {}

    def fake_ingest(args, project_dir, st):
        seen["footnotes"] = getattr(args, "footnotes", "MISSING")
        (project_dir / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")
        st.update(stage_completed="ingest", footnote_count=3,
                  footnote_mode=args.footnotes, source_words=123)
        return st

    def fake_split(args, project_dir, st):
        (project_dir / "chapters").mkdir(exist_ok=True)
        (project_dir / "chapters" / "chapter_01.txt").write_text(
            _chapter_body("A"), encoding="utf-8")
        st["stage_completed"] = "split"
        return st

    monkeypatch.setattr(tb, "stage_ingest", fake_ingest)
    monkeypatch.setattr(tb, "stage_split", fake_split)
    monkeypatch.setattr(flow, "_pattern_hints", lambda *a, **k: {
        "pattern_used": "auto", "detected": "auto", "warnings": [], "sections": [],
        "outline_report": None, "heading_outline": None, "dropped": []})

    result = flow.setup(str(proj), url="https://example.test/book.html", title="T", author="A")
    assert seen["footnotes"] == "import"  # the harness default is import
    assert result["footnotes_detected"] == 3
    assert result["footnotes_mode"] == "import"

    # An explicit --footnotes drop threads through unchanged.
    flow.setup(str(proj), url="https://example.test/book.html", title="T", author="A",
               footnotes="drop")
    assert seen["footnotes"] == "drop"


def test_setup_schema_documents_footnote_keys():
    for key in ("footnotes_detected", "footnotes_mode"):
        assert key in flow.OUTPUT_SCHEMAS["setup"]


def test_setup_no_url_path_reports_no_footnotes(tmp_path: Path):
    """The local source.txt path skips footnote detection, so the fields read 0 / None
    (there is nothing to prompt about)."""
    proj = tmp_path / "localbook"
    proj.mkdir()
    (proj / "source.txt").write_text(FIXTURE_SOURCE, encoding="utf-8")
    result = flow.setup(str(proj), url="", title="T", author="A")
    assert result["footnotes_detected"] == 0
    assert result["footnotes_mode"] is None


def _write_sidecar(project: Path, notes: list[dict]) -> None:
    (project / "footnotes.json").write_text(
        json.dumps(notes, ensure_ascii=False), encoding="utf-8")


def test_footnotes_translate_fails_closed_without_yes(project: Path, monkeypatch):
    """The paid note-body translation refuses without --yes and never spawns the script."""
    _write_sidecar(project, [{"number": 1, "source_body": "x"}])
    monkeypatch.setattr(flow, "_run_script",
                        lambda cmd: pytest.fail("must not spend without --yes"))
    assert flow.footnotes_translate(str(project), yes=False) == 2


def test_footnotes_translate_no_sidecar_is_noop(project: Path, monkeypatch):
    """No footnotes.json -> a clean no-op, not a spend or an error, even with --yes."""
    monkeypatch.setattr(flow, "_run_script",
                        lambda cmd: pytest.fail("nothing imported -> must not spend"))
    result = flow.footnotes_translate(str(project), yes=True)
    assert result["exit_code"] == 0 and result["translated"] == 0
    assert "note" in result


def test_footnotes_translate_yes_invokes_translator(project: Path, monkeypatch):
    """With --yes it wraps translate_footnotes.py with the config-derived flags and reports
    translated/pending counts read back from the sidecar."""
    import scripts.harness as harness

    _write_sidecar(project, [
        {"number": 1, "source_body": "x", "translated_body": "y"},
        {"number": 2, "source_body": "z"},
    ])
    captured: dict = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        return (0, None, None)

    monkeypatch.setattr(flow, "_run_script", fake_run)
    monkeypatch.setattr(sys, "argv",
                        ["harness.py", "footnotes", "translate", "--project", str(project), "--yes"])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 0
    assert "scripts/translate_footnotes.py" in captured["cmd"]
    assert "--project-dir" in captured["cmd"] and "--target-lang" in captured["cmd"]

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert (out["total"], out["translated"], out["pending"]) == (2, 1, 1)
    assert "_schema" not in out
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert sidecar == flow.OUTPUT_SCHEMAS["footnotes translate"]


def test_footnotes_apply_invokes_footnotes_stage(project: Path, monkeypatch):
    """apply runs only translate_book's footnotes stage (with book metadata) and reports the
    written count + rebuilt EPUB from the checkpoint."""
    import scripts.harness as harness

    _write_sidecar(project, [{"number": 1, "source_body": "x"}])
    captured: dict = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        (project / "pipeline_state.json").write_text(
            json.dumps({"footnotes_written": 4, "epub_path": str(project / "b.epub")}),
            encoding="utf-8")
        return (0, None, None)

    monkeypatch.setattr(flow, "_run_script", fake_run)
    monkeypatch.setattr(sys, "argv",
                        ["harness.py", "footnotes", "apply", "--project", str(project)])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 0
    assert "scripts/translate_book.py" in captured["cmd"]
    assert "--start-stage" in captured["cmd"] and "footnotes" in captured["cmd"]
    assert "--project-name" in captured["cmd"]

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["footnotes_written"] == 4 and out["epub_path"].endswith("b.epub")
    assert "_schema" not in out
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert sidecar == flow.OUTPUT_SCHEMAS["footnotes apply"]


def test_footnotes_drop_strips_tokens_and_removes_sidecar(project: Path):
    """drop reverts an import locally: strip [FOOTNOTE:N] from source.txt + chapters and
    delete the sidecar (the Step 0 'drop' choice, no re-fetch)."""
    (project / "source.txt").write_text("Hello[FOOTNOTE:1] world[FOOTNOTE:2].", encoding="utf-8")
    (project / "chapters" / "chapter_01.txt").write_text("Ch one[FOOTNOTE:3] end.", encoding="utf-8")
    # chapter_02 (from the fixture) has no tokens and must be left untouched.
    _write_sidecar(project, [{"number": 1, "source_body": "x"}])

    result = flow.footnotes_drop(str(project))
    assert result["tokens_stripped"] == 3
    assert result["files_cleaned"] == 2  # source.txt + chapter_01
    assert result["sidecar_removed"] is True
    assert not (project / "footnotes.json").exists()
    assert "[FOOTNOTE:" not in (project / "source.txt").read_text(encoding="utf-8")
    assert "[FOOTNOTE:" not in (project / "chapters" / "chapter_01.txt").read_text(encoding="utf-8")
    for key in result:  # every returned key is documented (#19)
        assert key in flow.OUTPUT_SCHEMAS["footnotes drop"], key


def test_footnotes_apply_noop_without_sidecar(project: Path, monkeypatch):
    import scripts.harness as harness

    called = {"n": 0}

    def boom(cmd):
        called["n"] += 1
        raise AssertionError(f"should not run script: {cmd}")

    monkeypatch.setattr(flow, "_run_script", boom)
    monkeypatch.setattr(sys, "argv",
                        ["harness.py", "footnotes", "apply", "--project", str(project)])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 0
    assert called["n"] == 0
    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert out["footnotes_written"] == 0
    assert "no footnotes.json" in out.get("note", "")


def test_footnote_counts_bad_json_is_zero(project: Path):
    sidecar = project / "footnotes.json"
    sidecar.write_text("{broken", encoding="utf-8")
    assert flow._footnote_counts(sidecar) == {"total": 0, "translated": 0, "pending": 0}


def test_footnotes_drop_updates_pipeline_state(project: Path):
    (project / "source.txt").write_text("Hi[FOOTNOTE:1].", encoding="utf-8")
    _write_sidecar(project, [{"number": 1, "source_body": "x"}])
    (project / "pipeline_state.json").write_text(
        json.dumps({"footnote_mode": "import", "footnote_count": 3}),
        encoding="utf-8",
    )
    flow.footnotes_drop(str(project))
    pstate = json.loads((project / "pipeline_state.json").read_text(encoding="utf-8"))
    assert pstate["footnote_mode"] == "drop"
    assert pstate["footnote_count"] == 0
    assert not (project / "footnotes.json").exists()


# ── retranslate / combine plumbing ─────────────────────────────────────────

def _translated_chunk(project: Path) -> Path:
    """Give the fixture project one fully-translated chunk."""
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from src.utils.file_io import save_chunk

    chunks_dir = project / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    src = "A short source sentence."
    chunk = Chunk(
        id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
        source_text=src, translated_text="Una frase breve.",
        metadata=ChunkMetadata(char_start=0, char_end=len(src), overlap_start=0,
                               overlap_end=0, paragraph_count=1,
                               word_count=len(src.split())),
        status=ChunkStatus.TRANSLATED,
    )
    cp = chunks_dir / "chapter_01_chunk_000.json"
    save_chunk(chunk, cp)
    return cp


def test_retranslate_and_combine_schemas_document_every_key(project: Path):
    """Friction-log #19 in its strong form: no key ships undocumented."""
    _translated_chunk(project)

    for command, result in (
        ("retranslate", flow.retranslate(str(project))),
        ("combine", flow.combine(str(project))),
    ):
        schema = flow.OUTPUT_SCHEMAS[command]
        undocumented = sorted(set(result) - set(schema))
        assert not undocumented, f"{command} returns undocumented keys: {undocumented}"


def test_translate_commit_and_status_schemas_document_the_recombine_keys():
    """The new combine seam has to be discoverable from _schema alone."""
    commit = flow.OUTPUT_SCHEMAS["translate-commit"]
    assert "recombined" in commit and "combine_failed" in commit
    assert "recombined" in commit["counts"]
    assert "combine_stale" in flow.OUTPUT_SCHEMAS["status"]


# ── OUTPUT_SCHEMAS completeness, statically ────────────────────────────────
#
# Since 0.40.4.0 the schema is not documentation: ``_schema_keys`` is derived from
# it and SKILL.md tells the agent to read key names off that index INSTEAD of
# guessing. An undocumented key is therefore a lie the agent is instructed to
# trust — `translate-commit` returned `evaluated` for four releases while its entry
# named it only inside the `counts` description string.
#
# Four hand-audits in a row missed a verb each time, so this reads the source
# instead. It is static on purpose: the runtime check below can only see branches
# its fixture reaches, which is exactly how `combine` shipped an undocumented
# `chapters` on its no-match branch while a runtime completeness test for `combine`
# sat green two functions away.

# Commands whose flow function is not the mechanical `-`/space -> `_` transform.
_SCHEMA_FUNC_ALIASES = {"split": "split_apply"}


def _literal_return_keys(fn) -> set[str]:
    """String keys of every ``return {...}`` literal in ``fn``'s OWN body.

    Nested ``def``/``lambda`` bodies are skipped: helper closures inside
    ``retranslate`` and ``combine`` return per-item dicts (``path``/``mtime``/
    ``bytes``) that are not the command's payload, and counting them would make
    this fail on correct code.
    """
    import ast

    keys: set[str] = set()
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys.update(
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
        stack.extend(ast.iter_child_nodes(node))
    return keys


def test_output_schemas_document_every_returned_key():
    """No verb may return a key its OUTPUT_SCHEMAS entry omits (bambi friction §2).

    Covers EVERY branch of the 23 commands whose flow function returns dict
    literals — including the error and no-match paths no fixture reaches. The
    remaining commands build their payload through a variable (`translate-prepare`,
    `align`, `status`, `translate-fanout`, `address-map precheck`) or through
    `_stream_result` (`chunk`, `cost`, `translate`, `epub`, `footnotes
    translate`/`apply`); their literal returns are still checked, but their
    variable-built keys need the hand-check EXTENDING.md asks for.
    """
    import ast

    tree = ast.parse(Path(flow.__file__).read_text(encoding="utf-8"))
    funcs = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    checked, undocumented = [], {}
    for command, schema in flow.OUTPUT_SCHEMAS.items():
        name = _SCHEMA_FUNC_ALIASES.get(
            command, command.replace("-", "_").replace(" ", "_"))
        fn = funcs.get(name)
        if fn is None:
            continue  # streaming/dispatched elsewhere — nothing static to read
        checked.append(command)
        missing = sorted(_literal_return_keys(fn) - set(schema))
        if missing:
            undocumented[command] = missing

    assert not undocumented, (
        "verbs return keys their OUTPUT_SCHEMAS entry never documents, so "
        f"_schema_keys under-reports them: {undocumented}"
    )
    # A rename that silently stops matching function names would otherwise turn
    # this into a vacuously green test.
    assert len(checked) >= 20, f"audit only reached {len(checked)} commands: {checked}"


def test_retranslate_and_combine_are_not_streaming_commands():
    """Both return plain dicts; the streaming branch would KeyError on exit_code."""
    import scripts.harness as harness

    assert "retranslate" not in harness._STREAMING_COMMANDS
    assert "combine" not in harness._STREAMING_COMMANDS


def test_cli_retranslate_preview_is_nonmutating(project: Path, monkeypatch):
    """The CLI preview path stamps ``_schema_path``, writes last_output.json, and changes nothing."""
    import scripts.harness as harness

    cp = _translated_chunk(project)
    before = cp.read_bytes()

    monkeypatch.setattr(sys, "argv",
                        ["harness.py", "retranslate", "--project", str(project)])
    harness.main()  # a dict command returns normally (no SystemExit)

    out = json.loads((project / ".harness" / "last_output.json").read_text(encoding="utf-8"))
    assert "_schema" not in out
    assert out["_schema_path"].endswith("last_output_schema.json")
    sidecar = json.loads(
        (project / ".harness" / "last_output_schema.json").read_text(encoding="utf-8")
    )
    assert sidecar == flow.OUTPUT_SCHEMAS["retranslate"]
    assert out["dry_run"] is True
    assert out["cleared"] == ["chapter_01_chunk_000"]
    assert cp.read_bytes() == before


# ── style-guide → glossary carry-forward ────────────────────────────────────
#
# A style guide states rules, not term→translation pairs (two sources of truth
# disagree). So when style-guide drafting surfaces a term needing a fixed
# translation, it hands it forward instead of defining it, and the glossary beat
# injects it as a candidate — frequency-ranked extraction can bury a
# rare-but-critical term.

def test_prepare_draft_reports_carryforward_path(project: Path):
    prep = flow.style_guide_prepare_questions(str(project))
    Path(prep["answers_path"]).write_text(json.dumps({}), encoding="utf-8")
    draft = flow.style_guide_prepare_draft(str(project))
    assert draft["carryforward_path"].endswith("glossary_carryforward.json")
    assert Path(draft["carryforward_path"]).parent.name == ".harness"


def test_glossary_prepare_injects_carryforward_terms(project: Path):
    save_style_guide(StyleGuide(content="Warm register."), project / "style.json")
    hdir = state.ensure_harness_dir(project)
    (hdir / "glossary_carryforward.json").write_text(json.dumps([
        {"term": "gobbler", "why": "pavo macho vs guajolote — dialect call", "type_guess": "other"},
        {"term": "the game", "why": "central motif; lock one phrasing", "type_guess": "concept"},
    ]), encoding="utf-8")

    prep = flow.glossary_prepare(str(project))
    assert prep["carryforward_count"] == 2
    prompt = Path(prep["prompt_path"]).read_text(encoding="utf-8")
    # Both the terms and the rationale reach the drafting prompt.
    assert "gobbler" in prompt and "the game" in prompt
    assert "dialect call" in prompt
    assert "central motif" in prompt


def test_carryforward_term_already_extracted_is_not_duplicated(project: Path):
    """A term extraction already found keeps its note but adds no second candidate."""
    save_style_guide(StyleGuide(content="Warm register."), project / "style.json")
    hdir = state.ensure_harness_dir(project)
    baseline = flow.glossary_prepare(str(project))["candidate_count"]

    (hdir / "glossary_carryforward.json").write_text(json.dumps([
        {"term": "Old Thomas", "why": "already extracted; note still applies"},
    ]), encoding="utf-8")
    prep = flow.glossary_prepare(str(project))

    assert prep["carryforward_count"] == 0          # no duplicate candidate
    assert prep["candidate_count"] == baseline
    assert "already extracted" in Path(prep["prompt_path"]).read_text(encoding="utf-8")


def test_glossary_prepare_survives_malformed_carryforward(project: Path):
    """A bad hand-off must not take down the glossary beat."""
    save_style_guide(StyleGuide(content="Warm register."), project / "style.json")
    hdir = state.ensure_harness_dir(project)
    (hdir / "glossary_carryforward.json").write_text("{not json", encoding="utf-8")

    prep = flow.glossary_prepare(str(project))
    assert prep["carryforward_count"] == 0
    assert Path(prep["prompt_path"]).exists()


# ── address map → style guide summary ───────────────────────────────────────

def test_prepare_draft_injects_address_summary(project: Path):
    from src.models import AddressMap
    from src.utils.file_io import save_address_map

    save_address_map(
        AddressMap(content="full prose the judge reads",
                   style_guide_summary="Children use usted to adults; adults use tú to children."),
        project / "address_map.json",
    )
    prep = flow.style_guide_prepare_questions(str(project))
    Path(prep["answers_path"]).write_text(json.dumps({}), encoding="utf-8")

    draft = flow.style_guide_prepare_draft(str(project))
    assert draft["address_summary_loaded"] is True
    prompt = Path(draft["prompt_path"]).read_text(encoding="utf-8")
    assert "Children use usted to adults" in prompt
    # The judge-facing prose is a different audience and must NOT leak in.
    assert "full prose the judge reads" not in prompt


def test_prepare_draft_without_address_map_falls_back(project: Path):
    prep = flow.style_guide_prepare_questions(str(project))
    Path(prep["answers_path"]).write_text(json.dumps({}), encoding="utf-8")
    draft = flow.style_guide_prepare_draft(str(project))
    assert draft["address_summary_loaded"] is False
    assert "no address map for this book" in Path(draft["prompt_path"]).read_text(encoding="utf-8")


def test_glossary_commit_flags_stale_address_map_names(project: Path):
    """The reconcile hand-off: the map was drafted before the glossary existed."""
    from src.models import AddressMap, AddressPair, AddressRule
    from src.utils.file_io import save_address_map

    save_address_map(
        AddressMap(
            content="Pollyanna addresses Aunt Polly with usted.",
            pairs=[AddressPair(a="Pollyanna", b="Aunt Polly", directions={
                "a_to_b": [AddressRule(form="usted", when="default")],
            })],
        ),
        project / "address_map.json",
    )
    state.ensure_harness_dir(project)
    (project / ".harness" / "glossary_draft.json").write_text(json.dumps([
        {"english": "Aunt Polly", "translation": "la tía Polly", "type": "character"},
    ]), encoding="utf-8")

    out = flow.glossary_commit(str(project))
    assert any("address_map.json still uses English cast names" in w for w in out["warnings"])
    assert any("la tía Polly" in w for w in out["warnings"])


def test_glossary_commit_surfaces_convention_reviews(project: Path):
    """REVIEW: flags reach the approval gate without blocking the commit."""
    state.ensure_harness_dir(project)
    (project / ".harness" / "glossary_draft.json").write_text(json.dumps([
        {"english": "Beldingsville", "translation": "Beldingsville", "type": "place",
         "alternatives": ["el pueblo de Beldingsville"]},
        {"english": "Aunt Polly", "translation": "tía Polly", "type": "character"},
    ]), encoding="utf-8")

    out = flow.glossary_commit(str(project))
    reviews = [w for w in out["warnings"] if w.startswith("REVIEW:")]
    assert len(reviews) == 2
    assert out["term_count"] == 2          # advisory: the glossary still committed
    assert (project / "glossary.json").exists()


# ── heading-outline split ───────────────────────────────────────────────────

def _outline_project(tmp_path: Path, headings, body_reps: int = 40) -> Path:
    """A project whose source.txt and headings.json agree, as ingest writes them."""
    proj = tmp_path / "book"
    proj.mkdir(exist_ok=True)
    body = "lorem ipsum dolor sit amet " * body_reps
    parts, outline = [], []
    for level, text in headings:
        parts += [text, body]
        outline.append({"level": level, "text": text})
    (proj / "source.txt").write_text("\n\n".join(parts), encoding="utf-8")
    (proj / "headings.json").write_text(
        json.dumps({"version": 1, "headings": outline}, ensure_ascii=False),
        encoding="utf-8")
    return proj


_MIXED_CASE_BOOK = [
    (1, "Among the Meadow People"),
    (2, "CONTENTS"),
    (2, "INTRODUCTION."),
    (2, "The BUTTERFLY That WENT CALLING"),
    (2, "THE ROBINS BUILD A NEST."),
    (2, "The Lazy Snail"),
    (2, "Mr GREEN FROG AND HIS VISITORS"),
    (2, "The Earthworm Half-Brothers"),
    (2, "The Crickets School"),
]


def test_split_preview_anchors_on_the_heading_outline(tmp_path: Path):
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)

    result = flow.split_preview(str(proj), pattern_type="auto")

    assert result["pattern_used"] == "headings"
    assert result["heading_outline"]["selected"] == "h2"
    assert result["heading_outline"]["unlocated"] == []
    # Six stories, every one a chapter — including the four whose Title-Case
    # headings `allcaps_heading` cannot see. CONTENTS is stripped, INTRODUCTION
    # is auto-tagged front matter, and the h1 book title is not at this level.
    assert [(s["kind"], s["name"]) for s in result["sections"]] == [
        ("front_matter", "Introduction"),
        ("chapter", "The BUTTERFLY That WENT CALLING"),
        ("chapter", "THE ROBINS BUILD A NEST."),
        ("chapter", "The Lazy Snail"),
        ("chapter", "Mr GREEN FROG AND HIS VISITORS"),
        ("chapter", "The Earthworm Half-Brothers"),
        ("chapter", "The Crickets School"),
    ]
    assert result["counts"] == {"front_matter": 1, "chapter": 6, "back_matter": 0}
    assert [d["label"] for d in result["dropped"]] == ["Contents"]
    assert result["files_written"] is False
    assert not (proj / "chapters").exists()


def test_split_preview_reports_the_level_table(tmp_path: Path):
    """A wrong level must be fixable from the output, without a custom regex."""
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)
    levels = flow.split_preview(str(proj), pattern_type="auto")["heading_outline"]["levels"]
    assert set(levels) >= {"h2"}
    for stats in levels.values():
        assert set(stats) == {"n", "median_chars", "tiny", "skew"}


def test_heading_level_override_is_honored(tmp_path: Path):
    # h2 is strictly denser, so the selector picks it on its own. Forcing h3
    # must change both the split and what the report says it used — a report
    # naming a level the split didn't use makes the level table useless.
    headings = []
    for i in range(1, 9):
        headings.append((2, f"Scene {i}"))
        if i % 2:
            headings.append((3, f"Chapter {i}"))
    proj = _outline_project(tmp_path, headings)

    auto = flow.split_preview(str(proj), pattern_type="auto")
    assert auto["heading_outline"]["selected"] == "h2"
    assert [s["name"] for s in auto["sections"]] == [f"Scene {i}" for i in range(1, 9)]

    forced = flow.split_preview(str(proj), pattern_type="headings", heading_level="h3")
    assert forced["heading_outline"]["selected"] == "h3"
    assert "explicitly requested" in forced["heading_outline"]["reason"]
    assert [s["name"] for s in forced["sections"]] == [
        f"Chapter {i}" for i in (1, 3, 5, 7)]
    # The level table still lists every candidate, so the choice stays reversible.
    assert set(forced["heading_outline"]["levels"]) >= {"h2", "h3"}


def test_split_apply_writes_the_outline_split(tmp_path: Path):
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)

    result = flow.split_apply(str(proj), pattern_type="auto")

    assert result["pattern_used"] == "headings"
    assert result["files_written"] is True
    assert result["chapter_count"] == 7  # 6 stories + the introduction
    assert len(list((proj / "chapters").glob("chapter_*.txt"))) == 7
    second = (proj / "chapters" / "chapter_02.txt").read_text(encoding="utf-8")
    assert second.startswith("The BUTTERFLY That WENT CALLING")


def test_forcing_a_regex_pattern_marks_the_outline_unapplied(tmp_path: Path):
    """The report is still computed on a book with a sidecar; it must not read
    as though the outline is what the split ran on."""
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)

    result = flow.split_preview(str(proj), pattern_type="allcaps_heading")

    assert result["pattern_used"] == "allcaps_heading"
    assert result["heading_outline"]["applied"] is False
    assert result["heading_outline"]["levels"]  # the table is still there to compare
    assert result["ledger"]["chapter_level"] is None
    assert result["ledger"]["chapter_level_headings"] is None

    applied = flow.split_preview(str(proj), pattern_type="auto")
    assert applied["heading_outline"]["applied"] is True
    assert applied["ledger"]["chapter_level"] == "h2"


def test_ledger_accounts_for_every_heading(tmp_path: Path):
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)
    ledger = flow.split_preview(str(proj), pattern_type="auto")["ledger"]
    assert ledger["chapter_level"] == "h2"
    assert ledger["outline_headings"] == len(_MIXED_CASE_BOOK)
    assert ledger["chapter_level_headings"] == 8  # every h2, boilerplate included
    assert ledger["sections"] + ledger["dropped"] == 8
    assert ledger["unlocated"] == 0


def test_unlocated_heading_is_reported_not_silent(tmp_path: Path):
    """source.txt hand-edited after ingest: the sidecar no longer matches."""
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)
    outline = json.loads((proj / "headings.json").read_text(encoding="utf-8"))
    outline["headings"].append({"level": 2, "text": "A Story That Was Cut"})
    (proj / "headings.json").write_text(json.dumps(outline), encoding="utf-8")

    result = flow.split_preview(str(proj), pattern_type="auto")

    assert result["heading_outline"]["unlocated"] == ["A Story That Was Cut"]
    assert result["ledger"]["unlocated"] == 1
    assert any("not found in source.txt" in w for w in result["warnings"])


def test_no_sidecar_leaves_the_regex_path_untouched(tmp_path: Path):
    """The no-regression guarantee: a project without headings.json splits
    exactly as it did before the outline existed."""
    proj = tmp_path / "book"
    proj.mkdir()
    (proj / "source.txt").write_text(_front_back_source(), encoding="utf-8")
    assert not (proj / "headings.json").exists()

    result = flow.split_preview(
        str(proj), pattern_type="auto",
        front_matter_titles=["To the Teacher"], back_matter_titles=["Afterword"])

    assert result["heading_outline"] is None
    assert result["pattern_used"] != "headings"
    assert result["ledger"]["outline_headings"] is None
    assert [s["kind"] for s in result["sections"]] == [
        "front_matter", "chapter", "chapter", "back_matter"]


def test_headings_requested_without_a_sidecar_fails_loudly(tmp_path: Path):
    proj = tmp_path / "book"
    proj.mkdir()
    (proj / "source.txt").write_text(_front_back_source(), encoding="utf-8")
    with pytest.raises(ValueError, match="headings.json"):
        flow.split_preview(str(proj), pattern_type="headings")


# A book the selector declines (3 h2s is under _HEADING_MIN_SECTIONS) whose
# Title-Case headings no regex pattern can find either. Naming a level by hand
# is the only way to split it, which is what --heading-level is for.
_DECLINED_BOOK = [(2, "The Wolf at the Door"), (2, "The Long Winter"),
                  (2, "A Letter Home")]


def test_auto_plus_heading_level_uses_and_reports_the_outline(tmp_path: Path):
    """``pattern_used`` comes from a second, independent resolve; it has to see
    ``heading_level`` too, or it names a regex pattern for an outline split."""
    proj = _outline_project(tmp_path, _DECLINED_BOOK)

    # Without the flag the selector declines and the regex fallback finds
    # nothing -- the state the flag exists to rescue.
    with pytest.raises(ValueError, match="No chapters detected"):
        flow.split_preview(str(proj), pattern_type="auto")

    result = flow.split_preview(str(proj), pattern_type="auto", heading_level="h2")

    assert result["pattern_used"] == "headings"
    assert result["heading_outline"]["applied"] is True
    assert result["heading_outline"]["selected"] == "h2"
    assert result["ledger"]["chapter_level"] == "h2"
    assert [s["name"] for s in result["sections"]] == [t for _, t in _DECLINED_BOOK]


def test_heading_level_without_a_sidecar_warns_instead_of_vanishing(tmp_path: Path):
    proj = tmp_path / "book"
    proj.mkdir()
    (proj / "source.txt").write_text(_front_back_source(), encoding="utf-8")

    result = flow.split_preview(str(proj), pattern_type="auto", heading_level="h2")

    assert result["heading_outline"] is None
    assert result["pattern_used"] != "headings"
    assert any("--heading-level h2 had no effect" in w for w in result["warnings"])


def _tear_sidecar(proj: Path) -> None:
    """Simulate a write that died partway, leaving truncated JSON."""
    raw = (proj / "headings.json").read_text(encoding="utf-8")
    (proj / "headings.json").write_text(raw[:len(raw) // 2], encoding="utf-8")


def test_broken_sidecar_fails_closed_when_headings_asked_for_by_name(tmp_path: Path):
    """``--chapter-pattern headings`` demanded the outline path, so silently
    regexing would answer a different question than the one asked."""
    from src.harness_guard import HarnessValidationError

    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)
    _tear_sidecar(proj)
    with pytest.raises(HarnessValidationError, match="headings.json exists but"):
        flow.split_preview(str(proj), pattern_type="headings")


def test_broken_sidecar_warns_but_still_splits_on_auto(tmp_path: Path):
    """Under ``auto`` the regex fallback is legitimate — it just must not be
    indistinguishable from a project that predates the sidecar."""
    proj = _outline_project(tmp_path, _MIXED_CASE_BOOK)
    _tear_sidecar(proj)

    result = flow.split_preview(str(proj), pattern_type="auto")

    assert result["pattern_used"] != "headings"
    assert result["heading_outline"] is None
    assert any("headings.json exists but could not be used" in w
               for w in result["warnings"])


# ── --custom-regex-file ────────────────────────────────────────────────────

def _regex_args(*, custom_regex=None, custom_regex_file=None):
    import argparse
    return argparse.Namespace(
        custom_regex=custom_regex, custom_regex_file=custom_regex_file)


def test_custom_regex_file_reads_the_pattern(tmp_path: Path):
    from scripts.harness import _resolve_custom_regex

    path = tmp_path / "pat.txt"
    path.write_text(r"^CHAPTER [IVX]+$", encoding="utf-8")
    assert _resolve_custom_regex(_regex_args(custom_regex_file=str(path))) == (
        r"^CHAPTER [IVX]+$")


def test_custom_regex_file_and_flag_are_mutually_exclusive(tmp_path: Path):
    from scripts.harness import _resolve_custom_regex

    path = tmp_path / "pat.txt"
    path.write_text("CHAPTER", encoding="utf-8")
    with pytest.raises(HarnessValidationError, match="mutually exclusive"):
        _resolve_custom_regex(_regex_args(
            custom_regex="CHAPTER", custom_regex_file=str(path)))


def test_custom_regex_file_missing_is_named(tmp_path: Path):
    from scripts.harness import _resolve_custom_regex

    missing = tmp_path / "nope.txt"
    with pytest.raises(HarnessValidationError, match="not found"):
        _resolve_custom_regex(_regex_args(custom_regex_file=str(missing)))


def test_custom_regex_file_empty_is_rejected(tmp_path: Path):
    from scripts.harness import _resolve_custom_regex

    path = tmp_path / "empty.txt"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(HarnessValidationError, match="is empty"):
        _resolve_custom_regex(_regex_args(custom_regex_file=str(path)))
