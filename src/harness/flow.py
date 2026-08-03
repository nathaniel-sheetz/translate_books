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
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from src.harness import state

_log = logging.getLogger(__name__)

# Valid ``GlossaryTermType`` values, for normalizing an agent-supplied type_guess.
_GLOSSARY_TYPES: frozenset[str] = frozenset(
    {"character", "place", "concept", "technical", "other"}
)

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
    is always-on (nothing to toggle) → ``False``. A full Claude model id (non-alias)
    falls back to the API-path support check. Cursor / non-Claude ids (``grok-*``,
    ``auto``, ``gpt-*``, ``composer-*``, …) never get the Claude thinking keyword.
    """
    if not worker_model:
        return False
    alias = worker_model.strip().lower()
    if alias in _THINKING_WORKER_ALIASES:
        return True
    if alias == "fable":
        return False
    # Cursor headless models and other non-Claude ids: never inject "think hard".
    if not alias.startswith("claude-"):
        return False
    from src.api_translator import model_supports_thinking
    return model_supports_thinking(worker_model)


def _warn_cursor_claude_model(cli: str, worker_model: str) -> str | None:
    """Return a warning when cursor is paired with a Claude-looking worker_model."""
    from src.harness.headless import warn_cursor_claude_model
    return warn_cursor_claude_model(cli, worker_model)


# ── helpers ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _quiet_stdout():
    """Route a helper's chatty ``print``s to stderr so stdout stays clean JSON."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# A wrapped script's stage failure, e.g. "  ERROR in translate: Template file not
# found: prompts/translation.txt" (scripts/translate_book.py). Captured off the stdout
# stream so the diagnosis reaches last_output.json's ``error`` (friction-log #6 — the
# artifact recorded exit_code 1 with error: null, and the only readable cause was in
# stdout, which the skill tells the agent never to parse).
_SCRIPT_ERROR_RE = re.compile(r"^\s*ERROR\b[^:]*:\s*(?P<msg>.+?)\s*$")


def _run_script(cmd: list[str]) -> tuple[int, dict | None, str | None]:
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
    Also retain the last ``ERROR ...:`` line seen so a failure carries a readable
    cause (friction-log #6). Returns ``(returncode, summary | None, error | None)``.
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
    last_error: str | None = None
    if proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith(state.HARNESS_RESULT_PREFIX):
                payload = line[len(state.HARNESS_RESULT_PREFIX):].strip()
                try:
                    summary = json.loads(payload)
                except json.JSONDecodeError:
                    summary = None  # malformed sentinel -> fall back to a minimal result
                continue  # machine-only line: never echo to the human stream
            match = _SCRIPT_ERROR_RE.match(line)
            if match:
                last_error = match.group("msg")
            print(line, end="")
    rc = proc.wait()
    return rc, summary, last_error


def _stream_result(
    command: str, rc: int, summary: dict | None, error: str | None = None
) -> dict:
    """Shape a streaming command's return: a fresh dict carrying the exit code.

    Always returns a dict (never a bare int) so ``main()`` writes a fresh
    ``last_output.json`` for this command — closing the stale-artifact trap
    (friction-log #18) even when the wrapped script emitted no sentinel. ``main()``
    propagates ``exit_code`` as the process exit status.

    On a non-zero exit, fill ``error`` from the wrapped script's last ``ERROR`` line
    so the documented "read the artifact, never parse stdout" contract actually yields
    a diagnosis (friction-log #6). A sentinel-supplied ``error`` is more specific, so
    it wins; the scraped line is only a fallback.
    """
    result = dict(summary) if summary else {}
    # Harness-authoritative keys win: a wrapped script's sentinel must never
    # overwrite the command name or the real process exit code we recorded.
    result["command"] = command
    result["exit_code"] = rc
    if rc != 0 and error and not result.get("error"):
        result["error"] = error
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
    always_include_image_instructions: bool | None = None,
    front_matter_titles: list[str] | None = None,
    back_matter_titles: list[str] | None = None,
    min_chapter_size: int | None = None,
    auto_strip_boilerplate: bool = True,
    footnotes: str = "import",
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
        "always_include_image_instructions": always_include_image_instructions,
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
        # 'import' (default) captures Gutenberg footnotes as [FOOTNOTE:N] tokens +
        # footnotes.json so the skill can offer keep/drop at Step 0; a harmless no-op
        # when the source has none (or on the local source.txt path, which skips
        # detection). Without this field stage_ingest defaulted to 'drop' (friction #2).
        footnotes=footnotes,
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
        "footnotes_detected": pstate.get("footnote_count", 0),  # notes found at ingest
        "footnotes_mode": pstate.get("footnote_mode"),  # 'import' | 'drop' | None
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

    # Fold in the approved address map's condensed summary, when the address-map
    # beat ran first. That summary was written against the whole-book dialogue
    # sample and already approved, so the style guide reproduces it rather than
    # re-deriving forms of address from the one questionnaire answer.
    address_summary = ""
    address_map_path = project_dir / "address_map.json"
    if address_map_path.exists():
        from src.utils.file_io import load_address_map
        try:
            address_summary = (load_address_map(address_map_path).style_guide_summary or "").strip()
        except (OSError, ValueError) as exc:
            _log.warning("Could not read style_guide_summary from %s: %s", address_map_path, exc)

    prompt = build_style_guide_prompt(
        allq, ans, source, cfg["target_language"], cfg["locale"],
        address_summary=address_summary,
    )
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
        "carryforward_path": str(hdir / "glossary_carryforward.json"),
        "resolved_answers": resolved,
        "unanswered": unanswered,
        "address_summary_loaded": bool(address_summary),
        "instructions": (
            "Check resolved_answers (each should read 'option' unless you meant a custom "
            "answer; 'custom' on a question you tried to answer by id means a typo). Then read "
            "prompt_path, draft the style-guide prose to draft_path, refine it with the user, "
            "then run `style-guide commit`. Put NO term→translation pairs in the guide: write "
            "any term that needs a fixed translation to carryforward_path as a JSON array of "
            "{term, why, type_guess} and the glossary beat will pick it up."
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

def _glossary_carryforward(hdir: Path, existing_terms: set[str]) -> tuple[list[dict], str]:
    """Read terms the style-guide beat handed forward to the glossary.

    A style guide states rules, not term→translation pairs — so when style-guide
    drafting surfaces a term that needs a fixed translation, it writes it to
    ``.harness/glossary_carryforward.json`` as ``[{term, why, type_guess}]``
    instead of defining it in the guide. Those terms are re-injected here as
    candidates (extraction ranks by frequency and can bury a rare-but-critical
    term) and their rationale becomes the prompt's guidance block.

    Returns ``(candidates, guidance)``; both empty when the file is absent or
    unreadable — a malformed hand-off must not take down the glossary beat.
    """
    path = hdir / "glossary_carryforward.json"
    if not path.exists():
        return [], ""
    try:
        entries = json.loads(_read(path))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Ignoring unreadable glossary carry-forward at %s: %s", path, exc)
        return [], ""
    if not isinstance(entries, list):
        _log.warning("Glossary carry-forward at %s is not a JSON array; ignoring.", path)
        return [], ""

    candidates: list[dict] = []
    notes: list[str] = []
    seen = {t.casefold() for t in existing_terms}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or "").strip()
        if not term:
            continue
        why = str(entry.get("why") or "").strip()
        if why:
            notes.append(f"- {term}: {why}")
        if term.casefold() in seen:
            continue  # extraction already found it; the note still applies
        seen.add(term.casefold())
        type_guess = str(entry.get("type_guess") or "other").strip().lower()
        candidates.append({
            "term": term,
            "type_guess": type_guess if type_guess in _GLOSSARY_TYPES else "other",
            "frequency": 0,
            "score": 0.0,
            "context_sentence": why,
            "detection_reasons": ["style_guide_carryforward"],
        })

    guidance = ""
    if notes:
        guidance = (
            "These terms were surfaced while drafting the style guide, which states rules "
            "rather than term pairs. Define each one here:\n" + "\n".join(notes)
        )
    return candidates, guidance


def glossary_prepare(project: str, *, max_candidates: int = 500) -> dict:
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
        report = extract_candidates(source, max_candidates=max_candidates, verbose=False)
    candidates = [c.model_dump() for c in report.candidates]
    sample = load_source_sample(project_dir)
    style_path = project_dir / "style.json"
    style_guide = _read(style_path) if style_path.exists() else ""

    # Terms the style-guide beat surfaced but (correctly) refused to define there:
    # a style guide states rules, not term→translation pairs. Inject them as
    # candidates so extraction cannot bury them, and pass their rationale through
    # the prompt's guidance slot.
    carryforward, guidance = _glossary_carryforward(hdir, {c["term"] for c in candidates})
    candidates.extend(carryforward)

    prompt = build_glossary_prompt(
        candidates, sample, style_guide, cfg["target_language"], glossary_guidance=guidance
    )
    prompt_path = hdir / "glossary_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return {
        "prompt_path": str(prompt_path),
        "candidate_count": len(candidates),
        "carryforward_count": len(carryforward),
        "source_kind": source_kind,  # "chunks"/"chapters" => front matter excluded; "source" => not
        "style_guide_loaded": bool(style_guide),
        "draft_path": str(hdir / "glossary_draft.json"),
        "instructions": (
            "Read prompt_path, draft proposals as a JSON array of "
            "{english, translation, type, context, alternatives} to draft_path (tracking any "
            "uncertain renderings to surface at the approval gate), then run `glossary commit`. "
            "Note the alternatives conventions in the prompt: places and bare personal names "
            "take none; a title + name leads with the narration form including the article."
        ),
    }


def glossary_commit(project: str, *, draft: str | None = None) -> dict:
    """Guard, build, save, and validate the agent-drafted glossary -> glossary.json."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.harness_guard import (
        address_map_name_warnings,
        diacritic_warning,
        glossary_convention_warnings,
        guard_glossary_proposals,
        validate_glossary_file,
    )
    from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
    from src.utils.file_io import save_glossary

    cfg = state.load_config(project_dir)
    draft_path = Path(draft) if draft else hdir / "glossary_draft.json"
    proposals = json.loads(_read(draft_path))
    guard_glossary_proposals(proposals)  # raises HarnessValidationError -> re-draft
    # Soft, non-blocking smell-check for an accent-stripped draft (see #21): the structural
    # guard above passes pure ASCII, so surface the warning for the agent + approval gate.
    warn = diacritic_warning(proposals, cfg.get("language_code"))
    warnings: list[str] = [warn] if warn else []
    # Convention flags (REVIEW:) — human judgement calls, not draft bugs.
    warnings.extend(glossary_convention_warnings(proposals))
    glossary = proposals_to_glossary(glossary_terms_from_proposals(proposals))
    out = project_dir / "glossary.json"
    save_glossary(glossary, out)
    validate_glossary_file(out)  # belt-and-suspenders

    # The address map is drafted before the glossary and carries English cast
    # names; now that the target-language forms are approved, flag the drift.
    address_map_path = project_dir / "address_map.json"
    if address_map_path.exists():
        from src.utils.file_io import load_address_map
        try:
            warnings.extend(address_map_name_warnings(glossary, load_address_map(address_map_path)))
        except (OSError, ValueError) as exc:
            _log.warning("Could not check address map names at %s: %s", address_map_path, exc)

    return {
        "glossary_path": str(out),
        "term_count": len(glossary.terms),
        "warnings": warnings,
        "terms": [
            {"english": t.english, "translation": t.spanish, "type": t.type, "context": t.context}
            for t in glossary.terms
        ],
    }


# ── address-map beat (forms-of-address expectations for the address judge) ──

# Per-chapter source cap fed to the drafting prompt, to bound cost/size while
# still giving the LLM enough dialogue to read the relationships.
_ADDRESS_SAMPLE_CHAR_CAP = 6000


def _format_characters_block(project_dir: Path) -> tuple[str, int]:
    """Render the glossary's character terms for the address-map prompt."""
    from src.utils.file_io import load_glossary

    # The address map is the FIRST beat, so a glossary usually does not exist yet.
    # Be explicit that the cast must then be named with the English source names
    # verbatim: a guessed target-language form the glossary later contradicts is
    # worse than an honest English one, and `address_map_name_warnings` can only
    # detect the drift if the names are predictable.
    no_cast = (
        "(no approved cast list yet — infer the cast from the sample chapters and use the "
        "ENGLISH source names EXACTLY as they appear there. Do NOT guess target-language "
        "forms; the glossary has not fixed them yet.)"
    )
    glossary_path = project_dir / "glossary.json"
    if not glossary_path.exists():
        return no_cast, 0
    glossary = load_glossary(glossary_path)
    chars = [t for t in glossary.terms if getattr(t.type, "value", t.type) == "character"]
    if not chars:
        return no_cast, 0
    lines = []
    for t in chars:
        note = f" — {t.context}" if t.context else ""
        lines.append(f"- {t.spanish} (English: {t.english}){note}")
    return "\n".join(lines), len(chars)


def _format_sample_chapters_block(project_dir: Path, scores: list) -> str:
    """Render the sampled chapters (source text, capped) for the prompt."""
    from src.utils.source_text import load_chapter_source_text

    blocks = []
    for s in scores:
        text, _mtime, _kind = load_chapter_source_text(project_dir, s.chapter_id)
        text = text.strip()
        if len(text) > _ADDRESS_SAMPLE_CHAR_CAP:
            text = text[:_ADDRESS_SAMPLE_CHAR_CAP] + "\n[... chapter truncated for sampling ...]"
        blocks.append(f"=== {s.chapter_id} (dialogue density {s.density:.1f}/1k words) ===\n{text}")
    return "\n\n".join(blocks)


def _forms_of_address_answer(project_dir: Path) -> str:
    """Resolve the user's answered ``forms_of_address`` preference, if any.

    The address-map beat runs before the style guide, and asks the single
    ``forms_of_address`` question up front so the map is drafted against the
    register the user actually wants. That answer lands in the normal style-guide
    answers file, so read it from there rather than inventing a second store.
    """
    hdir = state.harness_dir(project_dir)
    questions_path = hdir / "style_questions.json"
    answers_path = hdir / "style_answers.json"
    if not (questions_path.exists() and answers_path.exists()):
        return ""
    try:
        allq = json.loads(_read(questions_path))
        ans = json.loads(_read(answers_path))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(ans, dict) or "forms_of_address" not in ans:
        return ""

    from src.style_guide_wizard import resolve_answer

    question = next((q for q in allq if q.get("id") == "forms_of_address"), None)
    if question is None:
        return ""
    label, effect, _matched = resolve_answer(question, ans["forms_of_address"])
    return (effect or label or "").strip()


def address_map_precheck(project: str) -> dict:
    """Report whether this book has enough dialogue to warrant an address map.

    A forms-of-address map describes how characters address *each other*; a book
    with no interpersonal dialogue has nothing for it to say and nothing for the
    later ``address`` judge to check. Records ``address_map_decision="no_dialogue"``
    when the gate fails, so the router stops offering the beat on every resume.
    Read-only otherwise — offering and declining stay the skill's job.
    """
    project_dir = state.resolve_project_dir(project)
    state.ensure_harness_dir(project_dir)

    from src.harness.address_sample import dialogue_precheck

    result = dialogue_precheck(project_dir)
    if not result["chapters_scored"]:
        raise FileNotFoundError(
            "No chapter source text found to score for dialogue. Run `setup` + `split` first."
        )

    if result["dialogue_present"]:
        result["recommendation"] = (
            f"Offer the address map: {result['qualifying_chapters']} of "
            f"{result['chapters_scored']} chapters carry real back-and-forth dialogue."
        )
    else:
        cfg = state.load_config(project_dir)
        cfg["address_map_decision"] = "no_dialogue"
        state.save_config(project_dir, cfg)
        result["recommendation"] = (
            f"SKIP the address map — no chapter clears {result['min_turns']} quoted turns, so "
            "this book has no interpersonal dialogue for a usted/tú map to describe. Recorded "
            "as 'no_dialogue'; do not offer the beat. Continue to references/style-guide.md, "
            "where forms_of_address is just one of the standard questions."
        )
    result["address_map_decision"] = state.load_config(project_dir).get("address_map_decision")
    return result


def address_map_skip(project: str) -> dict:
    """Record that the user declined the address map. Never blocks translation."""
    project_dir = state.resolve_project_dir(project)
    state.ensure_harness_dir(project_dir)
    cfg = state.load_config(project_dir)
    cfg["address_map_decision"] = "skipped"
    state.save_config(project_dir, cfg)
    return {
        "address_map_decision": "skipped",
        "instructions": (
            "Address map declined. Continue to references/style-guide.md and ask "
            "forms_of_address as one of the standard questions. The map can be built "
            "later, here or from judge-review's setup precheck."
        ),
    }


def address_map_prepare(project: str, *, max_chapters: int = 6) -> dict:
    """Build the address-map drafting prompt (you are the LLM that drafts the map).

    Samples the book's highest interpersonal-dialogue chapters (a spread across
    beginning/middle/end), plus the glossary's cast and the style guide, and
    renders the prompt for the agent to draft ``address_map.json``.
    """
    project_dir = state.resolve_project_dir(project)
    hdir = state.ensure_harness_dir(project_dir)
    cfg = state.load_config(project_dir)

    from src.harness.address_sample import select_address_sample_chapters

    scores = select_address_sample_chapters(project_dir, max_chapters=max_chapters)
    if not scores:
        raise FileNotFoundError(
            "No chapter source text found to sample for the address map. "
            "Run `setup` + `split` first."
        )

    characters, char_count = _format_characters_block(project_dir)
    style_path = project_dir / "style.json"
    style_guide = ""
    if style_path.exists():
        from src.utils.file_io import load_style_guide
        style_guide = load_style_guide(style_path).content
    sample_block = _format_sample_chapters_block(project_dir, scores)
    forms_of_address = _forms_of_address_answer(project_dir)

    template = _read(state.REPO_ROOT / "prompts" / "address_map_generate.txt")
    prompt = (
        template
        .replace("{{target_language}}", cfg["target_language"])
        .replace("{{locale}}", cfg["locale"])
        .replace("{{characters}}", characters)
        .replace("{{style_guide}}", style_guide or "(no style guide yet)")
        .replace("{{forms_of_address}}", forms_of_address
                 or "(not answered — infer the register from the sample chapters)")
        .replace("{{sample_chapters}}", sample_block)
    )
    prompt_path = hdir / "address_map_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    return {
        "prompt_path": str(prompt_path),
        "draft_path": str(hdir / "address_map_draft.json"),
        "sample_chapters": [s.to_dict() for s in scores],
        "characters_loaded": char_count,
        "style_guide_loaded": bool(style_guide),
        "forms_of_address_loaded": bool(forms_of_address),
        "instructions": (
            "Read prompt_path, draft the forms-of-address map as a JSON object "
            "{content, style_guide_summary, pairs, global_rules} to draft_path (each "
            "non-empty direction must include a when='default' rule as the last entry; "
            "put specific when-rules before it), refine it with the user, then run "
            "`address-map commit`. `style_guide_summary` is read by a translator who "
            "sees ONE chunk: general rules plus high-frequency exceptions only, no "
            "chapter references, no full pair list."
        ),
    }


def address_map_commit(project: str, *, draft: str | None = None) -> dict:
    """Parse, save, and validate the agent-drafted address map -> address_map.json."""
    project_dir = state.resolve_project_dir(project)
    hdir = state.harness_dir(project_dir)

    from src.harness_guard import HarnessValidationError, validate_address_map_file
    from src.models import AddressMap
    from src.utils.file_io import save_address_map

    draft_path = Path(draft) if draft else hdir / "address_map_draft.json"
    try:
        data = json.loads(_read(draft_path))
    except (json.JSONDecodeError, FileNotFoundError, OSError) as exc:
        raise HarnessValidationError(
            f"Address map draft at {draft_path} is not readable JSON: {exc}\n"
            "Re-draft it as a JSON object {content, pairs, global_rules}."
        ) from exc
    try:
        address_map = AddressMap.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise HarnessValidationError(
            f"Address map draft failed validation: {exc}\n"
            "Fix the schema (form is 'tú'/'usted'; each non-empty direction must end "
            "with exactly one when='default' rule) and re-draft."
        ) from exc

    if not (address_map.content or "").strip():
        raise HarnessValidationError(
            "Address map draft has empty `content`. The address judge reads that "
            "prose — fill it with the forms-of-address expectations (pairs alone "
            "are not enough) and re-draft."
        )

    out = project_dir / "address_map.json"
    save_address_map(address_map, out)
    validate_address_map_file(out)  # belt-and-suspenders

    cfg = state.load_config(project_dir)
    cfg["address_map_decision"] = "built"
    state.save_config(project_dir, cfg)

    summary = (address_map.style_guide_summary or "").strip()
    warnings: list[str] = []
    if not summary:
        warnings.append(
            "No `style_guide_summary` in the draft. The style-guide beat injects that field "
            "as its FORMS OF ADDRESS section; without it the guide falls back to the "
            "questionnaire answer alone and the map informs nothing. Add it and re-commit."
        )
    return {
        "address_map_path": str(out),
        "pair_count": len(address_map.pairs),
        "has_content": bool(address_map.content.strip()),
        "chars": len(address_map.content),
        "style_guide_summary": summary,
        "warnings": warnings,
        "address_map_decision": "built",
        "pairs": [
            {"a": p.a, "b": p.b, "relationship": p.relationship,
             "directions": {k: [r.model_dump(exclude_none=True) for r in v]
                            for k, v in p.directions.items()}}
            for p in address_map.pairs
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
#        (no spend)        (+ optional shared preamble.txt + per-chunk .body.txt
#                          for the headless ``translate-fanout`` path)
#                          Task workers: read prompt_path -> write draft_path
#                          Headless:     CLI -p body + --system-prompt-file preamble
#                                        (claude; cursor folds preamble into stdin)
#   translate-fanout  ─► run one wave of headless CLI workers (claude|cursor; opt-in)
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

    Also emits a cacheable split for the headless fan-out path:
    ``.harness/translate/preamble.txt`` (shared prefix from
    ``build_translation_prompt_parts``) and ``.harness/translate/<id>.body.txt``
    (per-chunk suffix). Manifest entries get ``preamble_path`` + ``body_path``
    only when that chunk's prefix is **byte-identical** to the shared preamble
    (computed from the first non-empty prefix). The conditional ``dialogue_instructions`` /
    ``image_placeholder_instructions`` live in the prefix — they are only constant
    when the book sets ``always_include_dialogue`` /
    ``always_include_image_instructions`` (passed here as ``always_include_dialogue``
    + ``book_has_images``). On any prefix mismatch the entry omits those paths so
    fan-out falls back to the full ``prompt.txt`` with no ``--system-prompt-file``
    (correct, just uncached). ``preamble + body`` is always byte-identical to
    ``prompt.txt`` when both split files are written.

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
    from src.api_translator import build_translation_prompt_parts
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
    # a per-book config flag; always_include_image_instructions is optional (None =
    # auto from whole-book image presence) so the constant is stable regardless of
    # which wave runs.
    always_include_dialogue = bool(cfg.get("always_include_dialogue", False))
    cfg_images = cfg.get("always_include_image_instructions")
    if cfg_images is None:
        book_has_images = any(
            image_filenames(load_chunk(cp).source_text)
            for cps in all_discovered.values()
            for cp in cps
        )
    else:
        book_has_images = bool(cfg_images)

    translate_dir = hdir / "translate"
    translate_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    total_words = 0
    # Shared cacheable prefix for headless fan-out. Written once from the first
    # chunk that yields a non-empty prefix; later chunks must match byte-for-byte
    # or they fall back to the full prompt.txt (no --system-prompt-file).
    shared_preamble: str | None = None
    preamble_path = translate_dir / "preamble.txt"
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
            prefix, suffix = build_translation_prompt_parts(
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
            prompt = prefix + suffix
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
            entry: dict = {
                "chunk_id": chunk.id,
                "chapter_id": chunk.chapter_id,
                "chunk_path": str(cp),
                "prompt_path": str(prompt_path),
                "draft_path": str(draft_path),
                "source_word_count": chunk.word_count,
            }
            # Cache split: only when the prefix is non-empty and matches the shared
            # preamble (or establishes it). Mismatched prefixes omit the paths so
            # translate-fanout uses prompt.txt without --system-prompt-file.
            # Drop any stale .body.txt so a later rescue cannot reattach a mismatch.
            body_path = translate_dir / f"{chunk.id}.body.txt"
            if prefix:
                if shared_preamble is None:
                    shared_preamble = prefix
                    preamble_path.write_text(shared_preamble, encoding="utf-8")
                if prefix == shared_preamble:
                    body_path.write_text(suffix, encoding="utf-8")
                    entry["preamble_path"] = str(preamble_path)
                    entry["body_path"] = str(body_path)
                else:
                    body_path.unlink(missing_ok=True)
            else:
                body_path.unlink(missing_ok=True)
            entries.append(entry)
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
        rescued_entry: dict = {
            "chunk_id": cid,
            "chapter_id": rescue_chunk.chapter_id,
            "chunk_path": str(chunk_cp),
            "prompt_path": str(translate_dir / f"{cid}.prompt.txt"),
            "draft_path": str(draft),
            "source_word_count": rescue_chunk.word_count,
        }
        # Carry forward a prior cache split only when preamble+body still equals
        # prompt.txt — a later prepare may have rewritten the shared preamble.
        prior_preamble = prior_entry.get("preamble_path") if prior_entry else None
        prior_body = prior_entry.get("body_path") if prior_entry else None
        body_candidate = translate_dir / f"{cid}.body.txt"
        prompt_candidate = Path(rescued_entry["prompt_path"])
        if (
            prior_preamble
            and prior_body
            and _split_matches_prompt(
                Path(prior_preamble), Path(prior_body), prompt_candidate
            )
        ):
            rescued_entry["preamble_path"] = prior_preamble
            rescued_entry["body_path"] = prior_body
        elif (
            shared_preamble is not None
            and _split_matches_prompt(preamble_path, body_candidate, prompt_candidate)
        ):
            rescued_entry["preamble_path"] = str(preamble_path)
            rescued_entry["body_path"] = str(body_candidate)
        rescued.append(rescued_entry)

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
            "For each manifest entry, either (1) spawn a Task worker pinned to "
            "worker_model that reads prompt_path and writes ONLY the translated "
            "prose to draft_path, or (2) run `translate-fanout` for the headless "
            "CLI path (claude|cursor; uses preamble_path/body_path when present). Then run "
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
    # Chapters that gained a chunk this run — candidates for the post-loop recombine
    # that refreshes chapters/<id>.txt (see below).
    touched_chapters: set[str] = set()
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
        # From the loaded chunk, not entry["chapter_id"]: an older manifest may not
        # carry that key.
        touched_chapters.add(chunk.chapter_id)

    # Refresh chapters/<id>.txt for every chapter that became FULLY translated in this
    # run. Before this, the workers path never wrote that file at all (there was no
    # `combine` command), so it still held the English split output — and the web
    # reader derives paragraph breaks and [IMAGE:...] placement from it.
    #
    # Completeness is re-derived from chunks/ on disk, never from the manifest: a
    # manifest only carries chunks that NEEDED translation, so a chapter can complete
    # from one new commit plus three chunks skipped as already-translated above.
    recombined: list[str] = []
    combine_failed: list[dict] = []
    if touched_chapters:
        from scripts.translate_book import discover_chapters
        from src.corrections_apply import recombine_chapter

        discovered = discover_chapters(project_dir / "chunks")
        for chapter_id in sorted(touched_chapters):
            cps = discovered.get(chapter_id) or []
            try:
                if not cps or not all(load_chunk(cp).has_translation for cp in cps):
                    continue  # still partial — there is nothing coherent to stitch yet
                recombine_chapter(project_dir, chapter_id)
                recombined.append(chapter_id)
            except Exception as exc:  # noqa: BLE001 - never fail an authoritative commit
                # Deliberately NOT the evaluator's silent `pass` above: a failed
                # recombine leaves chapters/<id>.txt out of sync with prose the user is
                # about to read, and silent invisibility is the failure class this whole
                # seam exists to kill. Report it; the chunks are already stamped.
                combine_failed.append({"chapter_id": chapter_id,
                                       "error": f"{type(exc).__name__}: {exc}"[:500]})

    # A mixed run still recombines every chapter that completed, so the recombine
    # outcome belongs in BOTH branches — reporting it only on the clean path is how a
    # partial commit quietly leaves chapters/*.txt stale while the agent reads
    # "re-spawn the failures" and moves on.
    def _combine_note() -> str:
        note = ""
        if recombined:
            note += f" Refreshed chapters/*.txt for {len(recombined)} completed chapter(s)."
        if combine_failed:
            note += (
                f" WARNING: {len(combine_failed)} chapter(s) failed to recombine (see "
                f"`combine_failed`) — chapters/*.txt is stale for those; fix and run "
                f"`combine --chapters <ids>`."
            )
        return note

    if failed or missing:
        instructions = (
            "Re-spawn workers for any `failed` (fix per the named problems) and `missing` "
            "chunk_ids — write fresh prose to their draft_path — then run `translate-commit` "
            "again. Cap re-spawns ~3, then surface for manual edit."
        )
        instructions += _combine_note()
    else:
        instructions = "All in-scope chunks committed"
        instructions += (
            f"; refreshed chapters/*.txt for {len(recombined)} chapter(s)."
            if recombined else "."
        )
        if combine_failed:
            instructions += (
                f" WARNING: {len(combine_failed)} chapter(s) failed to recombine (see "
                f"`combine_failed`) — chapters/*.txt is stale for those; fix and run "
                f"`combine --chapters <ids>`."
            )
        instructions += (
            " Next: `align --chapters <set>` for the reader link, then `epub` when the "
            "book is done."
        )

    return {
        "committed": committed,
        "failed": failed,
        "missing": missing,
        "skipped_already_translated": skipped,
        "waived": waived,
        "evaluated": evaluated,
        "recombined": recombined,
        "combine_failed": combine_failed,
        "counts": {
            "committed": len(committed),
            "failed": len(failed),
            "missing": len(missing),
            "skipped": len(skipped),
            "evaluated": evaluated,
            "recombined": len(recombined),
            "combine_failed": len(combine_failed),
        },
        "instructions": instructions,
    }


def _split_matches_prompt(preamble_p: Path, body_p: Path, prompt_p: Path) -> bool:
    """True when ``preamble + body`` is byte-identical to ``prompt.txt``."""
    try:
        if not (preamble_p.exists() and body_p.exists() and prompt_p.exists()):
            return False
        return (
            preamble_p.read_text(encoding="utf-8") + body_p.read_text(encoding="utf-8")
            == prompt_p.read_text(encoding="utf-8")
        )
    except OSError:
        return False


def translate_fanout(
    project: str,
    *,
    chunk_ids: list[str] | None = None,
    concurrency: int | None = None,
    cli: str | None = None,
    cli_bin: str | None = None,
    claude_bin: str | None = None,
    effort: str | None = None,
    cache: str | None = None,
    runner=None,
) -> dict:
    """Run one headless CLI wave for translate-prepare manifest entries.

    Opt-in alternative to Task-tool workers. For each selected entry that lacks a
    non-empty draft, invoke the selected headless CLI (``claude`` or ``cursor``)
    from a neutral cwd, writing stdout to ``draft_path``.

    Claude profile: when ``preamble_path`` + ``body_path`` are present and
    ``preamble + body`` still equals ``prompt_path``, the body is the user prompt
    and the preamble is passed via ``--system-prompt-file`` (Claude Code
    cross-invocation cache on Sonnet). Cursor has no system-prompt-file flag —
    fan-out skips the split and sends the full prompt on stdin.

    Processes entries in waves of ``concurrency`` (default: manifest
    ``spawn_plan.batch_size``, else 3), finishing each wave before the next.
    Already-drafted entries are skipped (idempotent). Does **not** call
    ``translate-commit`` — the agent still commits after the wave.

    ``chunk_ids``, when given, limits the wave to those ids (chapter-parallel /
    sequential re-prepare loops pass only the current wave). ``runner`` is a
    test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    ``claude_bin`` is a back-compat alias for ``cli_bin``.
    ``effort`` is a per-run override of ``headless_effort_translate``;
    ``cache`` a per-run override of ``headless_prompt_cache``.
    """
    from src.harness.headless import run_headless_wave

    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    cli_name = (cli or cfg.get("headless_cli") or "claude").strip().lower()
    extra_flags, resolved_effort, _effort_source = state.resolve_headless_argv(
        cfg, command="translate", effort_override=effort,
    )
    requested_cache = state.resolve_prompt_cache(cfg, cache_override=cache)
    hdir = state.harness_dir(project_dir)
    manifest_path = hdir / "translate" / "manifest.json"
    if not manifest_path.exists():
        return {"error": "no manifest — run `translate-prepare` first", "wrote": []}
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"manifest unreadable (truncated write?): {exc}", "wrote": []}

    entries = list(doc.get("entries") or [])
    if chunk_ids is not None:
        wanted = set(chunk_ids)
        entries = [e for e in entries if e.get("chunk_id") in wanted]
        missing_ids = wanted - {e.get("chunk_id") for e in entries}
        if missing_ids:
            return {
                "error": f"chunk_ids not in manifest: {sorted(missing_ids)}",
                "wrote": [],
            }

    worker_model = doc.get("worker_model") or "sonnet"
    model_warning = _warn_cursor_claude_model(cli_name, worker_model)
    if model_warning:
        print(model_warning, file=sys.stderr)
    spawn_plan = doc.get("spawn_plan") or {}
    if concurrency is None:
        try:
            concurrency = int(spawn_plan.get("batch_size") or 3)
        except (TypeError, ValueError):
            concurrency = 3
    if concurrency < 1:
        return {"error": f"invalid concurrency {concurrency!r}; must be >= 1", "wrote": []}

    # Skip entries that already have a non-empty draft (idempotent / resume).
    # Cursor cannot use --system-prompt-file — skip the cache split entirely.
    use_cache_split = cli_name != "cursor"
    skipped: list[str] = []
    ready: list[dict] = []
    pre_failed: list[dict] = []
    for entry in entries:
        cid = entry["chunk_id"]
        draft_path = Path(entry["draft_path"])
        existing = _read_draft_text(draft_path) if draft_path.exists() else None
        if existing is not None and existing.strip():
            skipped.append(cid)
            continue
        prompt_path = Path(entry["prompt_path"])
        preamble = entry.get("preamble_path")
        body = entry.get("body_path")
        use_cached = bool(
            use_cache_split
            and preamble
            and body
            and _split_matches_prompt(Path(preamble), Path(body), prompt_path)
        )
        try:
            if use_cached:
                ready.append(
                    {
                        "id": cid,
                        "input_text": Path(body).read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": preamble,
                    }
                )
            elif not prompt_path.exists():
                pre_failed.append(
                    {"chunk_id": cid, "error": f"missing prompt_path: {prompt_path}"}
                )
            else:
                ready.append(
                    {
                        "id": cid,
                        "input_text": prompt_path.read_text(encoding="utf-8"),
                        "output_path": str(draft_path),
                        "system_prompt_file": None,
                    }
                )
        except OSError as exc:
            pre_failed.append(
                {"chunk_id": cid, "error": f"{type(exc).__name__}: {exc}"[:500]}
            )

    if not ready and not pre_failed:
        out = {
            "wrote": [],
            "failed": [],
            "skipped_existing_draft": skipped,
            "worker_model": worker_model,
            "cli": cli_name,
            "concurrency": concurrency,
            "cwd": None,
            "counts": {
                "wrote": 0,
                "failed": 0,
                "skipped": len(skipped),
                "todo": 0,
            },
            "instructions": (
                "Nothing to fan out — no matching manifest entries."
                if not skipped else
                "Run `translate-commit` to land drafts. Re-run `translate-fanout` "
                "(optionally with --chunk-ids) for any failed/missing, then commit again."
            ),
        }
        if model_warning:
            out["warning"] = model_warning
        return out

    wave_out = run_headless_wave(
        ready,
        model=worker_model,
        concurrency=concurrency,
        cli=cli_name,
        cli_bin=cli_bin,
        claude_bin=claude_bin,
        runner=runner,
        usage_log=hdir / "translate" / "usage.jsonl",
        extra_flags=extra_flags,
        effort=resolved_effort,
        cache=requested_cache,
    )

    # Fail-fast when the CLI binary is missing (no jobs ran).
    if "error" in wave_out and not wave_out.get("wrote") and not wave_out.get("failed"):
        err_out = {
            "error": wave_out["error"],
            "wrote": [],
            "failed": [],
            "skipped_existing_draft": skipped,
            "cli": cli_name,
            "counts": {
                "wrote": 0,
                "failed": 0,
                "skipped": len(skipped),
                "todo": 0,
            },
        }
        if model_warning:
            err_out["warning"] = model_warning
        return err_out

    failed = list(pre_failed)
    for item in wave_out.get("failed") or []:
        failed.append({"chunk_id": item["id"], "error": item["error"]})
    wrote = list(wave_out.get("wrote") or [])

    out = {
        "wrote": wrote,
        "failed": failed,
        "skipped_existing_draft": skipped,
        "worker_model": worker_model,
        "cli": cli_name,
        "concurrency": concurrency,
        "cwd": wave_out.get("cwd"),
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "skipped": len(skipped),
            "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Run `translate-commit` to land drafts. Re-run `translate-fanout` "
            "(optionally with --chunk-ids) for any failed/missing, then commit again."
            if (wrote or failed or skipped) else
            "Nothing to fan out — no matching manifest entries."
        ),
    }
    if wave_out.get("usage"):
        out["usage"] = wave_out["usage"]
    if model_warning:
        out["warning"] = model_warning
    return out


# ── retranslate (the redo verb — the one destructive beat) ──────────────────
#
# Everything else in this module is resume-shaped: idempotent FORWARD, skip what
# is already done. A redo inverts that, and the two intents are indistinguishable
# to prepare/fanout/commit because chunk state and draft state live apart:
#
#   clear translated_text  → drafts untouched
#   translate-prepare      → keeps non-empty drafts by design (:1296-1302); the
#                            rescue only covers drafts NOT in the new manifest
#                            (:1359-1360), so it reports rescued_prior_drafts: 0
#   translate-fanout       → skips every entry that already has a draft (:1776-1778)
#   translate-commit       → skips only chunks that already has_translation
#                            (:1543) — just cleared, so it commits ALL the stale
#                            drafts and reports "N committed, 0 failed"
#
# The user gets "fully re-translated, zero failures" over byte-identical old
# prose; every guard passes because the old drafts are genuinely good
# translations, just not new ones. This function is the supported way to break
# that chain: it clears the chunk AND deletes the draft (both sides of the
# landmine), with a preview, an optional archive, and a run-log beat.

def retranslate(
    project: str,
    *,
    chapters: str | None = None,
    chunk_ids: list[str] | None = None,
    yes: bool = False,
    archive: bool = False,
) -> dict:
    """Clear translations + their stale worker drafts so a redo actually re-runs.

    **Without ``yes`` this is a PREVIEW** — it reports exactly what would change and
    touches nothing (``dry_run: True``). With ``yes`` it deletes every in-scope chunk's
    rendered ``.draft.txt`` / ``.prompt.txt`` / ``.body.txt``, then clears translation
    fields (``translated_text`` / ``translated_at`` / ``status`` / ``review_data`` /
    ``last_llm_log`` / ``prompt_metadata``) only on chunks that still have a translation
    or ``review_data`` — already-pending rows keep their JSON byte-identical. Chunk JSON
    files are never deleted (``source_text`` and the chunking live there), and
    ``preamble.txt`` / ``manifest.json`` are kept: prepare rewrites both, and leaving
    the manifest makes a premature ``translate-commit`` fail LOUDLY with ``missing``
    instead of silently.

    ``archive`` snapshots the chunks, ``chapters/*.txt``, ``alignments/*.json``, the
    EPUBs and the review sidecars into ``archive/<stamp>/`` **before** anything is
    cleared; if that copy fails the whole command aborts having changed nothing.
    There is no restore command — restoring is a documented manual copy.

    Downstream artifacts (annotations, corrections, review marks, EPUBs) are
    **reported, never mutated** — annotations in particular carry sentence indices into
    the prose being replaced, so a redo mis-anchors them rather than merely staling
    them. ``evaluations/`` is self-healing (``translate-commit`` re-runs the coded
    evaluators per commit).

    Note for future work: per-chunk provenance cannot be stamped onto the chunk JSON —
    ``Chunk.model_config = {"extra": "ignore"}`` means any extra key is silently
    dropped on the next ``save_chunk``. The archive dir, the ``retranslate`` run-log
    event and ``translated_at: None`` carry that provenance instead.
    """
    from datetime import datetime

    project_dir = state.resolve_project_dir(project)
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {"error": "no chunks yet — nothing to re-translate", "cleared": []}
    if chapters and chunk_ids:
        return {"error": "pass --chapters or --chunk-ids, not both", "cleared": []}
    # An EMPTY (not absent) chunk_ids means the caller asked to scope the run and the
    # scope parsed to nothing — `--chunk-ids ""` or a comma typo. Falling through to
    # the `if chunk_ids:` test below would silently widen a one-chunk redo into the
    # whole book, and with --yes that clears every translation in the project. A
    # scope that parsed to nothing is an error, never "everything".
    if chunk_ids is not None and not chunk_ids:
        return {
            "error": "--chunk-ids parsed to an empty list (empty string or bare commas). "
                     "Refusing to widen an explicit scope to the whole project — pass real "
                     "chunk_ids, or omit the flag entirely to mean every chunk.",
            "cleared": [],
        }
    # Same footgun, other flag: `--chapters ""` is falsy and would fall through to
    # "all". An explicit empty scope is an error, never the whole book.
    if chapters is not None and not str(chapters).strip():
        return {
            "error": "--chapters is empty (empty string or whitespace). "
                     "Refusing to widen an explicit scope to the whole project — pass a "
                     "real chapter range, or omit the flag entirely to mean every chapter.",
            "cleared": [],
        }

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters, parse_chapter_range
    from src.models import ChunkStatus
    from src.utils.file_io import load_chunk, save_chunk

    all_discovered = discover_chapters(chunks_dir)

    # ── scope ────────────────────────────────────────────────────────────────
    scope_label: str
    in_scope: list[tuple[str, Path]] = []  # (chunk_id, chunk_path)
    if chunk_ids:
        by_id = {cp.stem: (cid, cp) for cid, cps in all_discovered.items() for cp in cps}
        unknown = [c for c in chunk_ids if c not in by_id]
        if unknown:
            return {
                "error": f"unknown chunk_ids: {', '.join(sorted(unknown))}",
                "cleared": [],
                "available_chunk_ids": sorted(by_id),
            }
        in_scope = [(cid, by_id[cid][1]) for cid in chunk_ids]
        scope_label = ",".join(chunk_ids)
    else:
        discovered = all_discovered
        if chapters:
            try:
                requested = parse_chapter_range(chapters)
            except (ValueError, TypeError) as exc:
                return {"error": f"invalid chapters value {chapters!r}: {exc}", "cleared": []}
            discovered = {k: v for k, v in all_discovered.items() if k in requested}
            if not discovered:
                return {
                    "cleared": [],
                    "chapters": chapters,
                    "available_chapters": sorted(all_discovered.keys()),
                    "note": f"no matching chapters for chapters {chapters}",
                }
        in_scope = [(cp.stem, cp) for cid in sorted(discovered) for cp in discovered[cid]]
        scope_label = chapters or "all"

    chunk_ids_in_scope = [cid for cid, _ in in_scope]
    _scope_set = set(chunk_ids_in_scope)
    chapter_ids = sorted({ch_id for ch_id, cps in all_discovered.items()
                          if any(cp.stem in _scope_set for cp in cps)})

    cleared: list[str] = []
    already_untranslated: list[str] = []
    unreadable: list[str] = []
    cleared_review_data: list[str] = []
    for cid, cp in in_scope:
        try:
            c = load_chunk(cp)
        except (OSError, ValueError):
            unreadable.append(cid)
            continue
        if c.has_translation:
            cleared.append(cid)
        else:
            already_untranslated.append(cid)
        if c.review_data is not None:
            cleared_review_data.append(cid)

    # ── stale drafts: the landmine, named explicitly ─────────────────────────
    tdir = state.harness_dir(project_dir) / "translate"

    def _file_info(p: Path) -> dict:
        st = p.stat()
        return {
            "path": str(p),
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "bytes": st.st_size,
        }

    stale_drafts: list[dict] = []
    for cid in chunk_ids_in_scope:
        dp = tdir / f"{cid}.draft.txt"
        if dp.exists() and (_read_draft_text(dp) or "").strip():
            stale_drafts.append({"chunk_id": cid, **_file_info(dp)})

    downstream = _downstream_report(project_dir, chapter_ids)
    warnings = _retranslate_warnings(downstream, stale_drafts, cleared_review_data)
    existing_archives = sorted(
        p.name for p in (project_dir / "archive").glob("*") if p.is_dir()
    ) if (project_dir / "archive").exists() else []

    counts = {
        "chunks": len(cleared),
        "chapters": len(chapter_ids),
        "drafts": len(stale_drafts),
        "annotations": downstream["annotations"]["rows_in_scope"],
        "reviewed": len(downstream["reviewed"]["marked_in_scope"]),
        "epubs": len(downstream["epubs"]),
    }

    # ── preview: report and stop ─────────────────────────────────────────────
    if not yes:
        return {
            "dry_run": True,
            "scope": {"chapters": scope_label if not chunk_ids else None,
                      "chunk_ids": chunk_ids or None},
            "chapters": chapter_ids,
            "cleared": cleared,
            "already_untranslated": already_untranslated,
            "unreadable": unreadable,
            "cleared_review_data": cleared_review_data,
            "stale_drafts": stale_drafts,
            "drafts_deleted": [],
            "prompts_deleted": 0,
            "bodies_deleted": 0,
            "archived": False,
            "archive": {"requested": archive, "dir": None, "manifest": None,
                        "files": 0, "total_bytes": 0,
                        "existing_archives": existing_archives},
            "downstream": downstream,
            "warnings": warnings,
            "counts": counts,
            "instructions": (
                f"PREVIEW ONLY — nothing was changed. {len(cleared)} chunk(s) across "
                f"{len(chapter_ids)} chapter(s) would be cleared and {len(stale_drafts)} stale "
                f"draft(s) deleted. Show the user `stale_drafts`, `downstream`, `warnings`"
                f"{', and `unreadable`' if unreadable else ''}, "
                f"then END THE TURN and ask for approval (including whether to archive). "
                f"Re-run with --yes (add --archive to snapshot first) in a LATER turn."
            ),
        }

    # ── archive FIRST: a precondition, not best-effort ───────────────────────
    # projects/ is gitignored, so this snapshot is the only thing between the user
    # and unrecoverable loss. Deliberately unlike the evaluator's swallow-all in
    # translate_commit: if any copy fails we abort having changed nothing.
    archive_info: dict = {"requested": archive, "dir": None, "manifest": None,
                          "files": 0, "total_bytes": 0,
                          "existing_archives": existing_archives}
    if archive:
        try:
            archive_info = _write_retranslate_archive(
                project_dir,
                chunk_paths=[cp for _, cp in in_scope],
                chapter_ids=chapter_ids,
                scope={"chapters": scope_label if not chunk_ids else None,
                       "chunk_ids": chunk_ids or None},
                downstream=downstream,
            )
            archive_info["requested"] = True
            archive_info["existing_archives"] = existing_archives
        except OSError as exc:
            return {
                "error": f"archive failed, nothing was cleared: {exc}",
                "archived": False,
                "cleared": [],
                "drafts_deleted": [],
                "stale_drafts": stale_drafts,
                "counts": {**counts, "chunks": 0, "drafts": 0},
            }

    # ── execute: drafts FIRST, then the chunk JSON ───────────────────────────
    # The order IS the safety argument. There is no atomic two-file write here, so
    # pick the half-done state that is survivable:
    #   drafts→chunks (this order): a crash in between leaves the chunks still
    #     translated and the drafts gone. The redo simply did not happen — prepare
    #     skips translated chunks, so nothing stale can land — and re-running
    #     `retranslate` finishes the job. Idempotent.
    #   chunks→drafts (the tempting order): a crash in between leaves cleared chunks
    #     and surviving drafts, which is EXACTLY the silent no-op documented above:
    #     fanout skips the chunk because a draft exists, commit re-lands the old prose
    #     and reports "0 failed". Recreating that landmine inside the verb built to
    #     defuse it is the one outcome this function must not have.
    drafts_deleted: list[dict] = []
    prompts_deleted = bodies_deleted = 0
    for cid in chunk_ids_in_scope:
        dp = tdir / f"{cid}.draft.txt"
        if dp.exists():
            drafts_deleted.append({"chunk_id": cid, **_file_info(dp)})
            dp.unlink(missing_ok=True)
        # prompt/body were rendered against the OLD previous-section context (and
        # possibly an older glossary/style guide); prepare rewrites both. A surviving
        # stale prompt+body pair can also reattach cache-split paths to a rescued draft
        # via _split_matches_prompt.
        pp = tdir / f"{cid}.prompt.txt"
        if pp.exists():
            pp.unlink(missing_ok=True)
            prompts_deleted += 1
        bp = tdir / f"{cid}.body.txt"
        if bp.exists():
            bp.unlink(missing_ok=True)
            bodies_deleted += 1

    for _cid, cp in in_scope:
        try:
            c = load_chunk(cp)
        except (OSError, ValueError):
            continue
        # Nothing to clear ⇒ do not rewrite the file. The preview promised exactly
        # `cleared` + `cleared_review_data`; rewriting an already-PENDING row would
        # break that promise, drop a FAILED row's last_llm_log diagnostics, and bump
        # an mtime that `status` reads as the combine_stale signal.
        if not c.has_translation and c.review_data is None:
            continue
        c.translated_text = None
        c.translated_at = None
        c.status = ChunkStatus.PENDING  # the ENUM: a bare "pending" trips a pydantic
        c.review_data = None            # serializer warning on save
        c.last_llm_log = None
        c.prompt_metadata = None
        save_chunk(c, cp)

    from src.utils.run_logger import log_run_event
    log_run_event(
        run_id=state.ensure_run_id(project_dir),
        project=project_dir.name,
        event="retranslate",
        scope=scope_label,
        chunks=len(cleared),
        chapters=len(chapter_ids),
        drafts_deleted=len(drafts_deleted),
        review_data_cleared=len(cleared_review_data),
        archived=bool(archive_info.get("dir")),
        archive_dir=archive_info.get("dir"),
    )

    return {
        "dry_run": False,
        "scope": {"chapters": scope_label if not chunk_ids else None,
                  "chunk_ids": chunk_ids or None},
        "chapters": chapter_ids,
        "cleared": cleared,
        "already_untranslated": already_untranslated,
        "unreadable": unreadable,
        "cleared_review_data": cleared_review_data,
        "stale_drafts": stale_drafts,
        "drafts_deleted": drafts_deleted,
        "prompts_deleted": prompts_deleted,
        "bodies_deleted": bodies_deleted,
        "archived": bool(archive_info.get("dir")),
        "archive": archive_info,
        "downstream": downstream,
        "warnings": warnings,
        "counts": {**counts, "drafts": len(drafts_deleted)},
        "instructions": (
            f"Cleared {len(cleared)} chunk(s); deleted {len(drafts_deleted)} draft(s). "
            f"Next: `translate-prepare` for this scope, then PROBE ONE chunk "
            f"(`translate-fanout --chunk-ids <one_id>`) and confirm it lands in `wrote`, "
            f"NOT `skipped_existing_draft`, before the full wave. Nothing under `downstream` "
            f"was mutated — those artifacts still describe the replaced text."
        ),
    }


def _downstream_report(project_dir: Path, chapter_ids: list[str]) -> dict:
    """Read-only census of artifacts that will describe replaced prose after a redo.

    Never mutates anything. ``evaluations/`` is reported with ``self_healing: True``
    because ``translate_commit`` re-runs the coded evaluators per committed chunk — the
    one downstream artifact that survives a redo consistent.
    """
    from datetime import datetime

    scope = set(chapter_ids)

    def _jsonl_rows(path: Path) -> tuple[int, int]:
        """(total rows, rows whose chapter_id is in scope)."""
        if not path.exists():
            return 0, 0
        total = in_scope = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    if json.loads(line).get("chapter_id") in scope:
                        in_scope += 1
                except json.JSONDecodeError:
                    continue
        except OSError:
            return total, in_scope
        return total, in_scope

    ann_path = project_dir / "annotations.jsonl"
    ann_rows, ann_in_scope = _jsonl_rows(ann_path)
    corr_path = project_dir / "corrections_applied.jsonl"
    corr_rows, corr_in_scope = _jsonl_rows(corr_path)
    retr_path = project_dir / "retranslations.jsonl"
    retr_rows, _ = _jsonl_rows(retr_path)

    reviewed_path = project_dir / "reviewed.json"
    marked: list[str] = []
    if reviewed_path.exists():
        try:
            marked = sorted(json.loads(reviewed_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            marked = []

    epubs: list[dict] = []
    for p in sorted(project_dir.glob("*.epub")):
        try:
            st = p.stat()
        except OSError:
            continue
        epubs.append({
            "name": p.name,
            "built_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size_kb": round(st.st_size / 1024),
        })

    align_dir = project_dir / "alignments"
    aligned = sorted(ch for ch in scope if (align_dir / f"{ch}.json").exists())
    eval_dir = project_dir / "evaluations"
    n_evals = len(list(eval_dir.glob("*.json"))) if eval_dir.exists() else 0
    edits_dir = project_dir / ".chunk_edits"
    edited = sorted(ch for ch in scope if (edits_dir / ch).exists())

    return {
        "annotations": {"path": str(ann_path), "rows": ann_rows,
                        "rows_in_scope": ann_in_scope},
        "corrections_applied": {"path": str(corr_path), "rows": corr_rows,
                                "rows_in_scope": corr_in_scope},
        "reviewed": {"path": str(reviewed_path), "marked": marked,
                     "marked_in_scope": sorted(set(marked) & scope)},
        "epubs": epubs,
        "alignments": {"chapters": aligned, "count": len(aligned)},
        "evaluations": {"count": n_evals, "self_healing": True},
        "chapters_txt": {
            "chapters": sorted(ch for ch in scope
                               if (project_dir / "chapters" / f"{ch}.txt").exists()),
            "note": ("still hold the replaced translation until translate-commit "
                     "recombines each chapter as it completes"),
        },
        "chunk_edits": {"chapters": edited},
        "retranslations": {"rows": retr_rows},
    }


def _retranslate_warnings(downstream: dict, stale_drafts: list[dict],
                          cleared_review_data: list[str]) -> list[str]:
    """Human-readable warnings the skill must relay verbatim to the user."""
    out: list[str] = []
    if stale_drafts:
        out.append(
            f"{len(stale_drafts)} stale worker draft(s) are on disk. Clearing "
            f"translated_text WITHOUT deleting these is a silent no-op: translate-fanout "
            f"skips the chunk and translate-commit re-lands the OLD prose reporting success. "
            f"This command deletes them."
        )
    n_ann = downstream["annotations"]["rows_in_scope"]
    if n_ann:
        out.append(
            f"{n_ann} annotation(s) anchor to sentences in the prose being replaced. Their "
            f"es_idx is an index into the TRANSLATION, so after a redo they are MIS-anchored, "
            f"not merely stale. They are not touched — decide with the user."
        )
    n_rev = len(downstream["reviewed"]["marked_in_scope"])
    if n_rev:
        out.append(f"{n_rev} chapter(s) are marked reviewed and will still be marked "
                   f"reviewed over prose nobody has read.")
    if downstream["epubs"]:
        out.append(f"{len(downstream['epubs'])} EPUB(s) were built from the replaced text — "
                   f"rebuild with `epub` when the redo lands.")
    if downstream["alignments"]["count"]:
        out.append(f"{downstream['alignments']['count']} alignment file(s) index into the "
                   f"replaced prose — re-run `align` after the redo or the reader shows "
                   f"mismatched Spanish.")
    if cleared_review_data:
        out.append(f"{len(cleared_review_data)} chunk(s) carry in-chunk review_data, which IS "
                   f"cleared (its offsets point into the deleted prose).")
    n_txt = len(downstream["chapters_txt"]["chapters"])
    if n_txt:
        # Named here because `status` cannot name it: combine only runs on FULLY
        # translated chapters, so a chapter mid-redo is neither complete (no
        # combine_stale row) nor visibly wrong — chapters/<id>.txt still holds the
        # whole previous translation and the web reader keeps serving it. The window
        # closes when the chapter finishes re-translating and translate-commit
        # recombines it. This warning is the only place the user is told it is open.
        out.append(
            f"{n_txt} chapters/*.txt file(s) still hold the PREVIOUS translation and keep "
            f"serving it to the web reader for the whole redo. `status.combine_stale` will "
            f"NOT flag them while the chapter is partially translated (combine only runs on "
            f"complete chapters) — the window closes when translate-commit recombines each "
            f"chapter as it completes."
        )
    return out


def _write_retranslate_archive(project_dir: Path, *, chunk_paths: list[Path],
                               chapter_ids: list[str], scope: dict,
                               downstream: dict) -> dict:
    """Snapshot everything describing the current translation. Raises OSError on failure.

    Lives at ``projects/<slug>/archive/<stamp>/`` — deliberately NOT under
    ``.harness/``, which ``setup`` wipes wholesale (``ensure_harness_dir(clean=True)``),
    and deliberately two levels deep so no existing single-level glob can see it (the
    project-root ``*.epub`` glob in ``status`` being the one that matters).
    """
    import shutil
    from datetime import datetime

    root = project_dir / "archive"
    stamp = f"retranslate_{datetime.now():%Y%m%d_%H%M%S}"
    dest = root / stamp
    suffix = 2
    while dest.exists():
        dest = root / f"{stamp}-{suffix}"
        suffix += 1
    dest.mkdir(parents=True)

    files: list[dict] = []

    def _copy(src: Path, rel: str) -> None:
        if not src.exists():
            return
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)  # copy2 preserves mtime — which era the prose is from
        st = src.stat()
        files.append({
            "src": str(src.relative_to(project_dir)),
            "archived": rel,
            "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })

    try:
        for cp in chunk_paths:
            _copy(cp, f"chunks/{cp.name}")
        for ch in chapter_ids:
            _copy(project_dir / "chapters" / f"{ch}.txt", f"chapters/{ch}.txt")
            _copy(project_dir / "alignments" / f"{ch}.json", f"alignments/{ch}.json")
        for p in sorted(project_dir.glob("*.epub")):
            _copy(p, f"epubs/{p.name}")
        for name in ("annotations.jsonl", "corrections_applied.jsonl", "reviewed.json"):
            _copy(project_dir / name, name)

        manifest = {
            "kind": "retranslate",
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "run_id": state.ensure_run_id(project_dir),
            "project": project_dir.name,
            "scope": scope,
            "chunk_ids": [cp.stem for cp in chunk_paths],
            "chapter_ids": chapter_ids,
            "counts": {
                "chunks": len(chunk_paths),
                "chapters_txt": sum(1 for f in files if f["archived"].startswith("chapters/")),
                "alignments": sum(1 for f in files if f["archived"].startswith("alignments/")),
                "epubs": sum(1 for f in files if f["archived"].startswith("epubs/")),
                "annotations_rows": downstream["annotations"]["rows"],
                "corrections_rows": downstream["corrections_applied"]["rows"],
                "reviewed_marks": len(downstream["reviewed"]["marked"]),
                "files": len(files),
                "bytes": sum(f["bytes"] for f in files),
            },
            "files": files,
            # The census in `downstream` is deliberately WIDER than this snapshot, so
            # spell out the gap here rather than only in the skill docs: this manifest
            # is what a user reads months later when deciding whether the archive is
            # enough to go back, and "copy back what you want" is a false promise for
            # anything not in `contains`.
            "contains": [
                "chunks/*.json (in scope)", "chapters/*.txt", "alignments/*.json",
                "epubs/*.epub", "annotations.jsonl", "corrections_applied.jsonl",
                "reviewed.json",
            ],
            "excludes": [
                ".chunk_edits/ (manual per-chunk edit history)",
                "retranslations.jsonl",
                "evaluations/ (self-healing — translate-commit re-runs the evaluators)",
                ".harness/ (prompts, drafts, manifest — all rebuilt by translate-prepare)",
            ],
            "restore": (
                "No restore command exists. Restoring is a manual copy back over the project of "
                "the paths in `contains` — then re-run `combine` + `align`. Anything under "
                "`excludes` is NOT in this snapshot and cannot be restored from it. See "
                ".claude/skills/translate-harness/references/retranslate.md, "
                "'Restoring from an archive'."
            ),
        }
        manifest_path = dest / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        # Partial stamp must not linger under archive/ (preview lists existing_archives).
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {
        "requested": True,
        "dir": str(dest),
        "manifest": str(manifest_path),
        "files": len(files),
        "total_bytes": manifest["counts"]["bytes"],
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


def _append_always_include_flags(cmd: list[str], cfg: dict) -> None:
    """Append --always-dialogue / --always-images flags from harness config."""
    if cfg.get("always_include_dialogue"):
        cmd.append("--always-dialogue")
    else:
        # Explicit off so translate_book.py does not inherit a stale env/default
        # and so the preflight summary matches the book's saved preference.
        cmd.append("--no-always-dialogue")
    images = cfg.get("always_include_image_instructions")
    if images is True:
        cmd.append("--always-images")
    elif images is False:
        cmd.append("--no-always-images")
    # None / absent → leave unset so translate_book.py auto-derives from chunks.


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
    _append_always_include_flags(cmd, cfg)
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
    _append_always_include_flags(cmd, cfg)
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


# ── footnotes (imported Gutenberg reader footnotes) ─────────────────────────

def _footnote_counts(sidecar: Path) -> dict:
    """Read footnotes.json and count total / translated / pending note bodies."""
    try:
        notes = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - counts are advisory, never fatal
        return {"total": 0, "translated": 0, "pending": 0}
    translated = sum(1 for n in notes if (n.get("translated_body") or "").strip())
    return {"total": len(notes), "translated": translated, "pending": len(notes) - translated}


# ── backend resolution (carry the chapter choice forward to footnotes) ──────

_BACKENDS = ("api", "subagent", "headless")


def resolve_backend(project_dir: Path, explicit: str | None = None) -> str:
    """Resolve which translation backend a step should use.

    The chapter-translation backend the user picks in Step 4/4B is recorded as a
    ``backend`` beat in ``logs/harness_runs.jsonl`` (``api`` | ``subagent`` |
    ``headless``) and optionally persisted via ``config-set`` into
    ``.harness/config.json``. Footnote translation carries that choice forward so
    the note bodies translate on the *same* backend. Resolution order:

    1. ``explicit`` (an ``--backend`` flag) when it names a known backend.
    2. The most recent ``backend`` beat for this project's current ``run_id``. A
       legacy ``subagent`` beat paired with a later ``fanout_mode: headless`` beat
       resolves to ``headless`` (headless used to be a sub-mode of subagent).
    3. The most recent ``backend`` beat for the project across any run.
    4. Persisted ``config.backend`` from ``config-set``.
    5. Config inference: persisted subagent spawn knobs (``worker_model`` /
       ``parallelism``) imply ``subagent``; otherwise ``api`` (today's default, so
       projects predating the run-log beat are unchanged).
    """
    if explicit in _BACKENDS:
        return explicit

    from src.utils.run_logger import read_run_events

    cfg = state.load_config(project_dir)
    slug = project_dir.name

    def _from_events(events: list[dict]) -> str | None:
        backend: str | None = None
        fanout_headless = False
        for rec in events:  # chronological; last write wins
            ev = rec.get("event")
            if ev == "backend" and rec.get("backend") in _BACKENDS:
                backend = rec.get("backend")
                fanout_headless = False  # a fresh backend pick resets any pairing
            elif ev == "fanout_mode":
                fanout_headless = rec.get("mode") == "headless"
        if backend == "subagent" and fanout_headless:
            return "headless"
        return backend

    run_id = cfg.get("run_id")
    if run_id:
        resolved = _from_events(read_run_events(project=slug, run_id=run_id))
        if resolved:
            return resolved
    resolved = _from_events(read_run_events(project=slug))
    if resolved:
        return resolved

    # Persisted once-per-book choice (config-set / skill router). Checked after
    # run-log beats so an in-progress run's logged pick still wins mid-session.
    if cfg.get("backend") in _BACKENDS:
        return cfg["backend"]

    if cfg.get("worker_model") or cfg.get("parallelism"):
        return "subagent"
    return "api"


# ── footnote translation: engine per backend (api / headless / subagent) ────

def _footnote_work_dir(project_dir: Path) -> Path:
    return state.harness_dir(project_dir) / "footnotes"


def _footnote_manifest_path(project_dir: Path) -> Path:
    return _footnote_work_dir(project_dir) / "manifest.json"


def _render_footnote_batches(
    project_dir: Path, *, retranslate: bool, source_language: str = "English",
) -> tuple[list[dict], dict]:
    """Render one prompt file per pending footnote batch; return ``(entries, meta)``.

    Rebuilds ``.harness/footnotes/`` from the current pending set (notes lacking a
    ``translated_body``, or every note when ``retranslate``). Stale ``batch_*``
    prompt/draft files from a prior prepare are cleared first so ``commit`` never
    reads a draft against a regrouped batch — committed notes have already left the
    pending set, so a re-prepare only ever rebuilds still-unfinished work.
    """
    from src.footnote_import import load_footnotes_sidecar
    from src.footnotes_translate_core import (
        batch_notes, build_footnotes_prompt, read_glossary_text, read_style_text,
    )

    cfg = state.load_config(project_dir)
    notes = load_footnotes_sidecar(project_dir)
    pending = [n for n in notes if retranslate or not (n.get("translated_body") or "").strip()]

    fdir = _footnote_work_dir(project_dir)
    fdir.mkdir(parents=True, exist_ok=True)
    for stale in list(fdir.glob("batch_*.prompt.txt")) + list(fdir.glob("batch_*.draft.txt")):
        try:
            stale.unlink()
        except OSError:
            pass

    glossary = read_glossary_text(project_dir)
    style = read_style_text(project_dir)
    target = cfg.get("target_language") or "Spanish"
    title = cfg.get("title") or project_dir.name

    entries: list[dict] = []
    for i, batch in enumerate(batch_notes(pending)):
        bid = f"batch_{i:02d}"
        prompt = build_footnotes_prompt(
            batch, source_language=source_language, target_language=target,
            title=title, glossary_text=glossary, style_text=style,
        )
        prompt_path = fdir / f"{bid}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        entries.append({
            "batch_id": bid,
            "prompt_path": str(prompt_path),
            "draft_path": str(fdir / f"{bid}.draft.txt"),
            "numbers": [n["number"] for n in batch],
        })

    meta = {
        "worker_model": cfg.get("worker_model") or "sonnet",
        "source_language": source_language,
        "target_language": target,
        "title": title,
        "total": len(notes),
        "pending": len(pending),
    }
    return entries, meta


def _write_footnote_manifest(project_dir: Path, entries: list[dict], meta: dict) -> None:
    doc = {**meta, "entries": entries}
    _footnote_manifest_path(project_dir).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def footnotes_translate(
    project: str,
    *,
    yes: bool = False,
    backend: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    retranslate: bool = False,
    runner=None,
    cli: str | None = None,
    cli_bin: str | None = None,
    claude_bin: str | None = None,
    effort: str | None = None,
    cache: str | None = None,
) -> int | dict:
    """Translate the imported footnote bodies on the book's chosen backend.

    The backend is carried forward from the chapter translation (``--backend`` for an
    explicit override; otherwise resolved from the ``backend`` run-log beat):

    - **api** — the metered path. Fail closed without ``--yes`` (the cost must be
      approved by the user in a SEPARATE turn first), then shell to
      ``translate_footnotes.py`` → ``call_llm``.
    - **headless** — render batch prompts, run one headless CLI wave, and commit the
      drafts. No metered spend, so no ``--yes`` gate. ``cli`` / ``headless_cli``
      selects Claude vs Cursor.
    - **subagent** — the Task-worker path is orchestrator-driven, so this does *not*
      translate; it returns guidance to use ``footnotes translate-prepare`` → spawn
      ``translator`` subagents → ``footnotes translate-commit``.

    A clean no-op when no footnotes were imported.
    """
    project_dir = state.resolve_project_dir(project)
    sidecar = project_dir / "footnotes.json"
    if not sidecar.exists():
        return _stream_result("footnotes", 0, {
            "stage": "footnotes-translate",
            "note": "no footnotes.json — nothing to translate (import at setup with --footnotes import)",
            "total": 0, "translated": 0, "pending": 0,
        })

    resolved = resolve_backend(project_dir, backend)

    if resolved == "subagent":
        # Spawning Task workers is the orchestrator's job, not the CLI's — point the
        # agent at the prepare/commit seam instead of spending anything here.
        return _stream_result("footnotes", 0, {
            "stage": "footnotes-translate",
            "backend": "subagent",
            "action_required": "prepare-commit",
            "note": (
                "backend is subagent (Task workers): run `footnotes translate-prepare`, spawn a "
                "`translator` subagent per batch to fill each draft, then `footnotes "
                "translate-commit`. No API spend, no --yes."
            ),
            **_footnote_counts(sidecar),
        })

    if resolved == "headless":
        hres = _footnotes_headless(
            project, retranslate=retranslate, runner=runner,
            cli=cli, cli_bin=cli_bin, claude_bin=claude_bin,
            effort=effort, cache=cache,
        )
        # A hard wave failure (e.g. CLI off PATH) is a real failure, not a no-op —
        # exit non-zero so it is never mistaken for success. Partial misses stay rc 0
        # (reported under `pending` for a re-run, like the chapter fan-out).
        rc = 1 if hres.get("error") else 0
        return _stream_result("footnotes", rc, hres)

    # resolved == "api": the metered path, gated on --yes like `translate`.
    if not yes:
        print(
            "Refusing to translate footnotes without --yes. The cost must be approved by the "
            "user in a SEPARATE turn first; only then re-run `footnotes translate` with --yes.",
            file=sys.stderr,
        )
        return 2
    cfg = state.load_config(project_dir)
    cmd = [
        "scripts/translate_footnotes.py",
        "--project-dir", str(project_dir),
        "--target-lang", cfg.get("target_language") or "Spanish",
        "--title", cfg.get("title") or project_dir.name,
    ]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    if retranslate:
        cmd.append("--retranslate")
    result = _stream_result("footnotes", *_run_script(cmd))
    # translate_footnotes.py emits no HARNESS_RESULT sentinel; derive counts from the sidecar.
    result["stage"] = "footnotes-translate"
    result["backend"] = "api"
    result.update(_footnote_counts(sidecar))
    return result


def footnotes_translate_prepare(project: str, *, retranslate: bool = False) -> dict:
    """Subagent path: render a prompt per pending footnote batch + a manifest (no spend).

    The Task-worker analog of ``translate-prepare`` for footnotes: the agent then spawns a
    ``translator`` subagent per ``batch_NN`` (reads ``batch_NN.prompt.txt`` → writes
    ``batch_NN.draft.txt``) and runs ``footnotes translate-commit``. A clean no-op when no
    footnotes were imported or all are already translated.
    """
    project_dir = state.resolve_project_dir(project)
    sidecar = project_dir / "footnotes.json"
    if not sidecar.exists():
        return {"command": "footnotes", "exit_code": 0,
                "stage": "footnotes-translate-prepare",
                "note": "no footnotes.json — nothing to prepare", "entries": [],
                "total": 0, "pending": 0}
    entries, meta = _render_footnote_batches(project_dir, retranslate=retranslate)
    _write_footnote_manifest(project_dir, entries, meta)
    return {
        "command": "footnotes",
        "exit_code": 0,
        "stage": "footnotes-translate-prepare",
        "worker_model": meta["worker_model"],
        "total": meta["total"],
        "pending": meta["pending"],
        "entries": entries,
        "instructions": (
            "Spawn one `translator` subagent per entry: read `prompt_path`, write ONLY the "
            "`N| <translation>` lines to `draft_path`, reply `done <batch_id>`. Then run "
            "`footnotes translate-commit`."
            if entries else
            "Nothing to prepare — all imported footnotes are already translated."
        ),
    }


def footnotes_translate_commit(project: str) -> dict:
    """Parse the batch drafts from a footnote prepare into ``footnotes.json``.

    Reads each manifest entry's ``draft_path``, parses its ``N| translation`` lines, and
    writes the results into the note bodies. Idempotent: already-filled notes are left
    as-is, and a missing/blank draft leaves its notes ``pending`` for a re-spawn.
    """
    from src.footnote_import import (
        load_footnotes_sidecar, write_footnotes_sidecar, FootnoteRecord, footnotes_sidecar_path,
    )
    from src.footnotes_translate_core import parse_numbered_translations

    project_dir = state.resolve_project_dir(project)
    manifest_path = _footnote_manifest_path(project_dir)
    if not manifest_path.exists():
        return {"command": "footnotes", "exit_code": 1,
                "stage": "footnotes-translate-commit",
                "error": "no footnote manifest — run `footnotes translate-prepare` first",
                "committed": [], "pending": [], "failed": []}
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"command": "footnotes", "exit_code": 1,
                "stage": "footnotes-translate-commit",
                "error": f"manifest unreadable: {exc}",
                "committed": [], "pending": [], "failed": []}

    notes = load_footnotes_sidecar(project_dir)
    by_number = {n["number"]: n for n in notes}
    committed: list[int] = []
    pending: list[int] = []

    for entry in doc.get("entries") or []:
        numbers = entry.get("numbers") or []
        draft_path = Path(entry["draft_path"])
        text = _read_draft_text(draft_path) if draft_path.exists() else None
        if not text or not text.strip():
            pending.extend(numbers)
            continue
        translations = parse_numbered_translations(text)
        for num in numbers:
            note = by_number.get(num)
            if note is None:
                continue
            body = (translations.get(num) or "").strip()
            if body:
                note["translated_body"] = body
                committed.append(num)
            else:
                pending.append(num)

    records = [FootnoteRecord(
        number=n["number"], ref_marker=n.get("ref_marker", ""),
        source_body=n.get("source_body", ""), detected=n.get("detected", ""),
        translated_body=n.get("translated_body"),
    ) for n in notes]
    write_footnotes_sidecar(project_dir, records)
    counts = _footnote_counts(footnotes_sidecar_path(project_dir))
    return {
        "command": "footnotes",
        # A partial commit (some notes still `pending`) is not a failure — it mirrors
        # the chapter fan-out, which stays rc 0 and reports misses for a re-run. The
        # headless path re-wraps this via `_stream_result`, which overrides both keys.
        "exit_code": 0,
        "stage": "footnotes-translate-commit",
        # counts first: the per-commit `committed`/`pending` note-number lists then
        # override the count ints of the same name with the actionable detail.
        **counts,
        "committed": sorted(set(committed)),
        "pending": sorted(set(pending)),
    }


def _footnotes_headless(
    project: str, *, retranslate: bool, runner=None,
    cli: str | None = None, cli_bin: str | None = None,
    claude_bin: str | None = None,
    concurrency: int | None = None,
    effort: str | None = None,
    cache: str | None = None,
) -> dict:
    """Headless backend: render batches, run one CLI wave, commit the drafts.

    The footnote analog of ``translate-fanout``: prepare → wave → commit in one call
    (footnotes are few and short, so there is no per-wave Task orchestration). ``runner``
    is the ``run_headless_wave`` test seam.
    """
    from src.harness.headless import run_headless_wave

    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    cli_name = (cli or cfg.get("headless_cli") or "claude").strip().lower()
    extra_flags, resolved_effort, _effort_source = state.resolve_headless_argv(
        cfg, command="footnotes", effort_override=effort,
    )
    requested_cache = state.resolve_prompt_cache(cfg, cache_override=cache)
    entries, meta = _render_footnote_batches(project_dir, retranslate=retranslate)
    _write_footnote_manifest(project_dir, entries, meta)

    if not entries:
        counts = _footnote_counts(project_dir / "footnotes.json")
        return {"stage": "footnotes-translate", "backend": "headless",
                "cli": cli_name,
                "note": "all imported footnotes are already translated",
                "committed": [], "pending": [], **counts}

    jobs = [{
        "id": e["batch_id"],
        "input_text": Path(e["prompt_path"]).read_text(encoding="utf-8"),
        "output_path": e["draft_path"],
    } for e in entries]

    if concurrency is None:
        try:
            concurrency = int(cfg.get("batch_size") or 3)
        except (TypeError, ValueError):
            concurrency = 3

    worker_model = meta["worker_model"]
    model_warning = _warn_cursor_claude_model(cli_name, worker_model)
    if model_warning:
        print(model_warning, file=sys.stderr)

    wave = run_headless_wave(
        jobs, model=worker_model, concurrency=concurrency,
        cli=cli_name, cli_bin=cli_bin, claude_bin=claude_bin, runner=runner,
        usage_log=state.harness_dir(project_dir) / "footnotes" / "usage.jsonl",
        extra_flags=extra_flags,
        effort=resolved_effort,
        cache=requested_cache,
    )
    # Fail fast when the CLI is missing (no jobs ran) — never silently fall back to spend.
    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        counts = _footnote_counts(project_dir / "footnotes.json")
        out = {"stage": "footnotes-translate", "backend": "headless",
               "cli": cli_name,
               "error": wave["error"], "committed": [], "pending": [], **counts}
        if model_warning:
            out["warning"] = model_warning
        return out

    result = footnotes_translate_commit(project)
    result["stage"] = "footnotes-translate"
    result["backend"] = "headless"
    result["cli"] = cli_name
    result["wave"] = {"wrote": wave.get("wrote") or [], "failed": wave.get("failed") or []}
    if wave.get("usage"):
        result["wave"]["usage"] = wave["usage"]
    if model_warning:
        result["warning"] = model_warning
    return result


def footnotes_apply(project: str) -> int | dict:
    """Free: convert surviving ``[FOOTNOTE:N]`` tokens into reader footnotes + rebuild the EPUB.

    Runs only the ``footnotes`` pipeline stage (last in ``translate_book``'s STAGES), which
    needs the align stage to have run — un-aligned chapters are skipped. Idempotent:
    re-running re-converts (prior ``origin:"gutenberg"`` annotations are tombstoned), so it is
    safe after the API path's auto-run. A clean no-op when no footnotes were imported.
    """
    project_dir = state.resolve_project_dir(project)
    sidecar = project_dir / "footnotes.json"
    if not sidecar.exists():
        return _stream_result("footnotes", 0, {
            "stage": "footnotes-apply",
            "note": "no footnotes.json — nothing to apply",
            "footnotes_written": 0,
        })
    cfg = state.load_config(project_dir)
    result = _stream_result("footnotes", *_run_script([
        "scripts/translate_book.py",
        "--project-dir", str(project_dir),
        "--start-stage", "footnotes",
        "--project-name", cfg.get("title") or project_dir.name,
        "--author", cfg.get("author") or "Unknown",
        "--target-lang-code", cfg.get("language_code") or "es",
    ]))
    result["stage"] = "footnotes-apply"
    # The stage prints but emits no sentinel; read the checkpoint for the count + EPUB path.
    try:
        pstate = json.loads((project_dir / "pipeline_state.json").read_text(encoding="utf-8"))
        result["footnotes_written"] = pstate.get("footnotes_written", 0)
        if pstate.get("epub_path"):
            result["epub_path"] = pstate["epub_path"]
    except Exception:  # noqa: BLE001 - advisory enrichment only
        pass
    return result


def footnotes_drop(project: str) -> dict:
    """Free/local: revert an import without re-fetching — the Step 0 'drop' choice.

    Strips ``[FOOTNOTE:N]`` tokens from ``source.txt`` and every split ``chapter_*.txt``, and
    deletes ``footnotes.json``, so the rest of the pipeline runs as if footnotes were dropped
    at ingest. Reflects the drop in ``pipeline_state.json`` so ``status``/``setup`` read consistently.
    """
    from src.utils.text_utils import strip_footnote_tokens

    project_dir = state.resolve_project_dir(project)
    targets: list[Path] = []
    src = project_dir / "source.txt"
    if src.exists():
        targets.append(src)
    targets.extend(sorted((project_dir / "chapters").glob("chapter_*.txt")))

    tokens_stripped = 0
    files_cleaned = 0
    for p in targets:
        text = p.read_text(encoding="utf-8")
        if "[FOOTNOTE:" not in text:
            continue
        cleaned, placements = strip_footnote_tokens(text)
        p.write_text(cleaned, encoding="utf-8")
        tokens_stripped += len(placements)
        files_cleaned += 1

    sidecar = project_dir / "footnotes.json"
    sidecar_removed = sidecar.exists()
    if sidecar_removed:
        sidecar.unlink()

    pstate_path = project_dir / "pipeline_state.json"
    if pstate_path.exists():
        try:
            pstate = json.loads(pstate_path.read_text(encoding="utf-8"))
            pstate["footnote_count"] = 0
            pstate["footnote_mode"] = "drop"
            pstate_path.write_text(
                json.dumps(pstate, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - checkpoint hygiene only
            pass

    return {
        "command": "footnotes",
        "exit_code": 0,
        "stage": "footnotes-drop",
        "tokens_stripped": tokens_stripped,
        "files_cleaned": files_cleaned,
        "sidecar_removed": sidecar_removed,
    }


def combine(project: str, *, chapters: str | None = None) -> dict:
    """Rewrite ``chapters/<chapter_id>.txt`` from the translated chunks.

    ``projects/<slug>/chapters/<id>.txt`` is **dual-purpose**: the split step writes
    the ENGLISH section text there (``book_splitter.save_chapters_to_files``), and
    combine overwrites it with the TRANSLATION. The metered-API path reaches combine
    through ``translate_book``'s stage chain; the workers path never did (there was no
    ``combine`` command at all), so on a subagent/headless project that file kept the
    English split output — or, after a redo, the *previous* translation — forever.

    ``translate-commit`` now recombines each chapter as it completes; this command is
    the explicit repair/backfill verb: for projects translated before that landed, for
    a chapter whose recombine failed, and after any out-of-band chunk edit.

    Only **fully-translated** chapters are written (a partial chapter has nothing
    coherent to stitch) — the same rule ``stage_combine`` applies. The EPUB does not
    read these files (``build_epub_from_chunks`` recombines into a temp dir), but the
    web reader does: it re-derives paragraph breaks and ``[IMAGE:...]`` placement from
    them, so this is what keeps the reader consistent with the chunks.
    """
    project_dir = state.resolve_project_dir(project)
    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        return {"error": "no chunks yet — translate first", "combined": [], "skipped": []}

    if str(state.REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(state.REPO_ROOT))
    from scripts.translate_book import discover_chapters, parse_chapter_range
    from src.corrections_apply import recombine_chapter
    from src.utils.file_io import load_chunk

    all_discovered = discover_chapters(chunks_dir)
    discovered = all_discovered
    if chapters:
        try:
            requested = parse_chapter_range(chapters)
        except (ValueError, TypeError) as exc:
            return {"error": f"invalid chapters value {chapters!r}: {exc}",
                    "combined": [], "skipped": []}
        discovered = {k: v for k, v in all_discovered.items() if k in requested}
        if not discovered:
            return {
                "combined": [],
                "skipped": [],
                "chapters": chapters,
                "available_chapters": sorted(all_discovered.keys()),
                "note": f"no matching chapters for chapters {chapters}",
            }

    combined: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for chapter_id in sorted(discovered):
        chunk_paths = discovered[chapter_id]
        try:
            chunks = [load_chunk(cp) for cp in chunk_paths]
        except (OSError, ValueError) as exc:
            failed.append({"chapter_id": chapter_id,
                           "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        if not all(c.has_translation for c in chunks):
            skipped.append({"chapter_id": chapter_id, "reason": "not fully translated"})
            continue
        out_path = project_dir / "chapters" / f"{chapter_id}.txt"
        previous = ""
        if out_path.exists():
            try:
                previous = out_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                previous = ""
        try:
            # Always write, even when the result is byte-identical: `status`'s
            # combine_stale signal is mtime-based, so a "skip the write when
            # unchanged" optimisation would leave the chapter flagged stale forever.
            written = recombine_chapter(project_dir, chapter_id)
        except Exception as exc:  # noqa: BLE001 - one bad chapter must not abort the batch
            failed.append({"chapter_id": chapter_id,
                           "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        text = written.read_text(encoding="utf-8")
        combined.append({
            "chapter_id": chapter_id,
            "path": str(written),
            "chunks": len(chunk_paths),
            "words": len(text.split()),
            "chars": len(text),
            "changed": text != previous,
            "previous_bytes": len(previous.encode("utf-8")),
        })

    n_changed = sum(1 for c in combined if c["changed"])
    instructions = (
        f"Rewrote chapters/<id>.txt from the translated chunks for {len(combined)} chapter(s) "
        f"({n_changed} changed). These files now hold the TRANSLATION — the split step wrote "
        f"the English there. The EPUB builds from chunks/ and is unaffected; the web reader "
        f"reads chapters/*.txt for paragraph breaks and [IMAGE:...] placement, so this is what "
        f"makes the reader consistent."
    )
    if skipped:
        instructions += (f" Skipped {len(skipped)} chapter(s) that are not fully translated — "
                         f"finish translating them, then re-run.")
    if failed:
        instructions += f" {len(failed)} chapter(s) FAILED to combine — see `failed`."

    return {
        "combined": combined,
        "skipped": skipped,
        "failed": failed,
        "chapters_dir": str(project_dir / "chapters"),
        "counts": {
            "combined": len(combined),
            "changed": n_changed,
            "skipped": len(skipped),
            "failed": len(failed),
        },
        "instructions": instructions,
    }


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
    # Source runs the translation never covered. The aligner finds these for free
    # (src/sentence_aligner.py:_coverage_gaps). Headless/subagent paths call this
    # harness align step after each wave; the API path auto-aligns via
    # translate_book.stage_align, which re-emits the translate HARNESS_RESULT with
    # coverage_warnings so last_output.json sees them too. Flattened across
    # chapters because the agent needs to act on them, not go hunting through
    # the per-chapter lists.
    coverage_warnings: list[dict] = []
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
            gaps = result.get("gaps") or []
            aligned.append({
                "chapter_id": chapter_id,
                "es_count": result.get("es_count"),
                "high_confidence_pct": result.get("high_confidence_pct"),
                "coverage": result.get("coverage"),
                "gaps": gaps,
            })
            for gap in gaps:
                coverage_warnings.append({"chapter_id": chapter_id, **gap})

    reader_base = f"http://{reader_host}:{reader_port}/read/{slug}"
    first = aligned[0]["chapter_id"] if aligned else None
    instructions = (
        f"Aligned {len(aligned)} chapter(s). Ensure the reader is running "
        f"(`python web_ui/app.py`), then open reader_first."
        if aligned else
        "No chapters were aligned — translate a chapter set fully first."
    )
    if coverage_warnings:
        # Loud by design: a dropped paragraph reads perfectly in the target language,
        # so nothing downstream — not length, not paragraph counts, not
        # high_confidence_pct — will ever raise it again.
        instructions = (
            f"COVERAGE WARNING: {len(coverage_warnings)} source run(s) have no "
            "translation at all — the translator dropped prose. Report every entry in "
            "coverage_warnings (chapter, chunk, position, sentences, chars) to the user "
            "and re-translate the affected chunks before continuing. " + instructions
        )
    out = {
        "aligned": aligned,
        "skipped": skipped,
        "coverage_warnings": coverage_warnings,
        "alignments_dir": str(align_dir),
        "reader_base": reader_base,
        "reader_first": f"{reader_base}/{first}" if first else None,
        "reader_links": [f"{reader_base}/{a['chapter_id']}" for a in aligned],
        "instructions": instructions,
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


# ── config-set (persist once-per-book skill decisions) ──────────────────────

# Sentinel: this key accepts free text (whitespace-split into argv by
# ``state.headless_extra_flags``). Enum keys keep a frozenset of legal values.
FREE_TEXT = object()

_CONFIG_SET_KEYS = {
    "backend": frozenset({"api", "subagent", "headless"}),
    "footnotes_decision": frozenset({"keep", "drop", "none"}),
    "headless_cli": frozenset({"claude", "cursor"}),
    "headless_prompt_cache": frozenset(state.CACHE_VALUES),
    "headless_extra_flags": FREE_TEXT,
    # One effort key per wave type (headless_effort_judges, …_translate, …).
    **{
        state.effort_config_key(cmd): frozenset(state.EFFORT_VALUES)
        for cmd in state.COMMAND_EFFORT_DEFAULTS
    },
}


def config_set(project: str, *, key: str, value: str) -> dict:
    """Persist one once-per-book skill decision into ``.harness/config.json``.

    Thin wrapper over ``state.load_config`` / ``state.save_config`` so the skill
    can record ``backend``, ``footnotes_decision``, ``headless_cli``,
    ``headless_effort_<type>``, ``headless_prompt_cache``, and
    ``headless_extra_flags`` at decision time and later sessions stop re-asking.
    Unknown keys / values fail closed with a clear error rather than poisoning
    the config.
    """
    from src.harness_guard import HarnessValidationError

    if key not in _CONFIG_SET_KEYS:
        allowed = ", ".join(sorted(_CONFIG_SET_KEYS))
        raise HarnessValidationError(f"unknown config key {key!r}; allowed: {allowed}")
    allowed_values = _CONFIG_SET_KEYS[key]
    if allowed_values is FREE_TEXT:
        # Free-text guard, fails closed: ``--bare`` defeats the subscription
        # preflight (auth becomes strictly ANTHROPIC_API_KEY / apiKeyHelper).
        try:
            tokens = state.split_extra_flags(value)
        except ValueError as exc:
            raise HarnessValidationError(
                f"headless_extra_flags is not parseable ({exc}); check the quoting"
            ) from None
        # Every token is appended verbatim to a child argv, and on Windows that
        # argv goes through the ``claude.CMD`` shim — i.e. ``cmd.exe``, which
        # re-parses &, |, >, ^ and %VAR% regardless of shell=False. Reject
        # anything that is not plainly a flag or a plain value.
        unsafe = state.unsafe_extra_flag_tokens(tokens)
        if unsafe:
            shown = ", ".join(repr(t) for t in unsafe)
            raise HarnessValidationError(
                f"headless_extra_flags contains token(s) that are not a plain "
                f"flag or value: {shown} — these are appended to the headless "
                "CLI's argv, which on Windows is re-parsed by cmd.exe, so shell "
                "metacharacters (& | < > ^ % \") are refused"
            )
        if "--bare" in tokens:
            raise HarnessValidationError(
                "headless_extra_flags must not contain --bare "
                "(its auth is strictly ANTHROPIC_API_KEY/apiKeyHelper — "
                "OAuth and keychain are never read)"
            )
        # Effort is per wave type; a book-wide flag list cannot express that, and
        # the resolver discards any --effort found here rather than honoring it.
        # Reject loudly instead of accepting a setting that would do nothing.
        if any(t == "--effort" or t.startswith("--effort=") for t in tokens):
            keys = ", ".join(
                state.effort_config_key(cmd) for cmd in state.COMMAND_EFFORT_DEFAULTS
            )
            raise HarnessValidationError(
                "headless_extra_flags must not contain --effort (it applies to "
                f"every wave type at once); set one of: {keys} — or pass "
                "--effort on the individual fanout command for a single run"
            )
        stored: object = value
    else:
        if value not in allowed_values:
            allowed = ", ".join(sorted(allowed_values))
            raise HarnessValidationError(
                f"invalid value {value!r} for {key}; allowed: {allowed}"
            )
        stored = value

    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)
    cfg[key] = stored
    state.save_config(project_dir, cfg)
    return {"project": project_dir.name, "key": key, "value": stored, "config": cfg}


def _footnotes_applied(project_dir: Path) -> bool:
    """True once the footnotes stage has written reader notes into the book.

    Keys off ``pipeline_state.json``'s ``footnotes_written`` (set by the footnotes
    stage in ``translate_book.py``), the one signal that survives ``footnotes_apply``
    — the sidecar ``footnotes.json`` is intentionally *not* deleted on apply, so the
    router needs this to know the notes are done rather than merely present.
    """
    pstate = project_dir / "pipeline_state.json"
    if not pstate.exists():
        return False
    try:
        data = json.loads(pstate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("footnotes_written", 0) > 0


def _suggested_reference(
    project_dir: Path,
    cfg: dict,
    artifacts: dict,
    stage: str,
    epubs: list,
    combine_stale: list[str] | None = None,
) -> str:
    """Map status signals → the translate-harness reference file to load next."""
    if not (project_dir / "project.json").exists() and not artifacts.get("source"):
        return "references/setup.md"
    if not artifacts.get("style_guide"):
        # The address-map beat runs BEFORE the style guide (it feeds the guide its
        # forms-of-address summary), so it is only routed to inside this window.
        # Keeping it nested means a mid-flight project that already has a style
        # guide but no map is never re-routed backwards — it keeps its old path.
        if not artifacts.get("address_map") and not cfg.get("address_map_decision"):
            return "references/address-map.md"
        return "references/style-guide.md"
    if not artifacts.get("glossary"):
        return "references/glossary.md"
    if stage == "pre-chunk" or not artifacts.get("chunks"):
        return "references/chunk.md"

    backend = cfg.get("backend")
    footnotes_decision = cfg.get("footnotes_decision")
    has_footnotes = (project_dir / "footnotes.json").exists()

    # SKILL.md router: non-empty combine_stale → epub.md with no stage gate.
    # chapters/*.txt is what combine / the reader / footnotes / reviews consume; repairing
    # drift first is the difference between applying work to current prose vs the previous
    # translation. Check before the partial translate branch — otherwise a mid-book project
    # with completed-but-stale chapters keeps routing to translate-workers/api.
    if combine_stale:
        return "references/epub.md"

    if stage in ("untranslated", "partial"):
        if backend == "api":
            return "references/translate-api.md"
        # workers path, or backend not yet chosen (choice lives in translate-workers)
        return "references/translate-workers.md"

    if stage == "fully-translated":
        if (
            has_footnotes
            and footnotes_decision not in ("drop", "none")
            and not _footnotes_applied(project_dir)
        ):
            return "references/footnotes.md"
        if not epubs:
            return "references/epub.md"
        return "references/reviews.md"

    return "references/reviews.md"


# ── status (resume at a glance) ─────────────────────────────────────────────

def status(project: str) -> dict:
    """Report a project's pipeline progress at a glance (read-only; no spend).

    Answers "where is this project and what's left?" on a RESUME without hand-rolling
    a loop over ``chunks/*.json`` (friction-log #11). Reports which pipeline artifacts
    exist, per-chapter translated-vs-pending counts (via ``chunk.has_translation``),
    the saved spawn plan, and whether the book is single-chunk-per-chapter (so the
    spawn-mode choice is moot). ``stage`` is the one-word summary the agent leads with.
    Also echoes ``backend`` / ``footnotes_decision`` / ``headless_cli`` /
    ``headless_effort`` and a ``suggested_reference`` so the skill router does not
    re-derive the mapping in prose.
    """
    project_dir = state.resolve_project_dir(project)
    cfg = state.load_config(project_dir)

    artifacts = {
        "source": (project_dir / "source.txt").exists(),
        "chapters": (project_dir / "chapters").exists(),
        "style_guide": (project_dir / "style.json").exists(),
        "glossary": (project_dir / "glossary.json").exists(),
        "address_map": (project_dir / "address_map.json").exists(),
        "difficulty": (project_dir / "difficulty.json").exists(),
        "chunks": (project_dir / "chunks").exists(),
    }
    epubs = sorted(p.name for p in project_dir.glob("*.epub"))
    spawn_plan, _ = _spawn_plan_from_cfg(cfg)
    backend = cfg.get("backend")
    footnotes_decision = cfg.get("footnotes_decision")
    # Resolve effort per wave type so a book carrying --effort medium is not
    # indistinguishable from one that isn't (status is what the skill router runs).
    effort_config: dict[str, str] = {}
    effort_resolved: dict[str, str | None] = {}
    for cmd_name in state.COMMAND_EFFORT_DEFAULTS:
        _argv, level, _src = state.resolve_headless_argv(cfg, command=cmd_name)
        effort_resolved[cmd_name] = level
        raw = cfg.get(state.effort_config_key(cmd_name))
        effort_config[cmd_name] = raw if isinstance(raw, str) else "auto"
    _effort_in_flags, residual_flags = state._split_effort_from_flags(
        state.headless_extra_flags(cfg)
    )
    base = {
        "project": project_dir.name,
        "artifacts": artifacts,
        "epubs": epubs,
        "spawn_plan": spawn_plan,
        "worker_model": cfg.get("worker_model") or "sonnet",
        "run_id": cfg.get("run_id"),
        "backend": backend,
        "footnotes_decision": footnotes_decision,
        "address_map_decision": cfg.get("address_map_decision"),
        "headless_cli": cfg.get("headless_cli") or "claude",
        "headless_effort": {
            "config": effort_config,
            "resolved": effort_resolved,
            "extra_flags": residual_flags,
        },
        "headless_prompt_cache": cfg.get("headless_prompt_cache") or "auto",
    }

    chunks_dir = project_dir / "chunks"
    if not chunks_dir.exists():
        stage = "pre-chunk"
        return {
            **base,
            "stage": stage,
            "spawn_mode_moot": None,
            "totals": {},
            "pending_chapters": [],
            "combine_stale": [],
            "chapters": [],
            "next": "no chunks yet — run `difficulty` then `chunk` before translating.",
            "suggested_reference": _suggested_reference(
                project_dir, cfg, artifacts, stage, epubs
            ),
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
    # Fully-translated chapters whose chapters/<id>.txt is missing or older than its
    # chunks. That file always LOOKS fine (plausible prose — either the English split
    # output or a previous translation), so nothing else surfaces the drift; the web
    # reader silently mis-derives paragraph breaks and image placement from it.
    # Kept as a top-level list rather than two booleans per chapter row: the per-chapter
    # array is already the bulkiest part of this output on a long book.
    combine_stale: list[str] = []
    for chapter_id in sorted(discovered):
        cps = discovered[chapter_id]
        max_per_chapter = max(max_per_chapter, len(cps))
        n_trans = sum(1 for cp in cps if load_chunk(cp).has_translation)
        total_chunks += len(cps)
        translated_chunks += n_trans
        complete = n_trans == len(cps)
        if not complete:
            pending_chapters.append(chapter_id)
        else:
            # Only complete chapters are eligible — `combine` refuses partial ones.
            ch_txt = project_dir / "chapters" / f"{chapter_id}.txt"
            try:
                stale = (not ch_txt.exists()) or (
                    ch_txt.stat().st_mtime < max(cp.stat().st_mtime for cp in cps)
                )
            except OSError:
                stale = True  # unreadable ⇒ assume stale; never crash status
            if stale:
                combine_stale.append(chapter_id)
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
        if combine_stale:
            nxt = (
                f"chapters/*.txt is stale for {len(combine_stale)} chapter(s) — run `combine` "
                f"(the web reader reads that file for paragraph breaks and image placement); "
                f"then " + nxt
            )
    elif stage in ("untranslated", "partial"):
        nxt = ("run `translate-prepare` (optionally --chapters) to render prompts for "
               "the pending chunks, then Task-spawn or `translate-fanout`, then "
               "`translate-commit`.")
        # A partial book still has fully-translated chapters, and a mid-book redo puts
        # the book BACK into `partial` — so this is exactly when stale chapters/*.txt
        # is most likely and least visible. Reporting it only in the fully-translated
        # branch hides it for the whole re-translation window.
        if combine_stale:
            nxt += (
                f" Separately: chapters/*.txt is stale for {len(combine_stale)} "
                f"already-complete chapter(s) (`combine_stale`) — run "
                f"`combine --chapters <ids>`; the web reader reads that file for "
                f"paragraph breaks and image placement."
            )
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
            "combine_stale_chapters": len(combine_stale),
        },
        "pending_chapters": pending_chapters,
        "combine_stale": combine_stale,
        "chapters": chapters_out,
        "next": nxt,
        "suggested_reference": _suggested_reference(
            project_dir, cfg, artifacts, stage, epubs, combine_stale
        ),
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
        "config": "persisted config dict (target_language, locale, provider, model, title, author, language_code, always_include_dialogue, always_include_image_instructions)",
        "chapters": "list of written chapter file stems (e.g. 'chapter_01')",
        "chapter_count": "number of sections written to chapters/",
        "pattern_used": "the chapter pattern the split actually ran on (an 'auto' request is resolved to a concrete pattern here)",
        "dropped": "list of {label, reason} for boilerplate stripped at split (Contents, Title Page, ...)",
        "footnotes_detected": "count of Gutenberg footnotes found at ingest (0 if none, or on the local source.txt path which skips detection)",
        "footnotes_mode": "how footnotes were handled: 'import' (default; kept as [FOOTNOTE:N] tokens + footnotes.json) or 'drop'; null when none detected. On 'import' with footnotes_detected>0, prompt keep/drop and run `footnotes drop` to discard",
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
        "carryforward_path": "path to write glossary terms surfaced while drafting: [{term, why, type_guess}]",
        "address_summary_loaded": "whether an approved address map supplied the FORMS OF ADDRESS section",
        "instructions": "what to do next",
    },
    "style-guide commit": {
        "style_path": "path to the written style.json",
        "chars": "character length of the committed style guide",
    },
    "glossary prepare": {
        "prompt_path": "path to the glossary-proposal prompt to read",
        "candidate_count": "number of extracted candidate terms (including carry-forwards)",
        "carryforward_count": "terms injected from the style-guide beat's hand-off",
        "style_guide_loaded": "whether style.json was fed into the prompt",
        "draft_path": "path to write the drafted proposals JSON to",
        "instructions": "what to do next",
    },
    "glossary commit": {
        "glossary_path": "path to the written glossary.json",
        "term_count": "number of committed terms",
        "warnings": "advisory, non-blocking flags for the approval gate: accent-stripping, REVIEW: alternatives-convention violations, and stale address-map cast names; empty when clean",
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
        "manifest": "list of work entries: {chunk_id, chapter_id, chunk_path, prompt_path, draft_path, source_word_count, optional preamble_path/body_path}",
        "manifest_path": "path to the written manifest.json",
        "worker_model": "model each worker should be pinned to",
        "spawn_plan": "dict {parallelism, window, batch_size}",
        "spawn_mode_moot": "True when every in-scope chapter is a single chunk (skip the spawn-mode question)",
        "usage_summary": "dict {chunks, source_words, worker_model, parallelism, window, batch_size, spawn_mode_moot}",
        "rescued_prior_drafts": "count of uncommitted drafts carried over from a prior prepare or discovered on disk (even out of the current --chapters scope)",
        "chapters": "the --chapters scope echoed back ('all' when omitted)",
        "instructions": "what to do next (Task spawn or translate-fanout, then translate-commit)",
        "error": "present only on failure (e.g. no chunks, bad --chapters)",
        "note": "present when no chapters matched the requested scope",
        "available_chapters": "present with note: chapter ids that do exist",
    },
    "translate-fanout": {
        "wrote": "list of chunk_ids whose draft_path was written this run",
        "failed": "list of {chunk_id, error} for headless CLI failures",
        "skipped_existing_draft": "list of chunk_ids that already had a non-empty draft",
        "worker_model": "model passed to the headless CLI --model",
        "concurrency": "wave width used",
        "cwd": "neutral cwd used for headless CLI (avoids project CLAUDE.md)",
        "cli": "headless CLI used (claude|cursor)",
        "warning": "optional non-fatal notice (e.g. Cursor Claude-model alias)",
        "counts": "dict {wrote, failed, skipped, todo}",
        "instructions": "what to do next (translate-commit, or re-fanout failed)",
        "error": "present only on failure (e.g. no manifest)",
    },
    "translate-commit": {
        "committed": "list of chunk_ids stamped this run",
        "failed": "list of {chunk_id, problems} that did not pass the guard",
        "missing": "list of chunk_ids whose draft file was absent",
        "skipped_already_translated": "list of chunk_ids already translated (idempotent skip)",
        "waived": "map of chunk_id -> waived guard problems (via --allow-problem)",
        "recombined": (
            "chapter_ids whose chapters/<id>.txt was rewritten from the translated chunks "
            "because that chapter became FULLY translated in this run. The workers path "
            "never wrote that file before; the web reader reads it for paragraph breaks and "
            "[IMAGE:...] placement"
        ),
        "combine_failed": (
            "list of {chapter_id, error} where the recombine failed — the chunks ARE "
            "committed, but chapters/*.txt is stale for those chapters; run "
            "`combine --chapters <ids>`"
        ),
        "counts": "dict {committed, failed, missing, skipped, evaluated, recombined, combine_failed}",
        "instructions": "what to do next (re-spawn failed/missing, or proceed)",
        "error": "present only on failure (e.g. no manifest); recombined/combine_failed are absent on that path",
    },
    "retranslate": {
        "dry_run": "True when --yes was omitted — the command is a PREVIEW and changed nothing on disk",
        "scope": "dict {chapters, chunk_ids} — exactly one is non-null",
        "chapters": "chapter_ids touched by the scope",
        "cleared": "chunk_ids whose translation was cleared (would be cleared, in preview)",
        "already_untranslated": "in-scope chunk_ids that had no translation to clear",
        "unreadable": "in-scope chunk_ids whose JSON could not be loaded (OSError/ValueError) — not the same as already_untranslated",
        "cleared_review_data": "chunk_ids whose in-chunk review_data was cleared (its offsets pointed into the deleted prose)",
        "stale_drafts": (
            "list of {chunk_id, path, mtime, bytes} for worker drafts found on disk. A "
            "non-empty list IS the silent-no-op hazard: without deleting these, "
            "translate-fanout skips the chunk and translate-commit re-lands the OLD prose "
            "reporting success"
        ),
        "drafts_deleted": "list of {chunk_id, path, mtime, bytes} actually removed (empty in preview)",
        "prompts_deleted": "count of <chunk_id>.prompt.txt removed (stale context; prepare rewrites them)",
        "bodies_deleted": "count of <chunk_id>.body.txt removed",
        "archived": "True when a snapshot was written",
        "archive": (
            "dict {requested, dir, manifest, files, total_bytes, existing_archives}. There is "
            "NO restore command — restoring is a manual copy (see references/retranslate.md). "
            "The archive is a PRECONDITION: if it fails, nothing is cleared"
        ),
        "downstream": (
            "read-only census of artifacts that will describe the replaced translation "
            "(annotations, corrections_applied, reviewed, alignments, epubs, chapters_txt, "
            "chunk_edits, retranslations, evaluations). NEVER mutated by this command"
        ),
        "warnings": "human-readable warnings to relay to the user verbatim",
        "counts": "dict {chunks, chapters, drafts, annotations, reviewed, epubs}",
        "instructions": "what to do next (preview: STOP and ask; execute: prepare, then PROBE one chunk)",
        "note": "present when --chapters matched nothing",
        "available_chapters": "all chapter ids discovered (present with note)",
        "available_chunk_ids": "all chunk ids discovered (present when --chunk-ids had an unknown id)",
        "error": "present only on failure (no chunks / both scope flags / invalid --chapters / archive failure)",
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
        "error": "present only on failure (scraped from the wrapped script's last ERROR line, or from a sentinel)",
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
        "error": "present only on failure (scraped from the wrapped script's last ERROR line, or from a sentinel)",
    },
    "translate": {
        "command": "the harness command ('translate')",
        "exit_code": "wrapped-script exit code (0 = ok; non-zero = refused/aborted)",
        "stage": "'translate'",
        "translated": "number of chunks translated this run",
        "chapters_done": "list of chapter_ids fully covered this run",
        "estimated_cost_usd": "estimated spend for this batch",
        "remaining_untranslated": "untranslated chunks left in the book after this batch",
        "coverage_warnings": (
            "list of {chapter_id, chunk_id, position, en_start, en_end, sentences, chars, "
            "preview} — present after the API auto-chain reaches align; source runs with NO "
            "translation (dropped prose). Report and re-translate before reviewing"
        ),
        "note": "present when nothing was translated (already done / no match)",
        "error": "present only on failure (scraped from the wrapped script's last ERROR line, or from a sentinel)",
    },
    "epub": {
        "command": "the harness command ('epub')",
        "exit_code": "wrapped-script exit code (0 = ok)",
        "stage": "'epub'",
        "path": "path to the built .epub",
        "size_kb": "EPUB size in KB",
        "included": "list of translated chapter_ids included",
        "skipped": "list of untranslated/partial chapter_ids skipped",
        "error": "present only on failure (scraped from the wrapped script's last ERROR line, or from a sentinel)",
    },
    "footnotes translate": {
        "command": "the harness command ('footnotes')",
        "exit_code": "wrapped-script exit code (0 = ok; 2 = refused without --yes)",
        "stage": "'footnotes-translate'",
        "total": "footnote notes in footnotes.json",
        "translated": "notes now carrying a translated_body",
        "pending": "notes still untranslated (0 = all done)",
        "note": "present when there was nothing to translate (no footnotes.json)",
        "error": "present only on failure (scraped from the wrapped script's last ERROR line)",
    },
    "footnotes apply": {
        "command": "the harness command ('footnotes')",
        "exit_code": "wrapped-script exit code (0 = ok)",
        "stage": "'footnotes-apply'",
        "footnotes_written": "reader-footnote annotations written across chapters this run",
        "epub_path": "path to the rebuilt EPUB (present when any footnote was applied)",
        "note": "present when there was nothing to apply (no footnotes.json)",
        "error": "present only on failure (scraped from the wrapped script's last ERROR line)",
    },
    "footnotes drop": {
        "command": "the harness command ('footnotes')",
        "exit_code": "always 0 (local operation)",
        "stage": "'footnotes-drop'",
        "tokens_stripped": "count of [FOOTNOTE:N] tokens removed from source.txt + chapters",
        "files_cleaned": "number of files a token was stripped from",
        "sidecar_removed": "whether footnotes.json existed and was deleted",
    },
    "align": {
        "aligned": "list of {chapter_id, es_count, high_confidence_pct, coverage, gaps}",
        "skipped": "list of {chapter_id, reason} not aligned",
        "coverage_warnings": (
            "list of {chapter_id, chunk_id, position, en_start, en_end, sentences, chars, "
            "preview} — source runs with NO translation (dropped prose). position is "
            "head|interior|tail|full relative to the chunk; a tail gap on a non-final chunk is a "
            "chunk-seam drop; full means the entire chunk was unclaimed. Report these and "
            "re-translate the chunk; no other metric sees them"
        ),
        "alignments_dir": "directory the alignment JSON was written to",
        "reader_base": "base reader URL for this project",
        "reader_first": "reader URL for the first newly-aligned chapter, or null",
        "reader_links": "reader URLs for every aligned chapter",
        "instructions": "what to do next",
        "error": "present only on a per-chapter aligner failure",
    },
    "combine": {
        "combined": (
            "list of {chapter_id, path, chunks, words, chars, changed, previous_bytes} — "
            "chapters/<id>.txt rewritten from the translated chunks. Only FULLY translated "
            "chapters are written. That file is dual-purpose: the split step puts the ENGLISH "
            "there and combine overwrites it with the TRANSLATION, so a backfill flips the "
            "content in place (changed=True says it did)"
        ),
        "skipped": "list of {chapter_id, reason} — not fully translated, so nothing to stitch",
        "failed": (
            "list of {chapter_id, error} — e.g. combine_chunks rejecting a chunk that carries "
            "overlap. One bad chapter never aborts the batch"
        ),
        "chapters_dir": "directory the chapter .txt files were written to",
        "counts": "dict {combined, changed, skipped, failed}",
        "instructions": "what to do next",
        "note": "present when --chapters matched nothing",
        "available_chapters": "all chapter ids discovered (present with note)",
        "error": "present only on failure (no chunks yet / invalid --chapters)",
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
        "backend": "persisted translation backend (api | subagent | headless), or null if unset",
        "footnotes_decision": "persisted footnotes choice (keep | drop | none), or null if unset",
        "address_map_decision": "address-map beat outcome (built | skipped | no_dialogue), or null if not yet offered",
        "headless_cli": "headless launcher family (claude | cursor); default claude",
        "stage": "one-word progress: pre-chunk | untranslated | partial | fully-translated",
        "spawn_mode_moot": "True if one chunk per chapter (else False; null pre-chunk)",
        "totals": "dict {chapters, complete_chapters, total_chunks, translated_chunks, pending_chunks, combine_stale_chapters}",
        "pending_chapters": "list of chapter_ids not yet fully translated",
        "combine_stale": (
            "fully-translated chapter_ids whose chapters/<id>.txt is missing or older than "
            "its chunks — the reader will show stale paragraph/image structure. Run `combine`. "
            "(translate-commit refreshes these automatically going forward; chapters edited in "
            "the web UI's chunk editor can also land here — that is real drift, not a false alarm). "
            "Only COMPLETE chapters are eligible: a chapter mid-redo holds the previous "
            "translation without appearing here — `retranslate` warns about that window"
        ),
        "chapters": "list of {chapter_id, chunks, translated, complete}",
        "next": "suggested next command",
        "suggested_reference": "translate-harness references/*.md path the skill should Read next",
    },
    "config-set": {
        "project": "project slug",
        "key": "config key that was set",
        "value": "value that was persisted",
        "config": "full .harness/config.json after the write",
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
