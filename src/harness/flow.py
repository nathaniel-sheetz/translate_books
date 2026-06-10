"""Harness orchestration: one function per translate-harness CLI command.

Each function composes the existing ``src/`` / ``scripts/`` primitives for one beat
of the pipeline. There is **no new business logic** here (same DRY stance as
``src/harness_guard.py``): prompts are built by ``style_guide_wizard`` /
``glossary_bootstrap``, drafts are parsed/validated by those modules plus
``harness_guard``, and the paid/deterministic stages are the existing
``scripts/translate_book.py`` / ``score_difficulty`` / ``build_epub`` paths.

Two shapes of beat:

  * **In-process** (return a dict the CLI prints as JSON): the style-guide and
    glossary beats, ``setup``, and ``difficulty`` — the parts that had *no*
    non-interactive entry point before (the gap this layer fills).
  * **Subprocess** (return an int exit code): ``chunk`` / ``cost`` / ``translate``
    / ``epub`` wrap the existing CLIs so the cost-gate semantics stay in exactly
    one place (``translate_book.py``). ``chunk``/``cost`` always pass
    ``--cost-only`` and physically cannot spend; ``translate`` fails closed
    without ``--yes``.

The agent writes the four *draft* artifacts itself (answers, follow-ups, style
guide, glossary proposals); the harness writes the *prompts* and the *final*
artifacts (``style.json`` / ``glossary.json``).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from src.harness import state


# ── helpers ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _quiet_stdout():
    """Route a helper's chatty ``print``s to stderr so stdout stays clean JSON."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _run_script(cmd: list[str]) -> int:
    """Run a repo script as a subprocess from the repo root, inheriting stdio.

    The agent sees the script's real output (cost estimate, progress) directly,
    and the cost-gate logic stays in the wrapped CLI rather than being re-derived.
    """
    sys.stdout.flush()
    return subprocess.run([sys.executable, *cmd], cwd=str(state.REPO_ROOT)).returncode


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── setup ──────────────────────────────────────────────────────────────────

def setup(
    project: str,
    *,
    url: str = "",
    chapter_pattern: str = "roman",
    custom_regex: str | None = None,
    target_language: str | None = None,
    locale: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    title: str | None = None,
    author: str | None = None,
    language_code: str | None = None,
) -> dict:
    """Create the project, persist config, run ingest + split (NOT chunk).

    Chunking is deferred to ``chunk`` so it can use the glossary-informed
    difficulty score. Wipes any prior ``.harness/`` working state for a clean run.
    """
    project_dir = state.resolve_project_dir(project, must_exist=False)
    project_dir.mkdir(parents=True, exist_ok=True)

    # Preserve prior config, override only the keys explicitly provided this run.
    cfg = state.load_config(project_dir)
    for key, value in {
        "target_language": target_language,
        "locale": locale,
        "provider": provider,
        "model": model,
        "title": title,
        "author": author,
        "language_code": language_code,
    }.items():
        if value is not None:
            cfg[key] = value

    state.ensure_harness_dir(project_dir, clean=True)  # fresh drafts/prompts
    state.save_config(project_dir, cfg)

    sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import (
        stage_ingest,
        stage_split,
        load_pipeline_state,
        save_pipeline_state,
    )

    args = SimpleNamespace(
        url=url or "",
        chapter_pattern=chapter_pattern,
        custom_regex=custom_regex,
        min_chapter_size=100,
    )
    with _quiet_stdout():
        pstate = load_pipeline_state(project_dir)
        pstate = stage_ingest(args, project_dir, pstate)
        save_pipeline_state(project_dir, pstate)
        pstate = stage_split(args, project_dir, pstate)
        save_pipeline_state(project_dir, pstate)

    chapters = sorted((project_dir / "chapters").glob("chapter_*.txt"))
    return {
        "project_dir": str(project_dir),
        "config": state.load_config(project_dir),
        "chapters": [c.stem for c in chapters],
        "chapter_count": len(chapters),
        "source_words": pstate.get("source_words"),
        "chunks_dir_exists": (project_dir / "chunks").exists(),  # expected False
        "next": "style-guide prepare-questions",
    }


# ── style guide beat ───────────────────────────────────────────────────────

def style_guide_prepare_questions(project: str) -> dict:
    """Gather fixed + feature-detected questions; print them for the agent to ask."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.ensure_harness_dir(project_dir)

    sys.path.insert(0, str(state.REPO_ROOT))
    from src.style_guide_wizard import get_active_questions, load_source_sample

    source = load_source_sample(project_dir)
    with _quiet_stdout():
        fixed, conditional, manifest = get_active_questions(project_dir)

    present = [name for name, r in manifest.features.items() if r.present]
    for q in conditional:  # attach the detected hint, as the old heredoc did
        feature = q.get("requires", {}).get("feature")
        if feature and feature in manifest.features and manifest.features[feature].evidence:
            q["_detected_hint"] = manifest.features[feature].evidence[0]
    allq = list(fixed) + list(conditional)

    (hdir / "style_source.txt").write_text(source, encoding="utf-8")
    (hdir / "style_fixed.json").write_text(json.dumps(fixed), encoding="utf-8")
    (hdir / "style_questions.json").write_text(json.dumps(allq), encoding="utf-8")

    return {
        "detected_features": present or [],
        "questions": [
            {
                "id": q["id"],
                "question": q["question"],
                "options": [o["label"] for o in q.get("options", [])],
                "hint": q.get("_detected_hint", ""),
            }
            for q in allq
        ],
        "answers_path": str(hdir / "style_answers.json"),
        "instructions": (
            "STOP: ask the user every question and WAIT for their answers — do not answer "
            "for them or pick defaults. Then Write {question_id: option_index_or_custom_string} "
            "to answers_path, then run `style-guide prepare-followups`."
        ),
    }


def style_guide_prepare_followups(project: str, *, answers: str | None = None) -> dict:
    """Build the follow-up-question prompt (you are the LLM that drafts them)."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    from src.style_guide_wizard import build_question_prompt

    source = _read(hdir / "style_source.txt")
    fixed = json.loads(_read(hdir / "style_fixed.json"))
    answers_path = Path(answers) if answers else hdir / "style_answers.json"
    ans = json.loads(_read(answers_path))

    prompt = build_question_prompt(source, cfg["target_language"], cfg["locale"], fixed, ans)
    prompt_path = hdir / "style_followups_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return {
        "prompt_path": str(prompt_path),
        "draft_path": str(hdir / "style_followups.json"),
        "instructions": (
            "Read prompt_path, draft the follow-up questions as a JSON array to draft_path, "
            "then run `style-guide commit-followups`."
        ),
    }


def style_guide_commit_followups(project: str, *, draft: str | None = None) -> dict:
    """Parse + merge agent-drafted follow-up questions into the question set."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.style_guide_wizard import parse_llm_questions

    draft_path = Path(draft) if draft else hdir / "style_followups.json"
    extra = parse_llm_questions(_read(draft_path))  # raises ValueError -> re-draft
    allq = json.loads(_read(hdir / "style_questions.json")) + extra
    (hdir / "style_questions.json").write_text(json.dumps(allq), encoding="utf-8")
    return {
        "new_questions": [
            {
                "id": q["id"],
                "question": q["question"],
                "options": [o["label"] for o in q.get("options", [])],
            }
            for q in extra
        ],
        "answers_path": str(hdir / "style_answers.json"),
        "instructions": (
            "STOP: ask the user these follow-ups and WAIT for their answers — do not answer "
            "for them or pick defaults. Then rewrite answers_path with the FULL answer set "
            "(prior answers + these), then run `style-guide prepare-draft`."
        ),
    }


def style_guide_prepare_draft(project: str, *, answers: str | None = None) -> dict:
    """Build the style-guide prompt (you are the LLM that drafts the prose)."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    from src.style_guide_wizard import build_style_guide_prompt

    allq = json.loads(_read(hdir / "style_questions.json"))
    answers_path = Path(answers) if answers else hdir / "style_answers.json"
    ans = json.loads(_read(answers_path))
    source = _read(hdir / "style_source.txt")

    prompt = build_style_guide_prompt(allq, ans, source, cfg["target_language"], cfg["locale"])
    prompt_path = hdir / "style_guide_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return {
        "prompt_path": str(prompt_path),
        "draft_path": str(hdir / "style_guide_draft.txt"),
        "instructions": (
            "Read prompt_path, draft the style-guide prose to draft_path, refine it with the "
            "user, then run `style-guide commit`."
        ),
    }


def style_guide_commit(project: str, *, draft: str | None = None) -> dict:
    """Parse, save, and validate the agent-drafted style guide -> style.json."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.style_guide_wizard import parse_style_guide_response, save_style_guide_json
    from src.harness_guard import validate_style_guide_file

    draft_path = Path(draft) if draft else hdir / "style_guide_draft.txt"
    content = parse_style_guide_response(_read(draft_path))
    out = project_dir / "style.json"
    save_style_guide_json(content, out)
    validate_style_guide_file(out)  # raises HarnessValidationError -> re-draft
    return {"style_path": str(out), "chars": len(content)}


# ── glossary beat ──────────────────────────────────────────────────────────

def glossary_prepare(project: str, *, max_candidates: int = 200) -> dict:
    """Extract candidates + build the proposal prompt (feeding in the style guide)."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.extract_glossary_candidates import extract_candidates
    from src.glossary_bootstrap import build_glossary_prompt
    from src.style_guide_wizard import load_source_sample

    source = _read(project_dir / "source.txt")
    with _quiet_stdout():
        report = extract_candidates(source, verbose=False)
    candidates = [c.model_dump() for c in report.candidates[:max_candidates]]
    sample = load_source_sample(project_dir)
    style_path = project_dir / "style.json"
    style_guide = _read(style_path) if style_path.exists() else ""

    prompt = build_glossary_prompt(candidates, sample, style_guide, cfg["target_language"])
    prompt_path = hdir / "glossary_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return {
        "prompt_path": str(prompt_path),
        "candidate_count": len(candidates),
        "style_guide_loaded": bool(style_guide),
        "draft_path": str(hdir / "glossary_draft.json"),
        "instructions": (
            "Read prompt_path, draft proposals as a JSON array of "
            "{english, translation, type, context} to draft_path (tracking any uncertain "
            "renderings to surface at the approval gate), then run `glossary commit`."
        ),
    }


def glossary_commit(project: str, *, draft: str | None = None) -> dict:
    """Guard, build, save, and validate the agent-drafted glossary -> glossary.json."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.harness_guard import guard_glossary_proposals, validate_glossary_file
    from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
    from src.utils.file_io import save_glossary

    draft_path = Path(draft) if draft else hdir / "glossary_draft.json"
    proposals = json.loads(_read(draft_path))
    guard_glossary_proposals(proposals)  # raises HarnessValidationError -> re-draft
    glossary = proposals_to_glossary(glossary_terms_from_proposals(proposals))
    out = project_dir / "glossary.json"
    save_glossary(glossary, out)
    validate_glossary_file(out)  # belt-and-suspenders
    return {
        "glossary_path": str(out),
        "term_count": len(glossary.terms),
        "terms": [
            {"english": t.english, "translation": t.spanish, "type": t.type, "context": t.context}
            for t in glossary.terms
        ],
    }


# ── difficulty (in-process; returns structured suggestion) ─────────────────

def difficulty(project: str) -> dict:
    """Score difficulty (glossary now exists) and report the suggested chunk size."""
    project_dir = state.resolve_project_dir(project)

    from src.difficulty_scorer import WORDFREQ_AVAILABLE, score_book

    with _quiet_stdout():
        manifest = score_book(project_dir, force=True)  # --force: reflect the new glossary
    b = manifest.book
    return {
        "book_difficulty": round(b.difficulty, 3),
        "length_score": round(b.length_score, 3),
        "rarity_score": round(b.rarity_score, 3),
        "suggested_target_size": b.suggested_target_size,
        "wordfreq_available": WORDFREQ_AVAILABLE,
        "chapters": [
            {
                "chapter_id": cd.chapter_id,
                "difficulty": round(cd.metrics.difficulty, 3),
                "suggested_target_size": cd.metrics.suggested_target_size,
            }
            for cd in manifest.chapters
        ],
        "next": "chunk --size <N> (default to suggested_target_size unless the user overrides)",
    }


# ── chunk / cost / translate / epub (subprocess wrappers) ──────────────────

def chunk(project: str, *, size: int) -> int:
    """Chunk at ``size`` and print the cost estimate, halting before any spend.

    Wraps ``translate_book.py --start-stage chunk --cost-only`` so the run chunks
    once and then stops at the estimate (``--cost-only`` exits before a single
    chunk is translated — it physically cannot spend).
    """
    project_dir = state.resolve_project_dir(project)
    return _run_script([
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "chunk",
        "--cost-only",
        "--chunk-size", str(int(size)),
    ])


def cost(project: str) -> int:
    """Re-print the translation cost estimate WITHOUT spending (pure estimator)."""
    project_dir = state.resolve_project_dir(project)
    return _run_script([
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "translate",
        "--cost-only",
    ])


def translate(project: str, *, yes: bool, model: str | None = None, provider: str | None = None) -> int:
    """The one paid step. Fails closed without ``--yes`` (cost gate, defense-in-depth)."""
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    if not yes:
        print(
            "Refusing to translate without --yes. The cost estimate must be approved by the "
            "user in a SEPARATE turn first; only then re-run `translate` with --yes.",
            file=sys.stderr,
        )
        return 2
    return _run_script([
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "translate",
        "--yes",
        "--provider", provider or cfg["provider"],
        "--model", model or cfg["model"],
    ])


def epub(project: str, *, title: str | None = None, author: str | None = None, language: str | None = None) -> int:
    """Build the EPUB from translated chunks (title/author/language default from config)."""
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    title = title or cfg.get("title") or ""
    author = author or cfg.get("author") or ""
    language = language or cfg.get("language_code") or "es"
    if not title or not author:
        print(
            "epub needs a title and author. Pass --title/--author, or set them once via "
            "`setup --title ... --author ...`.",
            file=sys.stderr,
        )
        return 2
    return _run_script([
        "scripts/build_epub.py",
        str(project_dir),
        "--title", title,
        "--author", author,
        "--language", language,
    ])
