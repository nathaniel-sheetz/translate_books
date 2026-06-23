#!/usr/bin/env python3
"""Non-interactive CLI for the translate-harness skill.

The skill (``.claude/skills/translate-harness/SKILL.md``) drives the book-translation
pipeline as a conversation: the agent *is* the thinking-mode LLM (it drafts the style
guide and glossary in-chat), and this CLI is the deterministic surface it calls between
drafts. Every command is non-interactive — it never calls ``input()`` and so never
deadlocks an agent.

Each command maps to one ``src/harness/flow.py`` function. Commands that produce data the
agent should relay print a JSON object to stdout; commands that wrap a deterministic /
paid stage (``chunk``/``cost``/``translate``/``epub``) inherit the wrapped CLI's output
and exit with its code.

Cost-gate safety (unchanged from the wrapped CLI):
  * ``chunk`` and ``cost`` always pass ``--cost-only`` — they physically cannot spend.
  * ``translate`` fails closed unless ``--yes`` is given, which the skill only supplies
    after a separate-turn user approval of the estimate.

Examples:
    python scripts/harness.py setup --project understood-betsy --target-lang Spanish
    python scripts/harness.py style-guide prepare-questions --project understood-betsy
    python scripts/harness.py glossary commit --project understood-betsy
    python scripts/harness.py chunk --project understood-betsy --size 1500
    python scripts/harness.py translate --project understood-betsy --yes --model claude-sonnet-4-6
"""

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# urllib3/chardet version-mismatch noise that ``requests`` emits at import time. It forced the
# agent to pipe harness output through ``grep``, which then mangled the UTF-8 JSON (friction-log
# #4). Filter it before the import below triggers it.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.harness import flow, state
from src.harness_guard import HarnessValidationError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_project(p):
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")

    # setup -----------------------------------------------------------------
    sp = sub.add_parser("setup", help="Create project, persist config, run ingest + split")
    add_project(sp)
    sp.add_argument("--url", default="", help="Gutenberg URL (omit if source.txt is in place)")
    sp.add_argument("--chapter-pattern", default="roman", choices=["roman", "numeric", "custom"])
    sp.add_argument("--custom-regex", default=None)
    sp.add_argument("--target-lang", dest="target_language", default=None)
    sp.add_argument("--locale", default=None)
    sp.add_argument("--provider", default=None)
    sp.add_argument("--model", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--author", default=None)
    sp.add_argument("--language-code", dest="language_code", default=None)
    sp.add_argument("--front-matter-title", dest="front_matter_titles", action="append",
                    help="Heading to force-tag as front matter (repeatable)")
    sp.add_argument("--back-matter-title", dest="back_matter_titles", action="append",
                    help="Heading to force-tag as back matter (repeatable)")
    sp.add_argument("--min-chapter-size", dest="min_chapter_size", type=int, default=None,
                    help="Min chars for a real chapter; filters short false matches "
                         "(default 100; raise to ~500 to drop stray front-matter lines)")

    def add_split_opts(p):
        """Shared chapter-split controls for the split-preview / split commands."""
        p.add_argument("--chapter-pattern", default="roman",
                       choices=["roman", "numeric", "custom"])
        p.add_argument("--custom-regex", default=None)
        p.add_argument("--min-chapter-size", dest="min_chapter_size", type=int, default=None,
                       help="Min chars for a real chapter (default 100; raise to ~500 to "
                            "drop stray front-matter lines)")
        p.add_argument("--front-matter-title", dest="front_matter_titles", action="append",
                       help="Heading to force-tag as front matter (repeatable)")
        p.add_argument("--back-matter-title", dest="back_matter_titles", action="append",
                       help="Heading to force-tag as back matter (repeatable)")
        p.add_argument("--no-auto-front-matter", dest="auto_detect_front_matter",
                       action="store_false", help="Disable built-in front-matter keyword detection")
        p.add_argument("--no-auto-back-matter", dest="auto_detect_back_matter",
                       action="store_false", help="Disable built-in back-matter keyword detection")

    # split-preview / split (chapter-split review beat) ---------------------
    spp = sub.add_parser("split-preview",
                         help="Dry-run a chapter split and print the detected sections (no files)")
    add_project(spp)
    add_split_opts(spp)

    swp = sub.add_parser("split",
                         help="(Re)write chapters/ from source.txt with the chosen split controls")
    add_project(swp)
    add_split_opts(swp)

    # style-guide <action> --------------------------------------------------
    sg = sub.add_parser("style-guide", help="Style-guide beat (prepare/commit per draft)")
    sg_sub = sg.add_subparsers(dest="action", required=True)
    for action in ("prepare-questions", "prepare-followups", "commit-followups",
                   "prepare-draft", "commit"):
        ap = sg_sub.add_parser(action)
        add_project(ap)
        if action in ("prepare-followups", "prepare-draft"):
            ap.add_argument("--answers", default=None, help="Answers JSON (default: .harness/style_answers.json)")
        if action in ("commit-followups", "commit"):
            ap.add_argument("--draft", default=None, help="Agent draft file (default: canonical .harness/ path)")

    # glossary <action> -----------------------------------------------------
    gl = sub.add_parser("glossary", help="Glossary beat (prepare/commit)")
    gl_sub = gl.add_subparsers(dest="action", required=True)
    gp = gl_sub.add_parser("prepare")
    add_project(gp)
    gp.add_argument("--max-candidates", type=int, default=200)
    gc = gl_sub.add_parser("commit")
    add_project(gc)
    gc.add_argument("--draft", default=None, help="Proposals JSON (default: .harness/glossary_draft.json)")

    # difficulty ------------------------------------------------------------
    dp = sub.add_parser("difficulty", help="Score difficulty; suggest a chunk target size")
    add_project(dp)

    def add_chapters(p):
        p.add_argument("--chapters", default=None,
                       help="Limit to these chapters, e.g. '1-2' or '3,7,12' (default: all)")

    # chunk / cost / translate / epub --------------------------------------
    cp = sub.add_parser("chunk", help="Chunk at --size and print the cost estimate (no spend)")
    add_project(cp)
    cp.add_argument("--size", type=int, required=True,
                    help="Book-level target words/chunk; also the per-chapter fallback")
    cp.add_argument("--per-chapter", dest="per_chapter", action="store_true",
                    help="Size each chapter by its difficulty.json suggested_target_size "
                         "(run `difficulty` first); --size is the fallback")
    add_chapters(cp)

    cop = sub.add_parser("cost", help="Re-print the cost estimate (no spend)")
    add_project(cop)
    add_chapters(cop)

    tp = sub.add_parser("translate", help="The one paid API step (requires --yes)")
    add_project(tp)
    tp.add_argument("--yes", action="store_true", help="Confirm the approved spend")
    tp.add_argument("--model", default=None)
    tp.add_argument("--provider", default=None)
    add_chapters(tp)

    # translate-prepare / translate-commit (harness subagent backend) -------
    tpp = sub.add_parser("translate-prepare",
                         help="Render per-chunk prompts + manifest for subagent workers (no spend)")
    add_project(tpp)
    add_chapters(tpp)
    tpp.add_argument("--worker-model", dest="worker_model", default=None,
                     help="Model tier to pin workers to (default: sonnet)")
    tpp.add_argument("--parallelism", default=None,
                     choices=["sequential", "chapter", "all"],
                     help="Spawn mode (persisted): sequential | chapter (default) | all")
    tpp.add_argument("--window", type=int, default=None,
                     help="Chapter-parallel window width X (default 8); persisted")

    tcp = sub.add_parser("translate-commit",
                         help="Validate worker drafts and stamp the chunks (idempotent)")
    add_project(tcp)
    tcp.add_argument("--worker-model", dest="worker_model", default=None)

    ep = sub.add_parser("epub", help="Build EPUB from translated chunks")
    add_project(ep)
    ep.add_argument("--title", default=None)
    ep.add_argument("--author", default=None)
    ep.add_argument("--language", default=None)

    # align (reader mode) ---------------------------------------------------
    al = sub.add_parser("align",
                        help="Align translated chapters for the reader; print a reader link")
    add_project(al)
    add_chapters(al)
    al.add_argument("--source-lang-code", dest="source_lang_code", default=None,
                    help="Source language code for alignment (default: en)")
    al.add_argument("--target-lang-code", dest="target_lang_code", default=None,
                    help="Target language code (default: config language_code or es)")
    al.add_argument("--reader-host", dest="reader_host", default="localhost",
                    help="Host for the printed reader link (default: localhost)")
    al.add_argument("--reader-port", dest="reader_port", type=int, default=5000,
                    help="Port for the printed reader link (default: 5000)")

    # show-translation (read-back for review) -------------------------------
    stp = sub.add_parser("show-translation",
                         help="Print source+translation for chapters from chunks/*.json "
                              "(read-only; no spend)")
    add_project(stp)
    add_chapters(stp)
    stp.add_argument("--max-chunks", dest="max_chunks", type=int, default=None,
                     help="Cap total chunks returned (default: all) — keeps a sample small")
    stp.add_argument("--no-source", dest="include_source", action="store_false",
                     help="Omit source_text; return only the translation per chunk")

    # log-event (record a qualitative beat in the run log) ------------------
    lep = sub.add_parser("log-event",
                         help="Append a quality/friction beat to logs/harness_runs.jsonl")
    add_project(lep)
    lep.add_argument("--event", required=True,
                     help="Beat name, e.g. approval | backend | spawn_mode | respawn")
    lep.add_argument("--data", default=None,
                     help="JSON object of fields to record, e.g. "
                          "'{\"beat\":\"glossary\",\"decision\":\"approved_first_pass\"}'")

    return parser


def _dispatch(args: argparse.Namespace):
    """Route a parsed command to its flow function. Returns a dict or an int exit code."""
    cmd = args.command
    if cmd == "setup":
        return flow.setup(
            args.project, url=args.url, chapter_pattern=args.chapter_pattern,
            custom_regex=args.custom_regex, target_language=args.target_language,
            locale=args.locale, provider=args.provider, model=args.model,
            title=args.title, author=args.author, language_code=args.language_code,
            front_matter_titles=args.front_matter_titles,
            back_matter_titles=args.back_matter_titles,
            min_chapter_size=args.min_chapter_size,
        )
    if cmd in ("split-preview", "split"):
        fn = flow.split_preview if cmd == "split-preview" else flow.split_apply
        return fn(
            args.project, pattern_type=args.chapter_pattern,
            custom_regex=args.custom_regex, min_chapter_size=args.min_chapter_size,
            front_matter_titles=args.front_matter_titles,
            back_matter_titles=args.back_matter_titles,
            auto_detect_front_matter=args.auto_detect_front_matter,
            auto_detect_back_matter=args.auto_detect_back_matter,
        )
    if cmd == "style-guide":
        if args.action == "prepare-questions":
            return flow.style_guide_prepare_questions(args.project)
        if args.action == "prepare-followups":
            return flow.style_guide_prepare_followups(args.project, answers=args.answers)
        if args.action == "commit-followups":
            return flow.style_guide_commit_followups(args.project, draft=args.draft)
        if args.action == "prepare-draft":
            return flow.style_guide_prepare_draft(args.project, answers=args.answers)
        if args.action == "commit":
            return flow.style_guide_commit(args.project, draft=args.draft)
    if cmd == "glossary":
        if args.action == "prepare":
            return flow.glossary_prepare(args.project, max_candidates=args.max_candidates)
        if args.action == "commit":
            return flow.glossary_commit(args.project, draft=args.draft)
    if cmd == "difficulty":
        return flow.difficulty(args.project)
    if cmd == "chunk":
        return flow.chunk(args.project, size=args.size, chapters=args.chapters,
                          per_chapter=args.per_chapter)
    if cmd == "cost":
        return flow.cost(args.project, chapters=args.chapters)
    if cmd == "translate":
        return flow.translate(args.project, yes=args.yes, model=args.model,
                              provider=args.provider, chapters=args.chapters)
    if cmd == "translate-prepare":
        return flow.translate_prepare(args.project, chapters=args.chapters,
                                      worker_model=args.worker_model,
                                      parallelism=args.parallelism, window=args.window)
    if cmd == "translate-commit":
        return flow.translate_commit(args.project, worker_model=args.worker_model)
    if cmd == "epub":
        return flow.epub(args.project, title=args.title, author=args.author, language=args.language)
    if cmd == "align":
        return flow.align(args.project, chapters=args.chapters,
                          source_lang_code=args.source_lang_code,
                          target_lang_code=args.target_lang_code,
                          reader_host=args.reader_host, reader_port=args.reader_port)
    if cmd == "show-translation":
        return flow.show_translation(args.project, chapters=args.chapters,
                                     max_chunks=args.max_chunks,
                                     include_source=args.include_source)
    if cmd == "log-event":
        return flow.log_event(args.project, event=args.event, data=args.data)
    raise SystemExit(f"unknown command: {cmd}")


def _write_output_artifact(args: argparse.Namespace, result: dict) -> None:
    """Mirror a command's JSON result to ``.harness/last_output.json`` (UTF-8).

    The agent reads this file instead of capturing stdout, sidestepping Windows console
    encoding entirely (friction-log #4). Best-effort: the artifact must never break a command,
    so any resolution/write failure is swallowed.
    """
    project = getattr(args, "project", None)
    if not project:
        return
    try:
        project_dir = state.resolve_project_dir(project)
        state.ensure_harness_dir(project_dir)
        out = state.harness_dir(project_dir) / "last_output.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"OUTPUT_JSON: {out}", file=sys.stderr)
    except Exception:  # noqa: BLE001 - the artifact is a convenience, never a hard dependency
        pass


# Result keys worth carrying into the run-log timeline — a small whitelist so a
# "command" event stays a compact summary (counts/sizes), never the full payload.
_RESULT_SUMMARY_KEYS = (
    "chapter_count", "source_words", "section_count", "candidate_count",
    "style_guide_loaded", "term_count", "chars", "book_difficulty",
    "suggested_target_size", "usage_summary", "spawn_plan", "counts",
    "rescued_prior_drafts", "shown_chunks", "total_chunks", "error",
)


def _log_command(args: argparse.Namespace, *, status: str, duration: float,
                 result: dict | None = None) -> None:
    """Append one ``command`` event to the run log. Best-effort: never raises.

    Fires on every outcome path in ``main()`` (success, wrapped-CLI exit code, or
    a caught error) so the log is a complete timeline of the run. ``log-event``
    is excluded — it writes its own beat and would otherwise double-log.
    """
    cmd = getattr(args, "command", None)
    if cmd in (None, "log-event"):
        return
    try:
        from src.utils.run_logger import log_run_event

        action = getattr(args, "action", None)
        label = f"{cmd} {action}" if action else cmd

        project = getattr(args, "project", None)
        run_id = None
        slug = None
        if project:
            try:
                project_dir = state.resolve_project_dir(project)
                slug = project_dir.name
                run_id = state.ensure_run_id(project_dir)
            except Exception:  # noqa: BLE001 - never let resolution break logging
                slug = project

        fields = {"cmd": label, "status": status, "dur_s": round(duration, 3)}
        if isinstance(result, dict):
            for key in _RESULT_SUMMARY_KEYS:
                if key in result:
                    fields[key] = result[key]

        log_run_event(run_id=run_id, project=slug, event="command", **fields)
    except Exception:  # noqa: BLE001 - logging is a convenience, never a dependency
        pass


def main() -> None:
    # Force UTF-8 so accented/curly-quote JSON survives a cp1252 Windows console (friction-log
    # #4). Guarded because pytest replaces stdout/stderr with non-reconfigurable captures.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    args = _build_parser().parse_args()
    started = time.monotonic()
    try:
        result = _dispatch(args)
    except HarnessValidationError as e:
        _log_command(args, status="validation_error", duration=time.monotonic() - started)
        print(f"VALIDATION ERROR — fix every issue below and re-draft:\n{e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        _log_command(args, status="parse_error", duration=time.monotonic() - started)
        print(f"DRAFT PARSE ERROR — fix the JSON/format and re-run:\n{e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        _log_command(args, status="not_found", duration=time.monotonic() - started)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        _log_command(args, status="unexpected_error", duration=time.monotonic() - started)
        raise

    elapsed = time.monotonic() - started
    if isinstance(result, int):
        # chunk/cost/translate/epub wrap a subprocess and return its exit code; the
        # dollar/progress detail is the wrapped CLI's own stdout, so log only the outcome.
        _log_command(args, status=("ok" if result == 0 else f"exit_{result}"), duration=elapsed)
        sys.exit(result)
    _log_command(args, status="ok", duration=elapsed, result=result)
    _write_output_artifact(args, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
