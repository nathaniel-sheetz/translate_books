#!/usr/bin/env python3
"""Non-interactive CLI for the translate-harness skill.

The skill (``.claude/skills/translate-harness/SKILL.md``) drives the book-translation
pipeline as a conversation: the agent *is* the thinking-mode LLM (it drafts the style
guide and glossary in-chat), and this CLI is the deterministic surface it calls between
drafts. Every command is non-interactive — it never calls ``input()`` and so never
deadlocks an agent.

Each command maps to one ``src/harness/flow.py`` function. Commands that produce data the
agent should relay print a JSON object to stdout. Commands that wrap a deterministic /
paid stage (``chunk``/``cost``/``translate``/``epub``) stream the wrapped CLI's live
output and exit with its code, but ALSO mirror a fresh structured result to
``last_output.json`` (friction-log #18 — they used to leave the previous command's result
in place). Per-key documentation lives in ``.harness/last_output_schema.json``; the
payload carries ``_schema_path`` + ``_schema_keys`` (or inlines ``_schema`` on
``--schema`` / errors) so the agent never has to guess field names without paying the
schema on every Read (friction-log #19 / bambi #4, §3b).

Cost-gate safety (unchanged from the wrapped CLI):
  * ``chunk`` and ``cost`` always pass ``--cost-only`` — they physically cannot spend.
  * ``translate`` fails closed unless ``--yes`` is given, which the skill only supplies
    after a separate-turn user approval of the estimate.

Examples:
    python scripts/harness.py setup --project understood-betsy --target-lang Spanish
    python scripts/harness.py style-guide prepare-questions --project understood-betsy
    python scripts/harness.py glossary commit --project understood-betsy
    python scripts/harness.py chunk --project understood-betsy --size 1500
    python scripts/harness.py translate --project understood-betsy --yes --model claude-sonnet-5
"""

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

# Force UTF-8 so accented/curly-quote JSON survives a cp1252/cp437 Windows console
# (friction-log #4). Module-top so import-time output is covered too. Guarded because
# pytest replaces stdout/stderr with non-reconfigurable captures.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# urllib3/chardet version-mismatch noise that ``requests`` emits at import time. It forced the
# agent to pipe harness output through ``grep``, which then mangled the UTF-8 JSON (friction-log
# #4). Filter it before the import below triggers it.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.harness import flow, state
from src.harness_guard import HarnessValidationError


def _chapter_pattern_choices() -> list[str]:
    """All selectable ``--chapter-pattern`` values: ``auto`` (detect from the
    text), every named pattern in ``split_patterns.json``, and ``custom`` (with
    ``--custom-regex``). Derived from the JSON so a pattern that exists there is
    never unreachable from the CLI (friction-log #1 — ``chapter_roman_titled``
    was defined but not exposed)."""
    from src.book_splitter import get_pattern_names
    return ["auto", *get_pattern_names(), "custom"]


_CHAPTER_PATTERN_HELP = (
    "How to detect chapter headings (default: auto). 'auto' picks the best-fit "
    "pattern from the source text; named patterns include roman / numeric and "
    "the titled variants chapter_roman_titled / chapter_numeric_titled "
    "(e.g. 'CHAPTER I. WATHO.'); 'custom' uses --custom-regex."
)


def _positive_int(value: str) -> int:
    """argparse type: integers >= 1."""
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_project(p):
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")

    # setup -----------------------------------------------------------------
    sp = sub.add_parser("setup", help="Create project, persist config, run ingest + split")
    sp.add_argument("--project", default=None,
                    help="Project id (under projects/) or path; omit to name the "
                         "folder from --title (collisions get a -2, -3, ... suffix)")
    sp.add_argument("--url", default="", help="Gutenberg URL (omit if source.txt is in place)")
    sp.add_argument("--chapter-pattern", default="auto",
                    choices=_chapter_pattern_choices(), help=_CHAPTER_PATTERN_HELP)
    sp.add_argument("--custom-regex", default=None)
    sp.add_argument("--target-lang", dest="target_language", default=None)
    sp.add_argument("--locale", default=None)
    sp.add_argument("--provider", default=None)
    sp.add_argument("--model", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--author", default=None)
    sp.add_argument("--language-code", dest="language_code", default=None)
    sp.add_argument("--always-dialogue", dest="always_include_dialogue",
                    action=argparse.BooleanOptionalAction, default=None,
                    help="Put the DIALOGUE FORMATTING block on every chunk (not just "
                         "dialogue-bearing ones) so it caches in the fixed prompt prefix. "
                         "Per-book; absent means auto (on when any chunk has dialogue). "
                         "Use --no-always-dialogue to force off. Also settable later "
                         "with `config-set --key always_include_dialogue`.")
    sp.add_argument("--always-images", dest="always_include_image_instructions",
                    action=argparse.BooleanOptionalAction, default=None,
                    help="Put the image-placeholder instruction on every chunk so it "
                         "caches in the fixed prompt prefix. Per-book; absent means auto "
                         "(on when any chunk has [IMAGE:...]). Use --no-always-images to "
                         "force off. Also settable later with `config-set --key "
                         "always_include_image_instructions`.")
    sp.add_argument("--front-matter-title", dest="front_matter_titles", action="append",
                    help="Heading to force-tag as front matter (repeatable)")
    sp.add_argument("--back-matter-title", dest="back_matter_titles", action="append",
                    help="Heading to force-tag as back matter (repeatable)")
    sp.add_argument("--min-chapter-size", dest="min_chapter_size", type=int, default=None,
                    help="Min chars for a real chapter; filters short false matches "
                         "(default 100; raise to ~500 to drop stray front-matter lines)")
    sp.add_argument("--no-auto-strip", dest="auto_strip_boilerplate",
                    action="store_false",
                    help="Keep navigation/boilerplate (Contents, Title Page, ...) "
                         "instead of stripping it (default: strip)")
    sp.add_argument("--footnotes", dest="footnotes", choices=["import", "drop"],
                    default="import",
                    help="Gutenberg footnote handling at ingest (URL path only): "
                         "'import' (default) captures them as translatable [FOOTNOTE:N] "
                         "tokens + footnotes.json; 'drop' removes them. Detected either way "
                         "and reported as footnotes_detected/footnotes_mode.")

    def add_split_opts(p):
        """Shared chapter-split controls for the split-preview / split commands."""
        p.add_argument("--chapter-pattern", default="auto",
                       choices=_chapter_pattern_choices(), help=_CHAPTER_PATTERN_HELP)
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
        p.add_argument("--no-auto-strip", dest="auto_strip_boilerplate",
                       action="store_false",
                       help="Keep navigation/boilerplate (Contents, Title Page, ...) "
                            "instead of stripping it (default: strip)")

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
    gp.add_argument("--max-candidates", type=int, default=500)
    gc = gl_sub.add_parser("commit")
    add_project(gc)
    gc.add_argument("--draft", default=None, help="Proposals JSON (default: .harness/glossary_draft.json)")

    # address-map <action> --------------------------------------------------
    am = sub.add_parser("address-map",
                        help="Forms-of-address (usted/tú) map beat "
                             "(precheck/prepare/commit/rename/skip)")
    am_sub = am.add_subparsers(dest="action", required=True)
    ampc = am_sub.add_parser("precheck",
                             help="Does this book have dialogue? Gates whether to offer the beat")
    add_project(ampc)
    ams = am_sub.add_parser("skip", help="Record that the user declined the address map")
    add_project(ams)
    amp = am_sub.add_parser("prepare")
    add_project(amp)
    amp.add_argument("--max-chapters", dest="max_chapters", type=_positive_int, default=6,
                     help="Max sampled chapters (spread across the book; default 6, min 1)")
    amc = am_sub.add_parser("commit")
    add_project(amc)
    amc.add_argument("--draft", default=None, help="Map JSON (default: .harness/address_map_draft.json)")
    amr = am_sub.add_parser("rename",
                            help="Apply the approved glossary cast to the committed map "
                                 "(writes a draft; review, then commit)")
    add_project(amr)
    amr.add_argument("--draft", default=None,
                     help="Where to write the renamed map (default: .harness/address_map_draft.json)")

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
    tp.add_argument("--thinking", dest="enable_thinking",
                    action=argparse.BooleanOptionalAction, default=None,
                    help="Enable/disable extended thinking for this API run "
                         "(--thinking / --no-thinking). Absent falls back to the "
                         "TRANSLATE_THINKING env default (off).")
    add_chapters(tp)

    # translate-prepare / translate-commit (harness subagent backend) -------
    tpp = sub.add_parser("translate-prepare",
                         help="Render per-chunk prompts + manifest for subagent workers (no spend)")
    add_project(tpp)
    add_chapters(tpp)
    tpp.add_argument("--worker-model", dest="worker_model", default=None,
                     help="Model tier to pin workers to (default: sonnet)")
    tpp.add_argument("--worker-thinking", dest="worker_thinking",
                     action=argparse.BooleanOptionalAction, default=None,
                     help="Enable extended 'think hard' thinking for workers "
                          "(--worker-thinking / --no-worker-thinking); persisted. "
                          "Absent leaves the saved value unchanged. Only takes effect "
                          "on a thinking-capable worker (fable is always-on and is "
                          "never flagged).")
    tpp.add_argument("--parallelism", default=None,
                     choices=["sequential", "chapter", "all"],
                     help="Spawn mode (persisted): sequential | chapter (default) | all")
    tpp.add_argument("--window", type=int, default=None,
                     help="Chapter-parallel window width X (default 3); persisted")
    tpp.add_argument("--batch-size", dest="batch_size", type=int, default=None,
                     help="Recommended workers to spawn per wave (default 3); persisted. "
                          "Ramp from this; throttle back to ~1 on a 529 (overloaded)")
    tpp.add_argument("--brief", action="store_true",
                     help="Omit the per-entry manifest echo; return chunk_ids + a "
                          "path_template instead (a 22-chunk manifest is ~180 lines of "
                          "mostly-derivable absolute paths). manifest.json on disk is "
                          "unchanged and still carries every field — translate-fanout and "
                          "translate-commit read it from there. Serves both backends: "
                          "--chunk-ids takes the ids, a Task spawn fills the template.")

    tcp = sub.add_parser("translate-commit",
                         help="Validate worker drafts and stamp the chunks (idempotent)")
    add_project(tcp)
    tcp.add_argument("--worker-model", dest="worker_model", default=None)
    tcp.add_argument("--allow-problem", dest="allow_problems", action="append", default=None,
                     metavar="SUBSTRING",
                     help="Waive a known guard false-positive: drop any guard problem whose message "
                          "contains SUBSTRING (case-insensitive) so the chunk commits if no other "
                          "problem remains. Repeatable. Other guards stay enforced; waives are "
                          "reported under `waived` and logged in provenance.")

    tfp = sub.add_parser(
        "translate-fanout",
        help="Headless CLI wave for translate-prepare drafts (opt-in; no Task workers)",
    )
    add_project(tfp)
    tfp.add_argument(
        "--chunk-ids", dest="chunk_ids", default=None,
        help="Comma-separated chunk_ids to run (default: all manifest entries still lacking a draft)",
    )
    tfp.add_argument(
        "--concurrency", type=int, default=None,
        help="Max parallel headless CLI processes per wave (default: spawn_plan.batch_size)",
    )
    tfp.add_argument(
        "--cli", dest="cli", default=None, choices=["claude", "cursor"],
        help="Headless CLI family (default: config headless_cli, else claude)",
    )
    tfp.add_argument(
        "--cli-bin", dest="cli_bin", default=None,
        help="Headless CLI binary override (default: claude or cursor-agent)",
    )
    tfp.add_argument(
        "--claude-bin", dest="claude_bin", default=None,
        help="Back-compat alias for --cli-bin (Claude profile)",
    )
    tfp.add_argument(
        "--effort", dest="effort", default=None,
        choices=["low", "medium", "high", "xhigh", "default"],
        help="Per-run Claude --effort override (default: config "
             "headless_effort_translate, else high; 'default' emits no --effort flag)",
    )
    tfp.add_argument(
        "--prompt-cache", dest="prompt_cache", default=None,
        choices=["auto", "5m", "1h", "off"],
        help="Per-run Claude prompt-cache TTL (default: config headless_prompt_cache / "
             "auto). auto picks 5m|1h|off from job shapes; off disables caching",
    )

    # retranslate (the redo verb) -------------------------------------------
    rtp = sub.add_parser(
        "retranslate",
        help="Clear translations AND their stale worker drafts so a redo actually "
             "re-translates. WITHOUT --yes this is a PREVIEW (nothing is changed).",
    )
    add_project(rtp)
    add_chapters(rtp)
    rtp.add_argument(
        "--chunk-ids", dest="chunk_ids", default=None,
        help="Comma-separated chunk_ids to clear (instead of --chapters)",
    )
    rtp.add_argument(
        "--yes", action="store_true",
        help="Execute the clear (omit for a preview of exactly what changes)",
    )
    rtp.add_argument(
        "--archive", action="store_true",
        help="Snapshot chunks, chapters/, alignments/, the EPUBs and the review sidecars "
             "into archive/<stamp>/ BEFORE clearing. There is no restore command — "
             "restoring is a manual copy.",
    )

    # combine (refresh chapters/*.txt from the translated chunks) ------------
    cbp = sub.add_parser(
        "combine",
        help="Rewrite chapters/<id>.txt from the translated chunks (fully-translated "
             "chapters only; free). translate-commit does this automatically — use this "
             "to repair or backfill an older project.",
    )
    add_project(cbp)
    add_chapters(cbp)

    ep = sub.add_parser("epub", help="Build EPUB from translated chunks")
    add_project(ep)
    ep.add_argument("--title", default=None)
    ep.add_argument("--author", default=None)
    ep.add_argument("--language", default=None)

    # footnotes <action> ----------------------------------------------------
    fn = sub.add_parser("footnotes",
                        help="Reader-footnote beat for imported Gutenberg footnotes "
                             "(translate/apply/drop). Needs `setup --footnotes import`.")
    fn_sub = fn.add_subparsers(dest="action", required=True)
    fnt = fn_sub.add_parser("translate",
                            help="Translate the note bodies on the book's chosen backend "
                                 "(api needs --yes; headless runs a claude -p wave)")
    add_project(fnt)
    fnt.add_argument("--yes", action="store_true",
                     help="Confirm the metered (api-backend) footnote translation "
                          "(approve in a SEPARATE turn first). Ignored on the headless backend.")
    fnt.add_argument("--backend", default="auto",
                     choices=["auto", "api", "headless", "subagent"],
                     help="Translation backend (default: auto = carry forward the chapter "
                          "backend from the run log). 'api' shells to the metered path; "
                          "'headless' runs a headless CLI wave; 'subagent' prints the "
                          "prepare/commit steps (Task workers are orchestrator-driven).")
    fnt.add_argument("--cli", dest="cli", default=None, choices=["claude", "cursor"],
                     help="Headless CLI family when backend=headless "
                          "(default: config headless_cli, else claude)")
    fnt.add_argument("--cli-bin", dest="cli_bin", default=None,
                     help="Headless CLI binary override (default: claude or cursor-agent)")
    fnt.add_argument("--claude-bin", dest="claude_bin", default=None,
                     help="Back-compat alias for --cli-bin (Claude profile)")
    fnt.add_argument(
        "--effort", dest="effort", default=None,
        choices=["low", "medium", "high", "xhigh", "default"],
        help="Per-run Claude --effort override (default: config "
             "headless_effort_footnotes, else high; 'default' emits no --effort flag)",
    )
    fnt.add_argument(
        "--prompt-cache", dest="prompt_cache", default=None,
        choices=["auto", "5m", "1h", "off"],
        help="Per-run Claude prompt-cache TTL (default: config headless_prompt_cache / "
             "auto). auto picks 5m|1h|off from job shapes; off disables caching",
    )
    fnt.add_argument("--provider", default=None)
    fnt.add_argument("--model", default=None)
    fnt.add_argument("--retranslate", action="store_true",
                     help="Re-translate notes that already have a translated_body")
    fntp = fn_sub.add_parser("translate-prepare",
                             help="Subagent path: render a prompt per footnote batch + a manifest "
                                  "(no spend); then spawn translator subagents and translate-commit")
    add_project(fntp)
    fntp.add_argument("--retranslate", action="store_true",
                      help="Re-prepare notes that already have a translated_body")
    fntc = fn_sub.add_parser("translate-commit",
                             help="Parse the footnote batch drafts into footnotes.json (idempotent)")
    add_project(fntc)
    fna = fn_sub.add_parser("apply",
                            help="Free: convert surviving [FOOTNOTE:N] tokens into reader "
                                 "footnotes and rebuild the EPUB (needs alignments)")
    add_project(fna)
    fnd = fn_sub.add_parser("drop",
                            help="Free: strip [FOOTNOTE:N] tokens from source.txt + chapters "
                                 "and delete footnotes.json (the Step 0 'drop' choice)")
    add_project(fnd)

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

    # status (resume at a glance) -------------------------------------------
    stp2 = sub.add_parser("status",
                          help="Report pipeline progress: translated/pending per chapter, "
                               "saved spawn plan, artifacts (read-only; no spend)")
    add_project(stp2)

    # runs (summarize the run log) ------------------------------------------
    rnp = sub.add_parser("runs",
                         help="Summarize a run from logs/harness_runs.jsonl: command "
                              "timeline, beats, outcomes (read-only)")
    add_project(rnp)
    rnp.add_argument("--run-id", dest="run_id", default=None,
                     help="Run id to summarize (default: the project's most recent run)")

    # log-event (record a qualitative beat in the run log) ------------------
    lep = sub.add_parser("log-event",
                         help="Append a quality/friction beat to logs/harness_runs.jsonl")
    add_project(lep)
    lep.add_argument("--event", required=True,
                     help="Beat name, e.g. approval | backend | spawn_mode | respawn")
    lep.add_argument("--data", default=None,
                     help="JSON object of fields to record, e.g. "
                          "'{\"beat\":\"glossary\",\"decision\":\"approved_first_pass\"}'")

    # config-set (persist once-per-book skill decisions) --------------------
    csp = sub.add_parser(
        "config-set",
        help="Persist a once-per-book skill decision into .harness/config.json "
             "(backend, footnotes_decision, prompt-prefix opt-ins)",
    )
    add_project(csp)
    csp.add_argument("--key", required=True, choices=sorted(flow._CONFIG_SET_KEYS),
                     help="Config key to set")
    csp.add_argument(
        "--value", required=True,
        help="Value to persist. Per key: backend=api|subagent|headless; "
             "footnotes_decision=keep|drop|none; headless_cli=claude|cursor; "
             "headless_effort_{judges,annotations,translate,footnotes}="
             "auto|default|low|medium|high|xhigh; "
             "headless_prompt_cache=auto|5m|1h|off; "
             "always_include_dialogue=on|off|auto; "
             "always_include_image_instructions=on|off|auto "
             "(both stored as true/false/null; auto = on when the book needs it. "
             "Safe to change mid-book — re-run translate-prepare and only "
             "untranslated chunks are re-rendered); "
             "headless_extra_flags=<free text, whitespace-split into argv — "
             "never --bare or --effort>",
    )

    # --schema on every leaf subcommand. The blocks cost real tokens on every
    # call (status alone is ~2KB), so they are opt-in on success — and, per
    # _stamp_schema, automatic on any error, where the caller most needs the
    # shape. Nested groups (style-guide / glossary / address-map / footnotes)
    # need a recursive walk; adding the flag to a parent *and* a child would
    # conflict on dest="schema".
    _add_schema_flags(parser)

    return parser


def _add_schema_flags(parser: argparse.ArgumentParser) -> None:
    """Add ``--schema`` to every leaf subparser (recursive for nested groups)."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for subparser in action.choices.values():
            has_nested = any(
                isinstance(a, argparse._SubParsersAction) for a in subparser._actions
            )
            if has_nested:
                _add_schema_flags(subparser)
                continue
            subparser.add_argument(
                "--schema",
                action="store_true",
                help="Include the _schema block documenting every output key (omitted "
                     "from successful output by default; always present on errors)",
            )


def _dispatch(args: argparse.Namespace):
    """Route a parsed command to its flow function. Returns a dict or an int exit code."""
    cmd = args.command
    if cmd == "setup":
        return flow.setup(
            args.project, url=args.url, chapter_pattern=args.chapter_pattern,
            custom_regex=args.custom_regex, target_language=args.target_language,
            locale=args.locale, provider=args.provider, model=args.model,
            title=args.title, author=args.author, language_code=args.language_code,
            always_include_dialogue=args.always_include_dialogue,
            always_include_image_instructions=args.always_include_image_instructions,
            front_matter_titles=args.front_matter_titles,
            back_matter_titles=args.back_matter_titles,
            min_chapter_size=args.min_chapter_size,
            auto_strip_boilerplate=args.auto_strip_boilerplate,
            footnotes=args.footnotes,
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
            auto_strip_boilerplate=args.auto_strip_boilerplate,
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
    if cmd == "address-map":
        if args.action == "precheck":
            return flow.address_map_precheck(args.project)
        if args.action == "skip":
            return flow.address_map_skip(args.project)
        if args.action == "prepare":
            return flow.address_map_prepare(args.project, max_chapters=args.max_chapters)
        if args.action == "commit":
            return flow.address_map_commit(args.project, draft=args.draft)
        if args.action == "rename":
            return flow.address_map_rename(args.project, draft=args.draft)
    if cmd == "difficulty":
        return flow.difficulty(args.project)
    if cmd == "chunk":
        return flow.chunk(args.project, size=args.size, chapters=args.chapters,
                          per_chapter=args.per_chapter)
    if cmd == "cost":
        return flow.cost(args.project, chapters=args.chapters)
    if cmd == "translate":
        return flow.translate(args.project, yes=args.yes, model=args.model,
                              provider=args.provider, chapters=args.chapters,
                              enable_thinking=args.enable_thinking)
    if cmd == "translate-prepare":
        return flow.translate_prepare(args.project, chapters=args.chapters,
                                      worker_model=args.worker_model,
                                      worker_thinking=args.worker_thinking,
                                      parallelism=args.parallelism, window=args.window,
                                      batch_size=args.batch_size, brief=args.brief)
    if cmd == "translate-commit":
        return flow.translate_commit(args.project, worker_model=args.worker_model,
                                     allow_problems=args.allow_problems)
    if cmd == "translate-fanout":
        chunk_ids = None
        if args.chunk_ids:
            chunk_ids = [c.strip() for c in args.chunk_ids.split(",") if c.strip()]
        return flow.translate_fanout(
            args.project,
            chunk_ids=chunk_ids,
            concurrency=args.concurrency,
            cli=args.cli,
            cli_bin=args.cli_bin,
            claude_bin=args.claude_bin,
            effort=getattr(args, "effort", None),
            cache=getattr(args, "prompt_cache", None),
        )
    if cmd == "retranslate":
        chunk_ids = None
        # `is not None`, not truthiness: `--chunk-ids ""` must reach flow as an EMPTY
        # list (an explicit scope that parsed to nothing → rejected there), not as None
        # (the flag was never passed → the whole project). On a destructive verb those
        # two must never collapse into each other.
        if args.chunk_ids is not None:
            chunk_ids = [c.strip() for c in args.chunk_ids.split(",") if c.strip()]
        return flow.retranslate(args.project, chapters=args.chapters,
                                chunk_ids=chunk_ids, yes=args.yes, archive=args.archive)
    if cmd == "combine":
        return flow.combine(args.project, chapters=args.chapters)
    if cmd == "epub":
        return flow.epub(args.project, title=args.title, author=args.author, language=args.language)
    if cmd == "footnotes":
        if args.action == "translate":
            backend = None if args.backend == "auto" else args.backend
            return flow.footnotes_translate(
                args.project, yes=args.yes, backend=backend,
                provider=args.provider, model=args.model,
                retranslate=args.retranslate,
                cli=getattr(args, "cli", None),
                cli_bin=getattr(args, "cli_bin", None),
                claude_bin=getattr(args, "claude_bin", None),
                effort=getattr(args, "effort", None),
                cache=getattr(args, "prompt_cache", None),
            )
        if args.action == "translate-prepare":
            return flow.footnotes_translate_prepare(args.project, retranslate=args.retranslate)
        if args.action == "translate-commit":
            return flow.footnotes_translate_commit(args.project)
        if args.action == "apply":
            return flow.footnotes_apply(args.project)
        if args.action == "drop":
            return flow.footnotes_drop(args.project)
    if cmd == "align":
        return flow.align(args.project, chapters=args.chapters,
                          source_lang_code=args.source_lang_code,
                          target_lang_code=args.target_lang_code,
                          reader_host=args.reader_host, reader_port=args.reader_port)
    if cmd == "show-translation":
        return flow.show_translation(args.project, chapters=args.chapters,
                                     max_chunks=args.max_chunks,
                                     include_source=args.include_source)
    if cmd == "status":
        return flow.status(args.project)
    if cmd == "runs":
        return flow.runs(args.project, run_id=args.run_id)
    if cmd == "log-event":
        return flow.log_event(args.project, event=args.event, data=args.data)
    if cmd == "config-set":
        return flow.config_set(args.project, key=args.key, value=args.value)
    raise SystemExit(f"unknown command: {cmd}")


# Commands that wrap a subprocess: they stream the wrapped CLI's output live and return
# a dict carrying ``exit_code`` (via ``flow._stream_result``), or a bare int on a pre-flight
# refusal (``epub`` missing metadata, ``translate`` without ``--yes``). Either way ``main()``
# leaves a FRESH ``last_output.json`` and propagates the wrapped exit code (friction-log #18).
_STREAMING_COMMANDS = ("chunk", "cost", "translate", "epub", "footnotes")


_SCHEMA_SIDECAR = "last_output_schema.json"


def _artifact_harness_dir(args: argparse.Namespace, result: dict) -> Path | None:
    """Resolve ``.harness/`` for the output artifacts, or None when it can't be known."""
    project = getattr(args, "project", None)
    if not project:
        # setup --title (no --project) resolves the dir itself; carry it in result.
        project_dir_str = result.get("project_dir") if isinstance(result, dict) else None
        if not project_dir_str:
            return None
        project_dir = Path(project_dir_str)
    else:
        project_dir = state.resolve_project_dir(project)
    return state.harness_dir(project_dir)


def _should_inline_schema(args: argparse.Namespace, result: dict) -> bool:
    """Inline ``_schema`` on ``--schema``, soft errors, or a non-zero streaming exit."""
    if getattr(args, "schema", False):
        return True
    if result.get("error"):
        return True
    exit_code = result.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def _stamp_schema(args: argparse.Namespace, result: dict) -> None:
    """Attach schema documentation without burning tokens on every successful Read.

    Friction-log #19 (self-documenting keys) vs bambi #4 (read-back tax): the registry
    still lives in ``flow.OUTPUT_SCHEMAS``, but the default transport is a sidecar file
    (``last_output_schema.json``) with ``_schema_path`` stamped *first* so a stray
    ``tail`` lands on result fields. ``--schema`` and error payloads still inline
    ``_schema`` — unlike ``run_judges``, we never tell the agent to re-run, because many
    harness commands mutate.

    The sidecar path also stamps ``_schema_keys`` — the schema's key NAMES, no
    descriptions. Bambi §3b: result keys differ per verb (``manifest`` / ``chapters`` /
    ``aligned``), and reading a whole schema file to learn one name feels heavier than
    guessing, which is how a probe got burned on a ``KeyError``. The names are ~5% of the
    inline block and cover keys the payload itself can never show (``error``, ``note``),
    so the guess has no excuse left. Derived from the registry, so it needs no maintenance
    of its own. Not added to the inline branch — ``_schema`` already carries them.
    """
    if not isinstance(result, dict):
        return
    cmd = getattr(args, "command", None)
    if not cmd:
        return
    schema = flow.schema_for(cmd, getattr(args, "action", None))
    if not schema:
        return

    if _should_inline_schema(args, result):
        result["_schema"] = schema
        return

    try:
        harness = _artifact_harness_dir(args, result)
    except Exception:  # noqa: BLE001 - fall through to inline
        harness = None
    if harness is None:
        # Can't point at a sidecar — keep self-docs rather than drop them.
        result["_schema"] = schema
        return

    # Pointer + key names first; drop any prior meta keys so a rebuild stays clean.
    rebuilt = {
        "_schema_path": str(harness / _SCHEMA_SIDECAR),
        "_schema_keys": list(schema),
    }
    for key, value in result.items():
        if key in ("_schema", "_schema_path", "_schema_keys"):
            continue
        rebuilt[key] = value
    result.clear()
    result.update(rebuilt)


def _write_output_artifact(args: argparse.Namespace, result: dict) -> None:
    """Mirror a command's JSON result to ``.harness/last_output.json`` (UTF-8).

    The agent reads this file instead of capturing stdout, sidestepping Windows console
    encoding entirely (friction-log #4). Schema docs live beside it in
    ``last_output_schema.json`` (bambi #4); the payload points there via ``_schema_path``
    unless ``_stamp_schema`` inlined ``_schema``. Best-effort: the artifact must never
    break a command, so any resolution/write failure is swallowed.
    """
    try:
        harness = _artifact_harness_dir(args, result)
        if harness is None:
            return
        project_dir = harness.parent
        state.ensure_harness_dir(project_dir)
        out = harness / "last_output.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        cmd = getattr(args, "command", None)
        if cmd:
            schema = flow.schema_for(cmd, getattr(args, "action", None))
            if schema:
                (harness / _SCHEMA_SIDECAR).write_text(
                    json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8",
                )
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
    "stage", "spawn_mode_moot", "totals",
    # retranslate: two scalars only. `scope`/`cleared`/`stale_drafts` are
    # deliberately excluded — they are unbounded and the summary stays compact.
    "dry_run", "archived",
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
        project_dir_str = result.get("project_dir") if isinstance(result, dict) else None
        if project:
            try:
                project_dir = state.resolve_project_dir(project)
                slug = project_dir.name
                run_id = state.ensure_run_id(project_dir)
            except Exception:  # noqa: BLE001 - never let resolution break logging
                slug = project
        elif project_dir_str:
            try:
                project_dir = Path(project_dir_str)
                slug = project_dir.name
                run_id = state.ensure_run_id(project_dir)
            except Exception:  # noqa: BLE001
                slug = project_dir_str

        fields = {"cmd": label, "status": status, "dur_s": round(duration, 3)}
        if isinstance(result, dict):
            for key in _RESULT_SUMMARY_KEYS:
                if key in result:
                    fields[key] = result[key]

        log_run_event(run_id=run_id, project=slug, event="command", **fields)
    except Exception:  # noqa: BLE001 - logging is a convenience, never a dependency
        pass


def main() -> None:
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

    if args.command in _STREAMING_COMMANDS:
        # The wrapped CLI's progress/cost already streamed live. Still leave a FRESH
        # structured last_output.json (friction-log #18 — never the previous command's
        # result) and propagate the wrapped exit code so the cost gate keeps its teeth.
        # `result` is a dict carrying exit_code, or a bare int on a pre-flight refusal.
        rc = result["exit_code"] if isinstance(result, dict) else result
        payload = result if isinstance(result, dict) else {"command": args.command, "exit_code": rc}
        _stamp_schema(args, payload)
        _log_command(args, status=("ok" if rc == 0 else f"exit_{rc}"), duration=elapsed,
                     result=payload)
        _write_output_artifact(args, payload)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.exit(rc)

    if isinstance(result, int):
        # A non-streaming command returning a bare exit code (defensive; none today).
        _log_command(args, status=("ok" if result == 0 else f"exit_{result}"), duration=elapsed)
        sys.exit(result)

    _stamp_schema(args, result)
    _log_command(args, status="ok", duration=elapsed, result=result)
    _write_output_artifact(args, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
