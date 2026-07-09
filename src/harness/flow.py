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
  * **Subprocess** (stream live output, return a dict carrying ``exit_code``):
    ``chunk`` / ``cost`` / ``translate`` / ``epub`` wrap the existing CLIs so the
    cost-gate semantics stay in exactly one place (``translate_book.py``).
    ``chunk``/``cost`` always pass ``--cost-only`` and physically cannot spend;
    ``translate`` fails closed without ``--yes`` (returning a bare int there). The
    wrapped script prints a ``HARNESS_RESULT:`` sentinel that ``_run_script`` captures
    so the command mirrors a FRESH structured result to ``last_output.json``
    (friction-log #18) rather than leaving the previous command's behind.

The agent writes the four *draft* artifacts itself (answers, follow-ups, style
guide, glossary proposals); the harness writes the *prompts* and the *final*
artifacts (``style.json`` / ``glossary.json``).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from src.harness import state

# Spawn modes for the subagent backend (Step 4B). The agent asks the user to pick
# one and it is persisted to the project config:
#   sequential — one chunk at a time, document order (max continuity).
#   chapter    — DEFAULT: a window of N chapters runs in parallel, each completed
#                wave-by-wave on chunk position before the next window starts.
#   all        — every chunk in bounded batches, no cross-chunk ordering (fastest).
_PARALLELISM_MODES = ("sequential", "chapter", "all")


def _spawn_plan_from_cfg(cfg: dict) -> tuple[dict, dict]:
    """Build ``spawn_plan`` from project config; enforce ``window <= batch_size``.

    Returns ``(spawn_plan, patches)`` where ``patches`` holds any config keys that
    were clamped (caller may persist them).
    """
    window = int(cfg.get("parallel_window") or 3)
    batch_size = int(cfg.get("batch_size") or 3)
    patches: dict = {}
    if window > batch_size:
        window = batch_size
        patches["parallel_window"] = window
    return {
        "parallelism": cfg.get("parallelism") or "chapter",
        "window": window,
        "batch_size": batch_size,
    }, patches


def _read_draft_text(path: Path) -> str | None:
    """Read a ``.draft.txt`` file; return ``None`` when missing or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

# Claude Code worker aliases whose extended thinking can be toggled ON by a
# "think hard" keyword in the spawn prompt. ``fable`` is deliberately absent: it
# is always-on (nothing to toggle), so a worker on it is treated as not
# thinking-capable for the opt-in gate (the analog of the GUI hiding the box).
_THINKING_WORKER_ALIASES = frozenset({"sonnet", "opus", "haiku"})


def _worker_supports_thinking(worker_model: str) -> bool:
    """True if a subagent worker on *worker_model* can be nudged into extended thinking.

    Workers are pinned to a Claude Code **alias** (``sonnet``/``opus``/``haiku``/
    ``fable``), not a full model id, so ``api_translator.model_supports_thinking``
    (which matches ``claude-sonnet-5`` etc.) can't be applied to it directly. The
    keyword-triggerable aliases toggle on via a "think hard" spawn keyword; ``fable``
    is always-on (nothing to toggle) → ``False``. A full model id (non-alias) falls
    back to the API-path support check.
    """
    if not worker_model:
        return False
    alias = worker_model.strip().lower()
    if alias in _THINKING_WORKER_ALIASES:
        return True
    if alias == "fable":
        return False
    from src.api_translator import model_supports_thinking
    return model_supports_thinking(worker_model)


# ── helpers ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _quiet_stdout():
    """Route a helper's chatty ``print``s to stderr so stdout stays clean JSON."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _run_script(cmd: list[str]) -> tuple[int, dict | None]:
    """Run a repo script as a subprocess from the repo root, streaming its stdout.

    The agent sees the script's real output (cost estimate, progress) live as it
    happens, and the cost-gate logic stays in the wrapped CLI rather than being
    re-derived. While streaming, capture the single ``HARNESS_RESULT:`` sentinel
    line the wrapper prints (``state.emit_harness_result``) and return it parsed,
    so the streaming command can mirror a FRESH structured result to
    ``last_output.json`` instead of leaving the previous command's behind
    (friction-log #18). The sentinel line is kept out of the human stream.

    Force UTF-8 in the child so accented output survives a cp1252 Windows console
    (friction-log #4); reconfiguring the parent's stdout does not reach the child.
    Returns ``(returncode, summary | None)``.
    """
    sys.stdout.flush()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, *cmd],
        cwd=str(state.REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=None,  # inherit: errors/tracebacks still surface live
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    summary: dict | None = None
    if proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith(state.HARNESS_RESULT_PREFIX):
                payload = line[len(state.HARNESS_RESULT_PREFIX):].strip()
                try:
                    summary = json.loads(payload)
                except json.JSONDecodeError:
                    summary = None  # malformed sentinel -> fall back to a minimal result
                continue  # machine-only line: never echo to the human stream
            print(line, end="")
    rc = proc.wait()
    return rc, summary


def _stream_result(command: str, rc: int, summary: dict | None) -> dict:
    """Shape a streaming command's return: a fresh dict carrying the exit code.

    Always returns a dict (never a bare int) so ``main()`` writes a fresh
    ``last_output.json`` for this command — closing the stale-artifact trap
    (friction-log #18) even when the wrapped script emitted no sentinel. ``main()``
    propagates ``exit_code`` as the process exit status.
    """
    result = dict(summary) if summary else {}
    # Harness-authoritative keys win: a wrapped script's sentinel must never
    # overwrite the command name or the real process exit code we recorded.
    result["command"] = command
    result["exit_code"] = rc
    return result


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── setup ──────────────────────────────────────────────────────────────────

def setup(
    project: str | None = None,
    *,
    url: str = "",
    chapter_pattern: str = "auto",
    custom_regex: str | None = None,
    target_language: str | None = None,
    locale: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    title: str | None = None,
    author: str | None = None,
    language_code: str | None = None,
    always_include_dialogue: bool | None = None,
    front_matter_titles: list[str] | None = None,
    back_matter_titles: list[str] | None = None,
    min_chapter_size: int | None = None,
    auto_strip_boilerplate: bool = True,
) -> dict:
    """Create the project, persist config, run ingest + split (NOT chunk).

    Chunking is deferred to ``chunk`` so it can use the glossary-informed
    difficulty score. Wipes any prior ``.harness/`` working state for a clean run.
    """
    # Name the project folder. An explicit --project is honored verbatim (and may
    # reuse an existing dir — the re-run-on-the-same-project path). Otherwise the
    # folder is named from the book title, suffixing on collision so a second copy
    # of the same book lands beside the first instead of clobbering it (#22).
    if project:
        project_dir = state.resolve_project_dir(project, must_exist=False)
    elif title:
        project_dir = state.available_project_dir(state.slugify(title))
    else:
        raise ValueError(
            "provide --title (the folder is named from it) or --project <slug>"
        )
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
        "always_include_dialogue": always_include_dialogue,
    }.items():
        if value is not None:
            cfg[key] = value

    state.ensure_harness_dir(project_dir, clean=True)  # fresh drafts/prompts
    cfg["run_id"] = state.new_run_id(project_dir)  # a clean run starts a new run id
    state.save_config(project_dir, cfg)

    if str(state.REPO_ROOT) not in sys.path:
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
        min_chapter_size=min_chapter_size if min_chapter_size is not None else 100,
        front_matter_titles=front_matter_titles or None,
        back_matter_titles=back_matter_titles or None,
        auto_strip_boilerplate=auto_strip_boilerplate,
    )
    with _quiet_stdout():
        pstate = load_pipeline_state(project_dir)
        pstate = stage_ingest(args, project_dir, pstate)
        save_pipeline_state(project_dir, pstate)
        pstate = stage_split(args, project_dir, pstate)
        save_pipeline_state(project_dir, pstate)

    chapters = sorted((project_dir / "chapters").glob("chapter_*.txt"))
    # Heading-derived hints. On the URL path ingest fills suggested_pattern /
    # chapter_report from the HTML; on the local source.txt path we derive them
    # here (parity) so setup can flag a wrong pattern or an under-split instead
    # of returning nulls and silently carrying a 1-chapter book to EPUB (#1/#2).
    hints = _pattern_hints(
        project_dir,
        requested=args.chapter_pattern,
        custom_regex=args.custom_regex,
        min_chapter_size=args.min_chapter_size,
        front_matter_titles=args.front_matter_titles,
        back_matter_titles=args.back_matter_titles,
        auto_strip_boilerplate=args.auto_strip_boilerplate,
    )
    return {
        "project_dir": str(project_dir),
        "config": state.load_config(project_dir),
        "chapters": [c.stem for c in chapters],
        "chapter_count": len(chapters),
        "pattern_used": hints["pattern_used"],  # the pattern actually split on
        "dropped": pstate.get("dropped", []),  # boilerplate stripped at split
        "source_words": pstate.get("source_words"),
        "suggested_pattern": pstate.get("suggested_pattern") or hints["detected"],
        "chapter_report": pstate.get("chapter_report") or _local_chapter_report(hints["sections"]),
        "warnings": hints["warnings"],  # e.g. "1 chapter for an 87KB source"
        "chunks_dir_exists": (project_dir / "chunks").exists(),  # expected False
        "next": "style-guide prepare-questions",
    }


# ── split (review beat) ─────────────────────────────────────────────────────

def _read_source(project_dir: Path) -> str:
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        raise FileNotFoundError(f"source.txt not found in {project_dir}")
    return source_path.read_text(encoding="utf-8")


def _detect_sections(
    book_text: str,
    *,
    pattern_type: str,
    custom_regex: str | None,
    min_chapter_size: int | None,
    front_matter_titles: list[str] | None,
    back_matter_titles: list[str] | None,
    auto_detect_front_matter: bool,
    auto_detect_back_matter: bool,
    auto_strip_boilerplate: bool = True,
    collect_dropped: list | None = None,
):
    """Run the shared splitter with the harness's defaults filled in."""
    from src.book_splitter import split_book_into_chapters

    return split_book_into_chapters(
        book_text=book_text,
        pattern_type=pattern_type or "roman",
        custom_regex=custom_regex,
        min_chapter_size=min_chapter_size if min_chapter_size is not None else 100,
        front_matter_titles=front_matter_titles or None,
        back_matter_titles=back_matter_titles or None,
        auto_detect_front_matter=auto_detect_front_matter,
        auto_detect_back_matter=auto_detect_back_matter,
        auto_strip_boilerplate=auto_strip_boilerplate,
        collect_dropped=collect_dropped,
    )


def _section_display_name(ch) -> str:
    """Mirror the GUI's label logic so chapters and matter read sensibly."""
    if ch.kind == "chapter":
        return ch.chapter_title or f"Chapter {ch.number or ch.position_index}"
    return ch.label or ch.chapter_title or ch.kind


def _kind_counts(chapters) -> dict:
    counts = {"front_matter": 0, "chapter": 0, "back_matter": 0}
    for ch in chapters:
        counts[ch.kind] = counts.get(ch.kind, 0) + 1
    return counts


# Keep in step with scripts/ingest_gutenberg.py's WORDS_PER_CHUNK, which drives
# the URL-path chapter_report so the two paths report comparable chunk counts.
_WORDS_PER_CHUNK = 2000


def _local_chapter_report(chapters) -> list[dict]:
    """Per-chapter report derived from detected sections — the local-source
    analog of ``ingest_gutenberg.build_chapter_report`` (which needs HTML
    heading offsets we don't have off ``source.txt``). Same shape:
    ``{number, heading, words, chunks}``, chapters only."""
    report = []
    for ch in chapters:
        if ch.kind != "chapter":
            continue
        words = len(ch.content.split())
        report.append({
            "number": ch.number,
            "heading": _section_display_name(ch),
            "words": words,
            "chunks": max(1, round(words / _WORDS_PER_CHUNK)),
        })
    return report


def _pattern_hints(
    project_dir: Path,
    *,
    requested: str | None,
    custom_regex: str | None,
    min_chapter_size: int | None,
    front_matter_titles: list[str] | None,
    back_matter_titles: list[str] | None,
    auto_detect_front_matter: bool = True,
    auto_detect_back_matter: bool = True,
    auto_strip_boilerplate: bool = True,
) -> dict:
    """Detect the best-fit pattern from ``source.txt`` and re-derive the
    committed split as objects, so any split beat can report ``pattern_used`` /
    ``suggested_pattern`` and run the sanity check on the local path — parity
    with the URL path's HTML-derived hints. Returns
    ``{detected, pattern_used, sections, warnings}``."""
    from src import book_splitter

    source_text = _read_source(project_dir)
    detected = book_splitter.detect_pattern_from_text(source_text)
    pattern_used = (detected or "roman") if requested in (None, "auto") else requested
    with _quiet_stdout():
        sections = _detect_sections(
            source_text,
            pattern_type=pattern_used,
            custom_regex=custom_regex,
            min_chapter_size=min_chapter_size,
            front_matter_titles=front_matter_titles,
            back_matter_titles=back_matter_titles,
            auto_detect_front_matter=auto_detect_front_matter,
            auto_detect_back_matter=auto_detect_back_matter,
            auto_strip_boilerplate=auto_strip_boilerplate,
        )
    warnings = book_splitter.split_sanity_warnings(
        sections, source_text, pattern_used=pattern_used, detected=detected)
    return {
        "detected": detected,
        "pattern_used": pattern_used,
        "sections": sections,
        "warnings": warnings,
    }


def split_preview(
    project: str,
    *,
    pattern_type: str = "auto",
    custom_regex: str | None = None,
    min_chapter_size: int | None = None,
    front_matter_titles: list[str] | None = None,
    back_matter_titles: list[str] | None = None,
    auto_detect_front_matter: bool = True,
    auto_detect_back_matter: bool = True,
    auto_strip_boilerplate: bool = True,
) -> dict:
    """Dry-run a chapter split and return the detected sections — writes NO files.

    Mirrors the web GUI's ``/split/preview`` so the agent can see how the chosen
    pattern and any declared front/back-matter titles resolve (each section comes
    back tagged ``front_matter`` / ``chapter`` / ``back_matter``) before
    committing the split with :func:`split_apply`. Navigation/boilerplate
    (Contents, Title Page, ...) is stripped and reported under ``dropped``.
    """
    project_dir = state.resolve_project_dir(project, must_exist=True)
    book_text = _read_source(project_dir)
    dropped: list[dict] = []
    with _quiet_stdout():
        chapters = _detect_sections(
            book_text,
            pattern_type=pattern_type,
            custom_regex=custom_regex,
            min_chapter_size=min_chapter_size,
            front_matter_titles=front_matter_titles,
            back_matter_titles=back_matter_titles,
            auto_detect_front_matter=auto_detect_front_matter,
            auto_detect_back_matter=auto_detect_back_matter,
            auto_strip_boilerplate=auto_strip_boilerplate,
            collect_dropped=dropped,
        )
    sections = [
        {
            "name": _section_display_name(ch),
            "kind": ch.kind,
            "label": ch.label,
            "number": ch.number,
            "words": len(ch.content.split()),
            "preview": ch.content[:200],
        }
        for ch in chapters
    ]
    from src import book_splitter
    detected = book_splitter.detect_pattern_from_text(book_text)
    pattern_used = (detected or "roman") if pattern_type in (None, "auto") else pattern_type
    return {
        "project_dir": str(project_dir),
        "section_count": len(sections),
        "counts": _kind_counts(chapters),
        "pattern_used": pattern_used,
        "suggested_pattern": detected,
        "sections": sections,
        "dropped": dropped,
        "warnings": book_splitter.split_sanity_warnings(
            chapters, book_text, pattern_used=pattern_used, detected=detected),
        "files_written": False,
    }


def split_apply(
    project: str,
    *,
    pattern_type: str = "auto",
    custom_regex: str | None = None,
    min_chapter_size: int | None = None,
    front_matter_titles: list[str] | None = None,
    back_matter_titles: list[str] | None = None,
    auto_detect_front_matter: bool = True,
    auto_detect_back_matter: bool = True,
    auto_strip_boilerplate: bool = True,
) -> dict:
    """Commit a chapter split: (re)write ``chapters/`` from ``source.txt``.

    Mirrors the web GUI's ``/split``. Clears stale ``chapter_*.txt`` first so a
    smaller re-split never leaves orphaned files behind (``save_chapters_to_files``
    writes by ``position_index`` and would otherwise leave higher-numbered files).
    Navigation/boilerplate (Contents, Title Page, ...) is stripped and reported
    under ``dropped``.
    """
    from src.book_splitter import save_chapters_to_files

    project_dir = state.resolve_project_dir(project, must_exist=True)
    book_text = _read_source(project_dir)
    chapters_dir = project_dir / "chapters"
    dropped: list[dict] = []
    with _quiet_stdout():
        chapters = _detect_sections(
            book_text,
            pattern_type=pattern_type,
            custom_regex=custom_regex,
            min_chapter_size=min_chapter_size,
            front_matter_titles=front_matter_titles,
            back_matter_titles=back_matter_titles,
            auto_detect_front_matter=auto_detect_front_matter,
            auto_detect_back_matter=auto_detect_back_matter,
            auto_strip_boilerplate=auto_strip_boilerplate,
            collect_dropped=dropped,
        )
        if chapters_dir.exists():
            for stale in chapters_dir.glob("chapter_*.txt"):
                stale.unlink()
        save_chapters_to_files(chapters, str(chapters_dir))

    written = sorted(chapters_dir.glob("chapter_*.txt"))
    from src import book_splitter
    detected = book_splitter.detect_pattern_from_text(book_text)
    pattern_used = (detected or "roman") if pattern_type in (None, "auto") else pattern_type
    return {
        "project_dir": str(project_dir),
        "chapter_count": len(chapters),
        "counts": _kind_counts(chapters),
        "pattern_used": pattern_used,
        "suggested_pattern": detected,
        "chapters": [p.stem for p in written],
        "dropped": dropped,
        "warnings": book_splitter.split_sanity_warnings(
            chapters, book_text, pattern_used=pattern_used, detected=detected),
        "files_written": True,
        "sections": [
            {
                "name": _section_display_name(ch),
                "kind": ch.kind,
                "label": ch.label,
                "number": ch.number,
                "words": len(ch.content.split()),
            }
            for ch in chapters
        ],
    }


# ── style guide beat ───────────────────────────────────────────────────────

def _options_with_ids(q: dict) -> list[dict]:
    """Render a question's options as ``[{"id": slug, "label": label}, …]``.

    The stable ``id`` lets the agent pass the user's pick straight through to
    ``style_answers.json`` instead of bookkeeping a positional index.
    """
    from src.style_guide_wizard import option_ids

    ids = option_ids(q)
    return [
        {"id": ids[i], "label": o["label"]}
        for i, o in enumerate(q.get("options", []))
    ]


def style_guide_prepare_questions(project: str) -> dict:
    """Gather fixed + feature-detected questions; print them for the agent to ask."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.ensure_harness_dir(project_dir)

    cfg = state.load_config(project_dir)

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from src.style_guide_wizard import (
        dialect_id_from_locale,
        get_active_questions,
        load_source_sample,
    )

    source = load_source_sample(project_dir)
    with _quiet_stdout():
        fixed, conditional, manifest = get_active_questions(project_dir)

    present = [name for name, r in manifest.features.items() if r.present]
    for q in conditional:  # attach the detected hint, as the old heredoc did
        feature = q.get("requires", {}).get("feature")
        if feature and feature in manifest.features and manifest.features[feature].evidence:
            q["_detected_hint"] = manifest.features[feature].evidence[0]
    allq = list(fixed) + list(conditional)

    # Pre-answer the redundant `dialect` question from the locale chosen at setup
    # (es-mx → mexican_spanish) so the agent confirms rather than re-asks it.
    locale = cfg.get("locale") or ""
    dialect_q = next((q for q in allq if q.get("id") == "dialect"), None)
    prefilled_dialect = dialect_id_from_locale(locale, dialect_q) if dialect_q else None

    (hdir / "style_source.txt").write_text(source, encoding="utf-8")
    (hdir / "style_fixed.json").write_text(json.dumps(fixed), encoding="utf-8")
    (hdir / "style_questions.json").write_text(json.dumps(allq), encoding="utf-8")

    def _question_entry(q: dict) -> dict:
        entry = {
            "id": q["id"],
            "question": q["question"],
            "options": _options_with_ids(q),
            "hint": q.get("_detected_hint", ""),
        }
        if q.get("id") == "dialect" and prefilled_dialect:
            entry["prefilled"] = prefilled_dialect
            entry["prefilled_reason"] = f"from setup locale {locale!r}"
        return entry

    instructions = (
        "STOP: ask the user every question and WAIT for their answers — do not answer "
        "for them or pick defaults. Then Write {question_id: option_id_or_label_or_custom_string} "
        "to answers_path (use each option's `id` or exact `label`; a numeric index still "
        "works; any other string is a custom answer), then run `style-guide prepare-followups`."
    )
    if prefilled_dialect:
        instructions += (
            f" The `dialect` question is pre-answered from the setup locale "
            f"(`prefilled`: {prefilled_dialect!r}) — present it as a confirm/override default, "
            "not a blank ask, and keep that id in answers unless the user changes it."
        )

    return {
        "detected_features": present or [],
        "questions": [_question_entry(q) for q in allq],
        "answers_path": str(hdir / "style_answers.json"),
        "instructions": instructions,
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
                "options": _options_with_ids(q),
            }
            for q in extra
        ],
        "answers_path": str(hdir / "style_answers.json"),
        "instructions": (
            "STOP: ask the user these follow-ups and WAIT for their answers — do not answer "
            "for them or pick defaults. Then rewrite answers_path with the FULL answer set "
            "(prior answers + these; use each option's `id` or exact `label`, or a custom string), "
            "then run `style-guide prepare-draft`."
        ),
    }


def style_guide_prepare_draft(project: str, *, answers: str | None = None) -> dict:
    """Build the style-guide prompt (you are the LLM that drafts the prose)."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    from src.style_guide_wizard import (
        build_style_guide_prompt,
        dialect_id_from_locale,
        resolve_answer,
    )

    allq = json.loads(_read(hdir / "style_questions.json"))
    answers_path = Path(answers) if answers else hdir / "style_answers.json"
    ans = json.loads(_read(answers_path))
    source = _read(hdir / "style_source.txt")

    # Defense-in-depth: if the agent treated the locale-prefilled dialect as
    # confirmed-by-default and left it out of the answers, fill it from the
    # locale so the dialect section is never blank.
    if "dialect" not in ans:
        dialect_q = next((q for q in allq if q.get("id") == "dialect"), None)
        mapped = dialect_id_from_locale(cfg.get("locale") or "", dialect_q) if dialect_q else None
        if mapped:
            ans["dialect"] = mapped

    prompt = build_style_guide_prompt(allq, ans, source, cfg["target_language"], cfg["locale"])
    prompt_path = hdir / "style_guide_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    # Echo how each answer resolved so the agent can confirm a mistyped id wasn't
    # silently demoted to a custom answer before the draft is generated.
    resolved = []
    for q in allq:
        if q["id"] in ans:
            label, _effect, matched = resolve_answer(q, ans[q["id"]])
            resolved.append({
                "id": q["id"],
                "question": q["question"],
                "answer": label,
                "source": "option" if matched else "custom",
            })
    unanswered = [q["id"] for q in allq if q["id"] not in ans]
    return {
        "prompt_path": str(prompt_path),
        "draft_path": str(hdir / "style_guide_draft.txt"),
        "resolved_answers": resolved,
        "unanswered": unanswered,
        "instructions": (
            "Check resolved_answers (each should read 'option' unless you meant a custom "
            "answer; 'custom' on a question you tried to answer by id means a typo). Then read "
            "prompt_path, draft the style-guide prose to draft_path, refine it with the user, "
            "then run `style-guide commit`."
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

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.extract_glossary_candidates import extract_candidates
    from src.glossary_bootstrap import build_glossary_prompt
    from src.style_guide_wizard import load_source_sample
    from src.utils.source_text import load_clean_source_text

    # Extract from the cleanest available text (chunks/ -> chapters/), so front
    # matter (TOC, copyright, chapter-title fragments) isn't fed to the
    # extractor. Mirrors the GUI route; falls back to raw source.txt only if
    # neither exists. See FRICTION_LOG_5 #27 (151-vs-200 candidate gap).
    source, _, source_kind = load_clean_source_text(project_dir)
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
        "source_kind": source_kind,  # "chunks"/"chapters" => front matter excluded; "source" => not
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

    from src.harness_guard import diacritic_warning, guard_glossary_proposals, validate_glossary_file
    from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
    from src.utils.file_io import save_glossary

    cfg = state.load_config(project_dir)
    draft_path = Path(draft) if draft else hdir / "glossary_draft.json"
    proposals = json.loads(_read(draft_path))
    guard_glossary_proposals(proposals)  # raises HarnessValidationError -> re-draft
    # Soft, non-blocking smell-check for an accent-stripped draft (see #21): the structural
    # guard above passes pure ASCII, so surface the warning for the agent + approval gate.
    warn = diacritic_warning(proposals, cfg.get("language_code"))
    glossary = proposals_to_glossary(glossary_terms_from_proposals(proposals))
    out = project_dir / "glossary.json"
    save_glossary(glossary, out)
    validate_glossary_file(out)  # belt-and-suspenders
    return {
        "glossary_path": str(out),
        "term_count": len(glossary.terms),
        "warnings": [warn] if warn else [],
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
        "dialect_score": round(b.dialect_score, 3),
        "dialogue_score": round(b.dialogue_score, 3),
        "verse_score": round(b.verse_score, 3),
        "suggested_target_size": b.suggested_target_size,
        "wordfreq_available": WORDFREQ_AVAILABLE,
        "chapters": [
            {
                "chapter_id": cd.chapter_id,
                "difficulty": round(cd.metrics.difficulty, 3),
                "dialogue_score": round(cd.metrics.dialogue_score, 3),
                "verse_score": round(cd.metrics.verse_score, 3),
                "suggested_target_size": cd.metrics.suggested_target_size,
            }
            for cd in manifest.chapters
        ],
        "next": "chunk --size <N> (default to suggested_target_size unless the user overrides)",
    }


# ── translate subagent backend (Phase B): prepare / commit ─────────────────
#
#   translate-prepare ─► .harness/translate/<id>.prompt.txt + manifest.json
#        (no spend)        the agent spawns one model-pinned worker per entry:
#                          worker reads prompt_path -> writes prose to draft_path
#   translate-commit  ─► guard each draft -> apply_translation + save_chunk
#        (idempotent)      + a provenance log per chunk; reports failed/missing
#
# The worker returns PROSE only and writes it to draft_path, so the orchestrator
# never holds translation text in its context (eng review A2). The actual prompt
# is build_translation_prompt's output — identical to the API path (A1).


def translate_prepare(
    project: str,
    *,
    chapters: str | None = None,
    worker_model: str | None = None,
    worker_thinking: bool | None = None,
    parallelism: str | None = None,
    window: int | None = None,
    batch_size: int | None = None,
) -> dict:
    """Render per-chunk prompts + a manifest for the subagent backend (no spend).

    For every UNTRANSLATED chunk in the requested chapters (``--chapters`` like
    ``1-2`` / ``3,7``; all chapters when omitted), render the same prompt the API
    path sends and write it to ``.harness/translate/<id>.prompt.txt``; assign a
    ``draft_path`` the worker writes its prose to.

    ``previous_chapter_context`` carries continuity from the preceding chunk
    (document order). When that preceding chunk is already **committed**, the
    context now includes BOTH its source tail and its translation (the same
    ``extract_previous_chapter_context`` block the web reader uses), so a chunk
    translated *after* its predecessor sees the predecessor's Spanish. That is
    why the sequential / chapter-parallel spawn modes re-run ``translate-prepare``
    after each ``translate-commit``: a just-finished translation then flows into
    the next chunk's prompt. When the predecessor is not yet translated (e.g. the
    all-parallel mode, or two chunks prepared in one pass) the context degrades
    to source-only — never blocking.

    ``parallelism`` (``sequential`` | ``chapter`` | ``all``), ``window``, and
    ``batch_size`` are the user's spawn-mode choice; when passed they are persisted
    to the project config so the later "translate the rest" batch reuses them, and
    they are echoed back under ``spawn_plan`` so the agent can confirm "same behavior
    as before". ``batch_size`` is the recommended fan-out width for one spawn wave —
    a saved number the agent ramps from and throttles back to on a 529 (SKILL.md
    Step 4B backoff guidance).

    ``spawn_mode_moot`` is ``True`` when every in-scope chapter is a single chunk: the
    continuity modes are then equivalent to all-parallel, so the agent can skip the
    spawn-mode question entirely.

    Prepare is **non-destructive**: a non-empty ``.draft.txt`` from a prior prepare is
    kept (only empty drafts are cleared), and every mappable uncommitted draft on disk —
    even one whose chunk is outside this call's ``--chapters`` scope — is rescued into
    the new manifest and counted in ``rescued_prior_drafts``. A narrower re-prepare
    therefore recovers (rather than wipes) a just-finished wave before
    ``translate-commit`` lands it; unmappable or unreadable drafts are left on disk.

    ``window`` is clamped to ``batch_size`` when it would exceed the fan-out throttle
    (chapter-parallel first-position waves spawn one worker per chapter in the window).
    """
    project_dir = state.resolve_project_dir(project)
    hdir = state.ensure_harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    # Persist the spawn knobs the agent passes (the "save that response" beat).
    persist: dict = {}
    if worker_model is not None:
        persist["worker_model"] = worker_model
    if worker_thinking is not None:
        persist["worker_thinking"] = bool(worker_thinking)
    if parallelism is not None:
        if parallelism not in _PARALLELISM_MODES:
            return {
                "error": f"invalid parallelism {parallelism!r}; "
                         f"use one of {sorted(_PARALLELISM_MODES)}",
                "manifest": [],
            }
        persist["parallelism"] = parallelism
    if window is not None:
        try:
            window_int = int(window)
        except (TypeError, ValueError):
            return {"error": f"invalid window {window!r}; must be a positive integer",
                    "manifest": []}
        if window_int < 1:
            return {"error": f"invalid window {window!r}; must be a positive integer",
                    "manifest": []}
        persist["parallel_window"] = window_int
    if batch_size is not None:
        try:
            batch_int = int(batch_size)
        except (TypeError, ValueError):
            return {"error": f"invalid batch_size {batch_size!r}; must be a positive integer",
                    "manifest": []}
        if batch_int < 1:
            return {"error": f"invalid batch_size {batch_size!r}; must be a positive integer",
                    "manifest": []}
        persist["batch_size"] = batch_int
    if persist:
        cfg.update(persist)
        state.save_config(project_dir, cfg)

    worker_model = cfg.get("worker_model") or "sonnet"
    # Resolve the worker's thinking choice, then gate it on model support: a
    # non-thinking worker (e.g. `fable`, always-on) can never be flagged on — the
    # analog of the GUI hiding+unchecking the checkbox for such models.
    worker_thinking = bool(cfg.get("worker_thinking")) and _worker_supports_thinking(worker_model)
    spawn_plan, spawn_patches = _spawn_plan_from_cfg(cfg)
    if spawn_patches:
        cfg.update(spawn_patches)
        state.save_config(project_dir, cfg)

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters, parse_chapter_range
    from src.api_translator import build_translation_prompt
    from src.translator import extract_previous_chapter_context
    from src.utils.file_io import load_chunk, load_glossary, load_style_guide
    from src.utils.text_utils import image_filenames

    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {"error": "no chunks yet — run `chunk` first", "manifest": []}

    all_discovered = discover_chapters(chunks_dir)
    discovered = all_discovered
    if chapters:
        try:
            requested = parse_chapter_range(chapters)
        except (ValueError, TypeError) as exc:
            return {"error": f"invalid --chapters value {chapters!r}: {exc}", "manifest": []}
        discovered = {k: v for k, v in all_discovered.items() if k in requested}
        if not discovered:
            return {
                "manifest": [],
                "chapters": chapters,
                "available_chapters": sorted(all_discovered.keys()),
                "note": f"no matching chapters for --chapters {chapters}",
            }

    glossary = None
    if (project_dir / "glossary.json").exists():
        glossary = load_glossary(project_dir / "glossary.json")
    style_guide = None
    if (project_dir / "style.json").exists():
        style_guide = load_style_guide(project_dir / "style.json")

    title = cfg.get("title") or project_dir.name
    target_lang = cfg.get("target_language") or "Spanish"

    # Book-level prompt-prefix opt-ins (see build_translation_prompt): forcing the
    # dialogue block / image bullet onto every chunk keeps the fixed prompt prefix
    # byte-identical across the book so it stays cacheable. always_include_dialogue is
    # a per-book config flag; book_has_images is computed over the WHOLE book (not just
    # the in-scope --chapters) so the constant is stable regardless of which wave runs.
    always_include_dialogue = bool(cfg.get("always_include_dialogue", False))
    book_has_images = any(
        image_filenames(load_chunk(cp).source_text)
        for cps in all_discovered.values()
        for cp in cps
    )

    translate_dir = hdir / "translate"
    translate_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    total_words = 0
    # Track the preceding chunk's source AND translation (document order) so an
    # untranslated chunk whose predecessor is already committed gets EN+ES context.
    prev_source: str | None = None
    prev_translated: str | None = None
    for chapter_id in sorted(discovered):
        for cp in discovered[chapter_id]:
            chunk = load_chunk(cp)
            if chunk.has_translation:
                # Already committed: it is the continuity context for the next
                # untranslated chunk — carry both its source and its Spanish.
                prev_source = chunk.source_text
                prev_translated = chunk.translated_text
                continue
            prev_context = (
                extract_previous_chapter_context(
                    prev_source,
                    previous_translated_text=prev_translated,
                    context_language="both",
                    source_language="English",
                    target_language=target_lang,
                )
                if prev_source else ""
            )
            prompt = build_translation_prompt(
                chunk,
                glossary=glossary,
                style_guide=style_guide,
                project_name=title,
                source_language="English",
                target_language=target_lang,
                previous_chapter_context=prev_context,
                always_include_dialogue=always_include_dialogue,
                always_include_image_instructions=book_has_images,
            )
            prompt_path = translate_dir / f"{chunk.id}.prompt.txt"
            draft_path = translate_dir / f"{chunk.id}.draft.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            total_words += chunk.word_count
            # Non-destructive: keep any non-empty draft from a prior prepare run — a
            # re-spawned worker overwrites it, and translate-commit lands it as-is if
            # not. Only clear an empty/whitespace draft so a stale zero-byte file can't
            # masquerade as work. (Fixes the Pollyanna hiccup #1 draft wipe.)
            draft_text = _read_draft_text(draft_path) if draft_path.exists() else None
            if draft_text is not None and not draft_text.strip():
                draft_path.unlink(missing_ok=True)
            entries.append({
                "chunk_id": chunk.id,
                "chapter_id": chunk.chapter_id,
                "chunk_path": str(cp),
                "prompt_path": str(prompt_path),
                "draft_path": str(draft_path),
                "source_word_count": chunk.word_count,
            })
            # This untranslated chunk becomes the next chunk's predecessor; only
            # its source exists yet, so the next prompt gets source-only context
            # until this chunk is committed and prepare re-runs.
            prev_source = chunk.source_text
            prev_translated = None

    # Rescue uncommitted-but-drafted chunks so a finished wave is never orphaned when
    # prepare is called again before translate-commit — even if that draft's chunk fell
    # outside this prepare's --chapters scope. We scan the drafts *on disk* (not just the
    # last manifest's entries), so a narrower re-prepare can't strand a wider wave.
    # (Fixes the Pollyanna hiccup #1 draft wipe alongside the non-destructive unlink.)
    existing_ids = {e["chunk_id"] for e in entries}
    # Pre-computed entry metadata from the last manifest, if any — used only to resolve a
    # draft's chunk_path cheaply before falling back to a full-project scan.
    prior_by_id: dict[str, dict] = {}
    prior_manifest_path = translate_dir / "manifest.json"
    if prior_manifest_path.exists():
        try:
            prior_doc = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
            prior_by_id = {e["chunk_id"]: e for e in prior_doc.get("entries", [])
                           if isinstance(e, dict) and "chunk_id" in e}
        except (json.JSONDecodeError, OSError, TypeError):
            prior_by_id = {}
    # chunk_id -> chunk path across the *whole* project (not just in scope). Built lazily
    # and only when a stray draft can't be resolved from the prior manifest.
    id_to_path: dict[str, Path] | None = None
    rescued: list[dict] = []
    rescued_ids: set[str] = set()
    for draft in sorted(translate_dir.glob("*.draft.txt")):
        cid = draft.name[: -len(".draft.txt")]
        if cid in existing_ids or cid in rescued_ids:
            continue  # already represented in the new manifest
        draft_text = _read_draft_text(draft)
        if draft_text is None or not draft_text.strip():
            continue  # empty, whitespace-only, or unreadable draft is not work
        chunk_cp: Path | None = None
        prior_entry = prior_by_id.get(cid)
        if prior_entry and prior_entry.get("chunk_path"):
            candidate = Path(prior_entry["chunk_path"])
            if candidate.exists():
                try:
                    if load_chunk(candidate).id == cid:
                        chunk_cp = candidate
                except Exception:
                    pass  # stale/corrupt manifest path — fall through to id_to_path
        if chunk_cp is None or not chunk_cp.exists():
            if id_to_path is None:
                id_to_path = {}
                for cps in all_discovered.values():
                    for cp2 in cps:
                        try:
                            id_to_path[load_chunk(cp2).id] = cp2
                        except Exception:
                            continue
            chunk_cp = id_to_path.get(cid)
        if chunk_cp is None:
            continue  # can't map this draft to a current chunk — leave it on disk
        try:
            rescue_chunk = load_chunk(chunk_cp)
        except Exception:
            continue
        if rescue_chunk.has_translation:
            continue  # already committed; the leftover draft is harmless
        rescued_ids.add(cid)
        rescued.append({
            "chunk_id": cid,
            "chapter_id": rescue_chunk.chapter_id,
            "chunk_path": str(chunk_cp),
            "prompt_path": str(translate_dir / f"{cid}.prompt.txt"),
            "draft_path": str(draft),
            "source_word_count": rescue_chunk.word_count,
        })

    if rescued:
        total_words += sum(e.get("source_word_count", 0) for e in rescued)
        entries = rescued + entries

    # When every in-scope chapter is a single chunk, the continuity spawn modes
    # (sequential / chapter-parallel window) collapse to "all-parallel in bounded
    # batches" — there is no later chunk to inherit a previous chunk's Spanish. The
    # agent uses this to skip the spawn-mode ceremony for single-chunk-per-chapter
    # books (SKILL.md Step 4B-0b).
    spawn_mode_moot = bool(discovered) and all(
        len(cps) <= 1 for cps in discovered.values()
    )

    manifest_doc = {
        "worker_model": worker_model,
        "worker_thinking": worker_thinking,
        "chapters": chapters or "all",
        "spawn_plan": spawn_plan,
        "spawn_mode_moot": spawn_mode_moot,
        "entries": entries,
    }
    (translate_dir / "manifest.json").write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "manifest": entries,
        "manifest_path": str(translate_dir / "manifest.json"),
        "worker_model": worker_model,
        "worker_thinking": worker_thinking,
        "spawn_plan": spawn_plan,
        "spawn_mode_moot": spawn_mode_moot,
        "usage_summary": {
            "chunks": len(entries),
            "source_words": total_words,
            "worker_model": worker_model,
            "worker_thinking": worker_thinking,
            "parallelism": spawn_plan["parallelism"],
            "window": spawn_plan["window"],
            "batch_size": spawn_plan["batch_size"],
            "spawn_mode_moot": spawn_mode_moot,
        },
        "rescued_prior_drafts": len(rescued),
        "chapters": chapters or "all",
        "instructions": (
            "For each manifest entry, spawn a worker pinned to worker_model that reads "
            "prompt_path and writes ONLY the translated prose to draft_path. Then run "
            "`translate-commit`. Nothing here spends or calls an API."
            if entries else
            "Nothing to translate — all chunks in scope already have translations."
        ),
    }


def translate_commit(
    project: str,
    *,
    worker_model: str | None = None,
    allow_problems: list[str] | None = None,
) -> dict:
    """Validate worker drafts, stamp the chunks, and report results (idempotent).

    Reads the ``translate-prepare`` manifest; for each entry reads the worker's
    draft prose, runs ``guard_translation_draft``, and on success writes a
    provenance prompt-log + stamps the chunk (``apply_translation`` + ``save_chunk``).
    Already-translated chunks are skipped, so a killed run resumes by re-running.
    Failed/missing chunks are reported for re-spawn (never stamped).

    ``allow_problems`` waives known guard false-positives (e.g. the Roman numeral
    ``XXX`` tripping the placeholder check): any guard problem whose message
    contains one of these substrings (case-insensitive) is dropped, and the chunk
    commits if no *other* problem remains — so image-parity / echo / length guards
    still block. Waived problems are reported (top-level ``waived`` map) and logged
    in the chunk's provenance, so a forced commit is never silent.
    """
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.api_translator import apply_translation
    from src.harness_guard import guard_translation_draft
    from src.utils.file_io import load_chunk, save_chunk
    from src.utils.prompt_logger import log_prompt

    manifest_path = hdir / "translate" / "manifest.json"
    if not manifest_path.exists():
        return {"error": "no manifest — run `translate-prepare` first", "committed": []}
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"manifest unreadable (truncated write?): {exc}", "committed": []}
    worker_model = worker_model or doc.get("worker_model") or "sonnet"
    entries = doc.get("entries", [])
    allow_subs = [s.lower() for s in (allow_problems or []) if s.strip()]

    committed: list[str] = []
    failed: list[dict] = []
    missing: list[str] = []
    skipped: list[str] = []
    waived: dict[str, list[str]] = {}
    evaluated = 0
    project_slug = project_dir.name

    from web_ui.evaluations import (
        _load_project_blacklist,
        _load_project_glossary,
        evaluate_and_persist_chunk,
    )

    glossary = _load_project_glossary(project_dir)
    blacklist = _load_project_blacklist(project_dir)

    for entry in entries:
        cp = Path(entry["chunk_path"])
        chunk = load_chunk(cp)
        if chunk.has_translation:
            skipped.append(entry["chunk_id"])
            continue
        draft_path = Path(entry["draft_path"])
        if not draft_path.exists():
            missing.append(entry["chunk_id"])
            continue
        prose = draft_path.read_text(encoding="utf-8")
        problems = guard_translation_draft(chunk, prose)
        if problems and allow_subs:
            kept = [p for p in problems if not any(s in p.lower() for s in allow_subs)]
            waived_here = [p for p in problems if p not in kept]
            if waived_here:
                waived[entry["chunk_id"]] = waived_here
            problems = kept
        if problems:
            failed.append({"chunk_id": entry["chunk_id"], "problems": problems})
            continue
        prompt_path = Path(entry["prompt_path"])
        prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        waived_note = waived.get(entry["chunk_id"])
        if waived_note:
            prompt_text += "\n\n[translate-commit] guard problems waived via --allow-problem:\n" + \
                "\n".join(f"  - {p}" for p in waived_note)
        log_path = log_prompt(
            prompt=prompt_text,
            response=prose.strip(),
            provider="harness-subagent",
            model=worker_model,
            call_type="translation",
            mode="harness-subagent",
            chunk_id=chunk.id,
            project_slug=project_slug,
        )
        apply_translation(chunk, prose, log_path=log_path)
        save_chunk(chunk, cp)
        try:
            evaluate_and_persist_chunk(
                project_dir, chunk, glossary=glossary, blacklist=blacklist
            )
            evaluated += 1
        except Exception:
            pass
        committed.append(entry["chunk_id"])

    return {
        "committed": committed,
        "failed": failed,
        "missing": missing,
        "skipped_already_translated": skipped,
        "waived": waived,
        "evaluated": evaluated,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "missing": len(missing),
            "skipped": len(skipped),
            "evaluated": evaluated,
        },
        "instructions": (
            "Re-spawn workers for any `failed` (fix per the named problems) and `missing` "
            "chunk_ids — write fresh prose to their draft_path — then run `translate-commit` "
            "again. Cap re-spawns ~3, then surface for manual edit."
            if (failed or missing) else
            "All in-scope chunks committed. Proceed to combine/epub."
        ),
    }


# ── chunk / cost / translate / epub (subprocess wrappers) ──────────────────

def chunk(
    project: str,
    *,
    size: int,
    chapters: str | None = None,
    per_chapter: bool = False,
) -> dict:
    """Chunk at ``size`` and print the cost estimate, halting before any spend.

    Wraps ``translate_book.py --start-stage chunk --cost-only`` so the run chunks
    once and then stops at the estimate (``--cost-only`` exits before a single
    chunk is translated — it physically cannot spend). ``--chapters`` scopes the
    printed estimate to those chapters (chunking itself still covers the book).

    With ``per_chapter`` set, each chapter is sized by the difficulty scorer's
    ``suggested_target_size`` (read from the cached ``difficulty.json`` the
    ``difficulty`` step wrote); ``size`` stays the fallback for any chapter not in
    the manifest. Requires a prior ``difficulty`` run — fails loudly otherwise.
    """
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    cmd = [
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "chunk",
        "--cost-only",
        "--provider", cfg["provider"],
        "--model", cfg["model"],
        "--chunk-size", str(int(size)),
        # The overlap/combine de-dup path is known-broken (FRICTION_LOG_4 #20); the
        # harness never creates overlapping chunks. Pass 0 explicitly so this holds
        # even if the CLI default ever changes.
        "--overlap-paragraphs", "0",
        "--min-overlap-words", "0",
    ]

    if per_chapter:
        from src.difficulty_scorer import load_manifest

        manifest = load_manifest(project_dir)
        if manifest is None:
            raise FileNotFoundError(
                "per-chapter chunking needs difficulty.json - run "
                "`harness.py difficulty` first."
            )
        sizes = {
            cd.chapter_id: cd.metrics.suggested_target_size
            for cd in manifest.chapters
        }
        hdir = state.ensure_harness_dir(project_dir)
        sizes_path = Path(hdir) / "chunk_sizes.json"
        sizes_path.write_text(
            json.dumps(sizes, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cmd += ["--chunk-sizes", str(sizes_path)]

    if chapters:
        cmd += ["--chapters", chapters]
    return _stream_result("chunk", *_run_script(cmd))


def cost(project: str, *, chapters: str | None = None) -> dict:
    """Re-print the translation cost estimate WITHOUT spending (pure estimator)."""
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    cmd = [
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "translate",
        "--cost-only",
        "--provider", cfg["provider"],
        "--model", cfg["model"],
    ]
    if chapters:
        cmd += ["--chapters", chapters]
    return _stream_result("cost", *_run_script(cmd))


def translate(
    project: str,
    *,
    yes: bool,
    model: str | None = None,
    provider: str | None = None,
    chapters: str | None = None,
    enable_thinking: bool | None = None,
) -> int | dict:
    """The one paid step. Fails closed without ``--yes`` (cost gate, defense-in-depth).

    ``--chapters`` (e.g. ``1-2`` / ``3,7``) translates only those chapters — the
    chapter-at-a-time workflow (translate, read, then continue). Resume is free:
    re-running skips chunks that already have a translation.
    """
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    if not yes:
        print(
            "Refusing to translate without --yes. The cost estimate must be approved by the "
            "user in a SEPARATE turn first; only then re-run `translate` with --yes.",
            file=sys.stderr,
        )
        return 2
    cmd = [
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "translate",
        "--yes",
        "--provider", provider or cfg["provider"],
        "--model", model or cfg["model"],
    ]
    if chapters:
        cmd += ["--chapters", chapters]
    # Absent (None) leaves the TRANSLATE_THINKING env default in force downstream;
    # only an explicit choice threads a flag through to translate_book.py.
    if enable_thinking is True:
        cmd.append("--thinking")
    elif enable_thinking is False:
        cmd.append("--no-thinking")
    return _stream_result("translate", *_run_script(cmd))


def epub(project: str, *, title: str | None = None, author: str | None = None, language: str | None = None) -> int | dict:
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
    return _stream_result("epub", *_run_script([
        "scripts/build_epub.py",
        str(project_dir),
        "--title", title,
        "--author", author,
        "--language", language,
    ]))


def align(
    project: str,
    *,
    chapters: str | None = None,
    source_lang_code: str | None = None,
    target_lang_code: str | None = None,
    reader_host: str = "localhost",
    reader_port: int = 5000,
) -> dict:
    """Compute sentence alignments for fully-translated chapters (reader mode).

    Wraps ``align_chapter_chunks`` per chapter so a freshly-translated set becomes
    readable in the web reader with zero manual steps — the per-set finisher the
    skill runs after each ``translate-commit`` batch. ``chapters`` (e.g. ``1-2`` /
    ``3,7``; all when omitted) limits the work; a chapter that is not *fully*
    translated is skipped (so running over the whole book only aligns what is
    ready). Returns the aligned / skipped chapter lists plus a reader link to the
    first newly-aligned chapter.
    """
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {"error": "no chunks yet — translate first", "aligned": [], "skipped": []}

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters, parse_chapter_range
    from src.sentence_aligner import align_chapter_chunks
    from src.utils.file_io import load_chunk

    source_lang_code = source_lang_code or "en"
    target_lang_code = target_lang_code or cfg.get("language_code") or "es"

    all_discovered = discover_chapters(chunks_dir)
    discovered = all_discovered
    if chapters:
        try:
            requested = parse_chapter_range(chapters)
        except (ValueError, TypeError) as exc:
            return {"error": f"invalid chapters value {chapters!r}: {exc}",
                    "aligned": [], "skipped": []}
        discovered = {k: v for k, v in all_discovered.items() if k in requested}
        if not discovered:
            return {
                "aligned": [],
                "skipped": [],
                "chapters": chapters,
                "available_chapters": sorted(all_discovered.keys()),
                "note": f"no matching chapters for chapters {chapters}",
            }

    align_dir = project_dir / "alignments"
    align_dir.mkdir(exist_ok=True)
    slug = project_dir.name

    aligned: list[dict] = []
    skipped: list[dict] = []
    # Surface a per-chapter aligner failure (e.g. embedding model unavailable)
    # without crashing the batch — keep the clean-JSON-on-stdout contract.
    align_error: str | None = None
    # The aligner loads an embedding model and is chatty; keep stdout clean JSON.
    with _quiet_stdout():
        for chapter_id in sorted(discovered):
            chunk_paths = discovered[chapter_id]
            chunks = [load_chunk(cp) for cp in chunk_paths]
            if not all(c.has_translation for c in chunks):
                skipped.append({"chapter_id": chapter_id, "reason": "not fully translated"})
                continue
            try:
                result = align_chapter_chunks(
                    chunk_paths=[str(p) for p in chunk_paths],
                    project_id=slug,
                    chapter_id=chapter_id,
                    source_lang=source_lang_code,
                    target_lang=target_lang_code,
                    output_path=str(align_dir / f"{chapter_id}.json"),
                )
            except Exception as exc:  # noqa: BLE001 - report, don't crash the batch
                align_error = f"align failed at {chapter_id}: {exc}"
                skipped.append({"chapter_id": chapter_id, "reason": f"align error: {exc}"})
                break
            aligned.append({
                "chapter_id": chapter_id,
                "es_count": result.get("es_count"),
                "high_confidence_pct": result.get("high_confidence_pct"),
            })

    reader_base = f"http://{reader_host}:{reader_port}/read/{slug}"
    first = aligned[0]["chapter_id"] if aligned else None
    out = {
        "aligned": aligned,
        "skipped": skipped,
        "alignments_dir": str(align_dir),
        "reader_base": reader_base,
        "reader_first": f"{reader_base}/{first}" if first else None,
        "reader_links": [f"{reader_base}/{a['chapter_id']}" for a in aligned],
        "instructions": (
            f"Aligned {len(aligned)} chapter(s). Ensure the reader is running "
            f"(`python web_ui/app.py`), then open reader_first."
            if aligned else
            "No chapters were aligned — translate a chapter set fully first."
        ),
    }
    if align_error:
        out["error"] = align_error
    return out


def show_translation(
    project: str,
    *,
    chapters: str | None = None,
    max_chunks: int | None = None,
    include_source: bool = True,
) -> dict:
    """Read committed source+translation back out of ``chunks/*.json`` (read-only).

    The supported way to display a translation for review. Committed translations
    live in ``projects/<slug>/chunks/*.json`` under **``translated_text``** (the
    English is ``source_text``); the per-worker ``.harness/translate/*.draft.txt``
    files are consumed at ``translate-commit`` and are empty afterward, so reading
    *them* for a sample returns nothing (friction-log #7). This command returns the
    real text so the agent never reads internal files or guesses the JSON key.

    ``chapters`` (e.g. ``1-2`` / ``3,7``; all when omitted) limits the scope;
    ``max_chunks`` caps the total chunks returned so a "show me a sample" call cannot
    flood context; ``include_source=False`` drops ``source_text`` from each chunk.
    """
    project_dir = state.resolve_project_dir(project)
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {"error": "no chunks yet — translate first", "chapters": []}

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters, parse_chapter_range
    from src.utils.file_io import load_chunk

    all_discovered = discover_chapters(chunks_dir)
    discovered = all_discovered
    if chapters:
        try:
            requested = parse_chapter_range(chapters)
        except (ValueError, TypeError) as exc:
            return {"error": f"invalid chapters value {chapters!r}: {exc}", "chapters": []}
        discovered = {k: v for k, v in all_discovered.items() if k in requested}
        if not discovered:
            return {
                "chapters": [],
                "requested": chapters,
                "available_chapters": sorted(all_discovered.keys()),
                "note": f"no matching chapters for chapters {chapters}",
            }

    out_chapters: list[dict] = []
    shown = 0
    total = 0
    truncated = False
    for chapter_id in sorted(discovered):
        chunks = sorted((load_chunk(cp) for cp in discovered[chapter_id]),
                        key=lambda c: c.position)
        rows: list[dict] = []
        translated = 0
        for chunk in chunks:
            total += 1
            if chunk.has_translation:
                translated += 1
            if max_chunks is not None and shown >= max_chunks:
                truncated = True
                continue
            row = {
                "id": chunk.id,
                "chapter_id": chunk.chapter_id,
                "position": chunk.position,
                "status": chunk.display_status,
                "has_translation": chunk.has_translation,
                "source_words": chunk.word_count,
                "translation_words": chunk.translation_word_count,
                "translated_text": chunk.translated_text,
            }
            if include_source:
                row["source_text"] = chunk.source_text
            rows.append(row)
            shown += 1
        out_chapters.append({
            "chapter_id": chapter_id,
            "total_chunks": len(chunks),
            "translated_chunks": translated,
            "chunks": rows,
        })

    return {
        "chapters": out_chapters,
        "available_chapters": sorted(all_discovered.keys()),
        "chunks_dir": str(chunks_dir),
        "shown_chunks": shown,
        "total_chunks": total,
        "truncated": truncated,
        # Self-document the keys the friction log #7 agent had to guess.
        "fields": {"translation": "translated_text", "source": "source_text"},
    }


# ── status (resume at a glance) ─────────────────────────────────────────────

def status(project: str) -> dict:
    """Report a project's pipeline progress at a glance (read-only; no spend).

    Answers "where is this project and what's left?" on a RESUME without hand-rolling
    a loop over ``chunks/*.json`` (friction-log #11). Reports which pipeline artifacts
    exist, per-chapter translated-vs-pending counts (via ``chunk.has_translation``),
    the saved spawn plan, and whether the book is single-chunk-per-chapter (so the
    spawn-mode choice is moot). ``stage`` is the one-word summary the agent leads with.
    """
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)

    artifacts = {
        "source": (project_dir / "source.txt").exists(),
        "chapters": (project_dir / "chapters").exists(),
        "style_guide": (project_dir / "style.json").exists(),
        "glossary": (project_dir / "glossary.json").exists(),
        "difficulty": (project_dir / "difficulty.json").exists(),
        "chunks": (project_dir / "chunks").exists(),
    }
    epubs = sorted(p.name for p in project_dir.glob("*.epub"))
    spawn_plan, _ = _spawn_plan_from_cfg(cfg)
    base = {
        "project": project_dir.name,
        "artifacts": artifacts,
        "epubs": epubs,
        "spawn_plan": spawn_plan,
        "worker_model": cfg.get("worker_model") or "sonnet",
        "run_id": cfg.get("run_id"),
    }

    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {
            **base,
            "stage": "pre-chunk",
            "spawn_mode_moot": None,
            "totals": {},
            "pending_chapters": [],
            "chapters": [],
            "next": "no chunks yet — run `difficulty` then `chunk` before translating.",
        }

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters
    from src.utils.file_io import load_chunk

    discovered = discover_chapters(chunks_dir)
    chapters_out: list[dict] = []
    total_chunks = translated_chunks = 0
    max_per_chapter = 0
    pending_chapters: list[str] = []
    for chapter_id in sorted(discovered):
        cps = discovered[chapter_id]
        max_per_chapter = max(max_per_chapter, len(cps))
        n_trans = sum(1 for cp in cps if load_chunk(cp).has_translation)
        total_chunks += len(cps)
        translated_chunks += n_trans
        complete = n_trans == len(cps)
        if not complete:
            pending_chapters.append(chapter_id)
        chapters_out.append({
            "chapter_id": chapter_id,
            "chunks": len(cps),
            "translated": n_trans,
            "complete": complete,
        })

    if total_chunks == 0:
        stage = "pre-chunk"
    elif translated_chunks == 0:
        stage = "untranslated"
    elif translated_chunks < total_chunks:
        stage = "partial"
    else:
        stage = "fully-translated"

    if stage == "fully-translated":
        nxt = "all chunks translated — run `epub` to (re)build the book."
    elif stage in ("untranslated", "partial"):
        nxt = ("run `translate-prepare` (optionally --chapters) to render prompts for "
               "the pending chunks, then spawn workers and `translate-commit`.")
    else:
        nxt = "no chunks yet — run `difficulty` then `chunk` before translating."

    return {
        **base,
        "stage": stage,
        "spawn_mode_moot": (max_per_chapter <= 1) if total_chunks else None,
        "totals": {
            "chapters": len(discovered),
            "complete_chapters": sum(1 for c in chapters_out if c["complete"]),
            "total_chunks": total_chunks,
            "translated_chunks": translated_chunks,
            "pending_chunks": total_chunks - translated_chunks,
        },
        "pending_chapters": pending_chapters,
        "chapters": chapters_out,
        "next": nxt,
    }


# ── run log (qualitative beats the CLI can't see) ───────────────────────────

def log_event(project: str, *, event: str, data: str | None = None) -> dict:
    """Append a quality/friction beat to the central run log.

    The automatic per-command timeline (harness.py) covers durations and
    outcomes, but the conversational beats — approve-vs-reject, the chosen
    backend / spawn mode, worker re-spawns — live only in the chat. This is how
    the agent records them: ``event`` names the beat and ``data`` is a JSON
    object of fields merged into the logged record. Stamped with the project's
    current ``run_id`` so it groups with the rest of the run.
    """
    project_dir = state.resolve_project_dir(project)
    fields: dict = {}
    if data:
        parsed = json.loads(data)  # bad JSON -> ValueError -> CLI re-run message
        if not isinstance(parsed, dict):
            raise ValueError("--data must be a JSON object, e.g. '{\"beat\":\"glossary\"}'")
        fields = parsed

    run_id = state.ensure_run_id(project_dir)

    from src.utils.run_logger import log_run_event

    log_run_event(run_id=run_id, project=project_dir.name, event=event, **fields)
    return {"logged": True, "run_id": run_id, "event": event, "fields": fields}


def runs(project: str, *, run_id: str | None = None) -> dict:
    """Summarize one harness run from ``logs/harness_runs.jsonl`` (read-only).

    ``log_event`` / the automatic per-command timeline write the run log but nothing
    read it back. This turns it into a compact retro for a single run: the command
    timeline (with durations + outcomes), the qualitative beats (approval / backend /
    spawn_mode / respawn), and outcome tallies. Defaults to the project's MOST RECENT
    run; pass ``run_id`` for a specific one. ``available_run_ids`` lists the rest.
    """
    from collections import Counter

    project_dir = state.resolve_project_dir(project)
    slug = project_dir.name

    from src.utils.run_logger import read_run_events

    all_events = read_run_events(project=slug)
    available = sorted({e.get("run_id") for e in all_events if e.get("run_id")})
    if not all_events:
        return {"project": slug, "run_id": None, "available_run_ids": [],
                "note": "no run-log events for this project yet"}

    if run_id is None:
        run_id = all_events[-1].get("run_id")  # latest event's run = most recent run
    run_events = [e for e in all_events if e.get("run_id") == run_id]

    commands = [e for e in run_events if e.get("event") == "command"]
    beats = [e for e in run_events if e.get("event") != "command"]

    timeline = [
        {"cmd": e.get("cmd"), "status": e.get("status"),
         "dur_s": e.get("dur_s"), "ts": e.get("ts")}
        for e in commands
    ]
    # Beats keep their own fields, minus the keys already on every record.
    beat_summary = [
        {k: v for k, v in e.items() if k not in ("run_id", "project")}
        for e in beats
    ]

    return {
        "project": slug,
        "run_id": run_id,
        "available_run_ids": available,
        "command_count": len(commands),
        "beat_count": len(beats),
        "total_command_seconds": round(
            sum(e.get("dur_s") or 0 for e in commands), 3
        ),
        "status_counts": dict(Counter(e.get("status", "?") for e in commands)),
        "timeline": timeline,
        "beats": beat_summary,
    }

# ── output schemas (self-documenting last_output.json) ────────────────────────
#
# Friction-log #19: the JSON each command writes to ``.harness/last_output.json``
# had undocumented keys, so the agent guessed wrong field names and burned
# round-trips introspecting with ``python -c``. ``harness.py`` stamps the matching
# entry below into every artifact under ``_schema`` (and into the printed JSON), so
# the keys are self-describing at the point of use. Keep this in sync with the
# return dicts above; ``tests/test_harness_pipeline.py`` guards the core commands.
#
# Each value maps a top-level result key to a one-line description; nested shapes
# are noted inline in the description text. Commands with sub-actions are keyed
# ``"<command> <action>"`` (e.g. ``"glossary commit"``).
OUTPUT_SCHEMAS: dict[str, dict[str, str]] = {
    "setup": {
        "project_dir": "absolute path to the project directory",
        "config": "persisted config dict (target_language, locale, provider, model, title, author, language_code, always_include_dialogue)",
        "chapters": "list of written chapter file stems (e.g. 'chapter_01')",
        "chapter_count": "number of sections written to chapters/",
        "pattern_used": "the chapter pattern the split actually ran on (an 'auto' request is resolved to a concrete pattern here)",
        "dropped": "list of {label, reason} for boilerplate stripped at split (Contents, Title Page, ...)",
        "source_words": "word count of the ingested source (null on the no-URL path)",
        "suggested_pattern": "best-fit chapter pattern detected from the text (or the HTML on the URL path); compare to pattern_used to catch a wrong pick",
        "chapter_report": "per-chapter {number, heading, words, chunks} report; now populated on the local source.txt path too",
        "warnings": "advisory strings when the split looks wrong (e.g. 1 chapter for a large source); empty when clean",
        "chunks_dir_exists": "whether chunks/ exists yet (expected False right after setup)",
        "next": "the next command to run",
    },
    "split-preview": {
        "project_dir": "absolute path to the project directory",
        "section_count": "number of detected sections",
        "counts": "dict of section counts by kind: {front_matter, chapter, back_matter}",
        "pattern_used": "the chapter pattern resolved for this preview (an 'auto' request becomes a concrete pattern)",
        "suggested_pattern": "best-fit chapter pattern detected from the text, or null",
        "sections": "list of {name, kind, label, number, words, preview}",
        "dropped": "list of {label, reason} for boilerplate stripped (Contents, Title Page, ...)",
        "warnings": "advisory strings when the split looks wrong; empty when clean",
        "files_written": "always False for the dry-run preview",
    },
    "split": {
        "project_dir": "absolute path to the project directory",
        "chapter_count": "number of sections written",
        "counts": "dict of section counts by kind: {front_matter, chapter, back_matter}",
        "pattern_used": "the chapter pattern the split actually ran on (an 'auto' request is resolved here)",
        "suggested_pattern": "best-fit chapter pattern detected from the text, or null",
        "chapters": "list of written chapter file stems",
        "dropped": "list of {label, reason} for boilerplate stripped (Contents, Title Page, ...)",
        "warnings": "advisory strings when the split looks wrong; empty when clean",
        "files_written": "always True for the apply",
        "sections": "list of {name, kind, label, number, words}",
    },
    "style-guide prepare-questions": {
        "detected_features": "list of source features that triggered conditional questions",
        "questions": "list of {id, question, options:[{id,label}], hint, [prefilled, prefilled_reason]}",
        "answers_path": "path to write the {question_id: answer} JSON to",
        "instructions": "what to do next (ask the user, then write answers_path)",
    },
    "style-guide prepare-followups": {
        "prompt_path": "path to the follow-up-question prompt to read",
        "draft_path": "path to write the drafted follow-up questions JSON to",
        "instructions": "what to do next",
    },
    "style-guide commit-followups": {
        "new_questions": "list of merged-in follow-ups: {id, question, options:[{id,label}]}",
        "answers_path": "path to rewrite with the FULL answer set",
        "instructions": "what to do next",
    },
    "style-guide prepare-draft": {
        "prompt_path": "path to the style-guide prompt to read",
        "draft_path": "path to write the drafted style-guide prose to",
        "resolved_answers": "list of {id, question, answer, source} ('option' vs 'custom')",
        "unanswered": "list of question ids with no answer",
        "instructions": "what to do next",
    },
    "style-guide commit": {
        "style_path": "path to the written style.json",
        "chars": "character length of the committed style guide",
    },
    "glossary prepare": {
        "prompt_path": "path to the glossary-proposal prompt to read",
        "candidate_count": "number of extracted candidate terms",
        "style_guide_loaded": "whether style.json was fed into the prompt",
        "draft_path": "path to write the drafted proposals JSON to",
        "instructions": "what to do next",
    },
    "glossary commit": {
        "glossary_path": "path to the written glossary.json",
        "term_count": "number of committed terms",
        "warnings": "advisory, non-blocking smells to surface at the approval gate (e.g. accent-stripping); empty when clean",
        "terms": "list of {english, translation, type, context}",
    },
    "difficulty": {
        "book_difficulty": "overall difficulty score (0-1, rounded)",
        "length_score": "length component of difficulty (rounded)",
        "rarity_score": "rare-word component of difficulty (rounded)",
        "dialect_score": "dialect component of difficulty (rounded)",
        "dialogue_score": "nested-dialogue component of difficulty (rounded)",
        "verse_score": "verse/poetry component of difficulty (rounded)",
        "suggested_target_size": "recommended whole-book chunk size in words",
        "wordfreq_available": "whether the wordfreq rarity model was available",
        "chapters": "list of {chapter_id, difficulty, dialogue_score, verse_score, suggested_target_size}",
        "next": "the next command to run",
    },
    "translate-prepare": {
        "manifest": "list of work entries: {chunk_id, chapter_id, chunk_path, prompt_path, draft_path, source_word_count}",
        "manifest_path": "path to the written manifest.json",
        "worker_model": "model each worker should be pinned to",
        "spawn_plan": "dict {parallelism, window, batch_size}",
        "spawn_mode_moot": "True when every in-scope chapter is a single chunk (skip the spawn-mode question)",
        "usage_summary": "dict {chunks, source_words, worker_model, parallelism, window, batch_size, spawn_mode_moot}",
        "rescued_prior_drafts": "count of uncommitted drafts carried over from a prior prepare or discovered on disk (even out of the current --chapters scope)",
        "chapters": "the --chapters scope echoed back ('all' when omitted)",
        "instructions": "what to do next (spawn workers, then translate-commit)",
        "error": "present only on failure (e.g. no chunks, bad --chapters)",
        "note": "present when no chapters matched the requested scope",
        "available_chapters": "present with note: chapter ids that do exist",
    },
    "translate-commit": {
        "committed": "list of chunk_ids stamped this run",
        "failed": "list of {chunk_id, problems} that did not pass the guard",
        "missing": "list of chunk_ids whose draft file was absent",
        "skipped_already_translated": "list of chunk_ids already translated (idempotent skip)",
        "waived": "map of chunk_id -> waived guard problems (via --allow-problem)",
        "counts": "dict {committed, failed, missing, skipped}",
        "instructions": "what to do next (re-spawn failed/missing, or proceed)",
        "error": "present only on failure (e.g. no manifest)",
    },
    "chunk": {
        "command": "the harness command ('chunk')",
        "exit_code": "wrapped-script exit code (0 = ok)",
        "stage": "'cost-estimate' (chunk runs chunking then halts at the estimate)",
        "chunks_needing_translation": "number of untranslated chunks in scope",
        "total_chunks_in_scope": "total chunks produced (the chunk count)",
        "input_tokens": "estimated input tokens for the metered API",
        "api_cost_usd": "conditional metered-API cost (subagent backend is free)",
        "provider": "provider used for the estimate",
        "model": "model used for the estimate",
        "cost_only": "always True — this command never spends",
    },
    "cost": {
        "command": "the harness command ('cost')",
        "exit_code": "wrapped-script exit code (0 = ok)",
        "stage": "'cost-estimate'",
        "chunks_needing_translation": "number of untranslated chunks in scope",
        "total_chunks_in_scope": "total chunks in scope",
        "input_tokens": "estimated input tokens for the metered API",
        "api_cost_usd": "conditional metered-API cost (subagent backend is free)",
        "provider": "provider used for the estimate",
        "model": "model used for the estimate",
        "cost_only": "always True — this command never spends",
    },
    "translate": {
        "command": "the harness command ('translate')",
        "exit_code": "wrapped-script exit code (0 = ok; non-zero = refused/aborted)",
        "stage": "'translate'",
        "translated": "number of chunks translated this run",
        "chapters_done": "list of chapter_ids fully covered this run",
        "estimated_cost_usd": "estimated spend for this batch",
        "remaining_untranslated": "untranslated chunks left in the book after this batch",
        "note": "present when nothing was translated (already done / no match)",
    },
    "epub": {
        "command": "the harness command ('epub')",
        "exit_code": "wrapped-script exit code (0 = ok)",
        "stage": "'epub'",
        "path": "path to the built .epub",
        "size_kb": "EPUB size in KB",
        "included": "list of translated chapter_ids included",
        "skipped": "list of untranslated/partial chapter_ids skipped",
    },
    "align": {
        "aligned": "list of {chapter_id, es_count, high_confidence_pct}",
        "skipped": "list of {chapter_id, reason} not aligned",
        "alignments_dir": "directory the alignment JSON was written to",
        "reader_base": "base reader URL for this project",
        "reader_first": "reader URL for the first newly-aligned chapter, or null",
        "reader_links": "reader URLs for every aligned chapter",
        "instructions": "what to do next",
        "error": "present only on a per-chapter aligner failure",
    },
    "show-translation": {
        "chapters": "list of {chapter_id, total_chunks, translated_chunks, chunks:[{id, position, status, has_translation, source_words, translation_words, translated_text, source_text}]}",
        "available_chapters": "all chapter ids discovered",
        "chunks_dir": "path to chunks/",
        "shown_chunks": "number of chunk rows returned (capped by --max-chunks)",
        "total_chunks": "total chunks in scope",
        "truncated": "True when --max-chunks capped the sample",
        "fields": "key aliases: {translation: 'translated_text', source: 'source_text'}",
    },
    "status": {
        "project": "project slug",
        "artifacts": "dict of which pipeline artifacts exist (source, chapters, style_guide, glossary, difficulty, chunks)",
        "epubs": "list of built .epub filenames",
        "spawn_plan": "dict {parallelism, window, batch_size}",
        "worker_model": "configured worker model",
        "run_id": "current run id",
        "stage": "one-word progress: pre-chunk | untranslated | partial | fully-translated",
        "spawn_mode_moot": "True if one chunk per chapter (else False; null pre-chunk)",
        "totals": "dict {chapters, complete_chapters, total_chunks, translated_chunks, pending_chunks}",
        "pending_chapters": "list of chapter_ids not yet fully translated",
        "chapters": "list of {chapter_id, chunks, translated, complete}",
        "next": "suggested next command",
    },
    "runs": {
        "project": "project slug",
        "run_id": "the summarized run id (most recent unless one was requested)",
        "available_run_ids": "all run ids logged for this project",
        "command_count": "number of command events in the run",
        "beat_count": "number of qualitative beats in the run",
        "total_command_seconds": "summed command durations",
        "status_counts": "dict of command status -> count",
        "timeline": "list of {cmd, status, dur_s, ts}",
        "beats": "list of logged qualitative beats (minus run_id/project)",
        "note": "present when there are no events yet",
    },
    "log-event": {
        "logged": "always True on success",
        "run_id": "the run id the beat was stamped with",
        "event": "the beat name",
        "fields": "the extra fields merged into the logged record",
    },
}


def schema_for(command: str, action: str | None = None) -> dict | None:
    """Return the documented output schema for a command (friction-log #19).

    Sub-action commands (``style-guide``/``glossary``) are keyed ``"<command>
    <action>"``; falls back to the bare command name.
    """
    if action:
        keyed = OUTPUT_SCHEMAS.get(f"{command} {action}")
        if keyed is not None:
            return keyed
    return OUTPUT_SCHEMAS.get(command)
