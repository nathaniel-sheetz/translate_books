#!/usr/bin/env python3
"""
Read one book's judge findings together and report what is systemic.

A single judge finding says one sentence is wrong. Read across a whole book,
the findings say more: a glossary entry that never reached the translator, a
style guide that contradicts the address map, a rule the translator keeps
missing, a judge whose findings keep being dismissed. This command runs the
coded detectors in ``src/book_review/signals.py`` over every dialogue, address
and editorial finding the book has, and writes a report naming those patterns
with the findings behind each.

Each subcommand prints one JSON object with a ``_schema`` block:

    status    coverage, owed chapters, last review   (no spend, no writes)
    judge     run every judge over the chapters that owe it, then editorial
              pass 2: --estimate prepares and projects tokens (no spawns),
              --yes runs the headless wave (subscription)
    signals   run the coded detectors; write signals.json + a report  (no spend)
    prepare   detectors + budgeted evidence + one synthesis job per theme  (no spend)
    fanout    run the synthesis jobs as one headless wave             (subscription)
    commit    validate the proposals; write recommendations.json + the report (no spend)
    run       prepare -> fanout -> commit in one call (needs --yes)    (subscription)
    board     where the review page is, and what the user picked and approved on it
    revise    rewrite one recommendation's changes as the user's note asked
              (--id R-1a2b3c --from revised.json); they approve it on the page (no spend)
    apply     apply picked recommendations: --from-board (what the user approved on
              the page) or --select R-1a2b3c[,R-4d5e6f.2]; a dry run with diffs
              unless --yes                                             (no spend)
    skip      mark the current translations reviewed without reviewing them,
              so the harness stops routing here until new chunks land

    python scripts/book_review.py status  --project pollyanna
    python scripts/book_review.py judge   --project pollyanna --estimate
    python scripts/book_review.py judge   --project pollyanna --yes --concurrency 10
    python scripts/book_review.py signals --project pollyanna

Only `apply --yes` edits the book (glossary, style guide, address map); nothing
deletes a finding.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.book_review import judging, progress, review_dir  # noqa: E402
from src.book_review import board as review_board  # noqa: E402
from src.book_review import synthesize  # noqa: E402
from src.book_review.revise import revise as revise_recommendation  # noqa: E402
from src.book_review.apply import apply as apply_selection  # noqa: E402
from src.book_review.corpus import load_book  # noqa: E402
from src.book_review.report import write_signals  # noqa: E402
from src.book_review.signals import coverage, run_all  # noqa: E402
from src.harness import state as hstate  # noqa: E402

_STATUS_SCHEMA = {
    "status": "'ok' | 'error'",
    "project": "project directory name",
    "findings_by_judge": "judge findings on disk (kept, retracted and filtered), per judge",
    "coverage": "{address_map, judges: {<judge>: {state, owed, done, last_run}}, "
    "unverified_editorial_chunks}: what has not been judged yet",
    "last_signals": "{path, generated_at} of the last signals.json, or null",
    "instructions": "next step",
}

_SIGNALS_SCHEMA = {
    "status": "'ok' | 'error'",
    "project": "project directory name",
    "findings": "judge findings read",
    "counts": "signals per detector",
    "headline": "the highest-scoring signals, one line each, worst first",
    "signals_path": "full detector output (JSON)",
    "report_path": "the markdown report to show the operator",
    "coverage": "same shape as status.coverage",
    "instructions": "next step",
}

_JUDGE_SCHEMA = {
    "status": "'ok' | 'needs_confirm' | 'error'",
    "plan": "{judges, address_map, owed: {judge: [chapter ids]}, owed_chunks, "
    "pending_pass2_chunks, nothing_to_do}",
    "effective": "the profile the wave runs under (cli, worker_model, effort, ...) with provenance",
    "concurrency": "--estimate only: parallel CLI processes both passes will run (--concurrency, "
    "default 10)",
    "concurrency_source": "--estimate only: '--concurrency' or 'default'",
    "job_timeout_s": "--estimate only: seconds a job may run before it is killed (--job-timeout, "
    "else the CLI's default: 900 on cursor and codex, 1800 on claude)",
    "job_timeout_source": "--estimate only: '--job-timeout' or 'default for <cli>'",
    "wall": "--estimate only: {pass1_s, pass2_max_s, total_s, per_judge_s, median_job_s, "
    "p90_job_s, source, background_cap_s, fits_background_cap} projected from logged job "
    "walls at this width; null when no job of this CLI has been logged",
    "pass1": "--estimate only: {jobs, prompt_tokens, projected_tokens, output_tokens, "
    "baseline_tokens, resumed_drafts, ...} for the judge wave; jobs excludes drafts kept "
    "from a cut-off sweep",
    "adjudication": "--estimate only: editorial pass 2 ceiling {jobs_max, tokens_max, "
    "baseline_tokens, baseline_source}; pass 2 cannot be measured before pass 1 runs",
    "summary": "--yes only: {ran, total, wrote, resumed, failed, missing, adjudication, error_count, "
    "errors, usage, effective}",
    "instructions": "next step",
}

HEADLINE = 10

# Upstream causes first: a conflict or a dead glossary key is one fix for many
# findings, while a plain recurring cluster may only be a rule written after
# the chapters it is judged against.
_DETECTOR_RANK = {
    "address_style_conflicts": 0,
    "dead_glossary_keys": 1,
    "glossary_sense": 2,
    "rule_drift": 3,
    "false_positive_clusters": 4,
    "opposite_directions": 5,
    "recurring_rules": 6,
}
_LABEL_RANK = {"resisted": 0, "disputed": 1, "recurring": 2, "unreviewed": 3}


PER_DETECTOR = 3


def _headline(signals: list[dict]) -> list[dict]:
    """At most three lines per detector, upstream causes first, then the rest by rank.

    A book with twenty dead glossary keys would otherwise fill the headline with
    them and hide the address conflict underneath.
    """
    ranked = sorted(signals, key=_headline_rank)
    taken: dict[str, int] = {}
    head, rest = [], []
    for s in ranked:
        if taken.get(s["detector"], 0) < PER_DETECTOR:
            taken[s["detector"]] = taken.get(s["detector"], 0) + 1
            head.append(s)
        else:
            rest.append(s)
    return (head + rest)[:HEADLINE]


def _headline_rank(signal: dict) -> tuple:
    detector = signal["detector"]
    label = (signal.get("metrics") or {}).get("label")
    if detector == "dead_glossary_keys" and (signal.get("metrics") or {}).get("state") == "absent":
        return (9, 0, 0)
    return (_DETECTOR_RANK.get(detector, 5), _LABEL_RANK.get(label, 0), -signal["score"])


def _print(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "error" else 0


def _resolve_project(arg: str) -> Path:
    try:
        return hstate.resolve_project_dir(arg).resolve()
    except FileNotFoundError:
        raise SystemExit(_print({
            "status": "error",
            "error": f"Project not found: {arg!r} (looked for a directory, projects/{arg}, "
                     "and projects/*/{arg}).",
        }))


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    corpus = load_book(project_dir)
    signals_path = review_dir(project_dir) / "signals.json"
    last = None
    if signals_path.exists():
        last = {"path": str(signals_path), "generated_at": signals_path.stat().st_mtime}
    counts: dict[str, int] = {}
    for f in corpus.findings:
        counts[f.judge] = counts.get(f.judge, 0) + 1
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "findings_by_judge": counts,
        "coverage": coverage(corpus),
        "last_signals": last,
        "instructions": "Run `signals` to write the coded report (free, no model).",
        "_schema": _STATUS_SCHEMA,
    })


def _cmd_judge(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    options = dict(judges=args.judge, cli=args.cli, worker_model=args.worker_model,
                   effort=args.effort, cache=args.prompt_cache, concurrency=args.concurrency,
                   job_timeout=args.job_timeout)
    if args.yes:
        out = judging.run(project_dir, **options)
        out["instructions"] = "Run `signals` next to read the new findings."
    elif args.estimate:
        out = judging.estimate(project_dir, **options)
        out["instructions"] = (
            "Relay plan, pass1 and adjudication as one consent gate and stop. "
            "On approval run `judge --yes` with the same flags."
        )
    else:
        out = {"status": "needs_confirm", "plan": judging.plan(project_dir, judges=args.judge),
               "instructions": "Run with --estimate for a token projection, then --yes."}
    out["project"] = project_dir.name
    out["_schema"] = _JUDGE_SCHEMA
    return _print(out)


def _cmd_signals(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    corpus = load_book(project_dir)
    result = run_all(corpus)
    paths = write_signals(corpus, result)
    progress.record_review(project_dir, kind="signals", report_path=paths["report_path"])
    counts: dict[str, int] = {}
    for s in result["signals"]:
        counts[s["detector"]] = counts.get(s["detector"], 0) + 1
    ranked = _headline(result["signals"])
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "findings": result["findings"],
        "counts": counts,
        "headline": [s["title"] for s in ranked],
        **paths,
        "coverage": result["coverage"],
        "instructions": "Show the operator the headline and the report path.",
        "_schema": _SIGNALS_SCHEMA,
    })


_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "jobs": "synthesis jobs rendered (one per theme, never split)",
    "by_theme": "jobs per theme (glossary, address_style, rules, drift, dismissals)",
    "input_tokens": "estimated input tokens across all jobs, preamble included",
    "omitted": "signals shown in less detail to fit the budget: [{signal_id, from, to, reason}]",
    "effective": "the profile the wave would run under, with provenance",
    "instructions": "next step",
}

_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some drafts unparseable) | 'error'",
    "recommendations": "validated recommendations written",
    "by_priority": "{1: fix before translating more, 2: before publishing, 3: worth doing}",
    "no_action": "signals the model judged need no change",
    "rejected": "proposals with no valid patch (see recommendations.json rejected[])",
    "failed": "jobs whose draft could not be parsed: re-run fanout for them",
    "missing": "jobs with no draft",
    "report_path": "the markdown report to show the operator",
    "instructions": "next step",
}


def _profile_kwargs(args: argparse.Namespace) -> dict:
    return {k: getattr(args, k) for k in ("cli", "worker_model", "effort") if getattr(args, k, None)}


def _cmd_prepare(args: argparse.Namespace) -> int:
    out = synthesize.prepare(_resolve_project(args.project), **_profile_kwargs(args))
    if out.get("jobs"):
        out["instructions"] = ("Quote jobs, input_tokens and effective as one consent gate and stop; "
                               "on approval run `fanout`, then `commit`.")
    out["_schema"] = _PREPARE_SCHEMA
    return _print(out)


def _cmd_fanout(args: argparse.Namespace) -> int:
    kwargs = _profile_kwargs(args)
    kwargs.pop("worker_model", None)
    if args.concurrency:
        kwargs["concurrency"] = args.concurrency
    return _print(synthesize.fanout(_resolve_project(args.project), **kwargs))


def _cmd_commit(args: argparse.Namespace) -> int:
    out = synthesize.commit(_resolve_project(args.project))
    out["instructions"] = _AFTER_COMMIT
    out["_schema"] = _COMMIT_SCHEMA
    return _print(out)


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.yes:
        return _print({"status": "error", "error": "`run` spends subscription tokens: pass --yes "
                       "after the user approved `prepare`'s gate."})
    project_dir = _resolve_project(args.project)
    prep = synthesize.prepare(project_dir, **_profile_kwargs(args))
    if prep.get("status") == "error" or not prep.get("jobs"):
        return _print(prep)
    fan = synthesize.fanout(project_dir, **({"concurrency": args.concurrency}
                                            if args.concurrency else {}))
    if fan.get("error"):
        return _print({"status": "error", "error": fan["error"], "fanout": fan})
    out = synthesize.commit(project_dir)
    out["instructions"] = _AFTER_COMMIT
    out["fanout"] = {"counts": fan.get("counts"), "failed": fan.get("failed"), "usage": fan.get("usage")}
    out["_schema"] = _COMMIT_SCHEMA
    return _print(out)


_AFTER_COMMIT = (
    "With more than two recommendations, run `board` and give the user its url: they pick "
    "and approve there. With one or two, show them in the chat and `apply --select`."
)

DEFAULT_BASE_URL = "http://127.0.0.1:5000"

_BOARD_SCHEMA = {
    "status": "'ok' | 'error'",
    "url": "the review page: give it to the user",
    "server_running": "false when the web UI did not answer at --base-url",
    "recommendations": "recommendations in the last commit",
    "applied": "ids already applied",
    "open": "ids not applied yet",
    "selection": "what is picked on the page now (apply --select tokens)",
    "notes": "{id: note} the user typed on a recommendation: read and act on them",
    "revise": "{id: note} the user asked to have rewritten from the note: `revise` each",
    "approved": "true when the picks are approved and still match the artifacts",
    "approved_selection": "what `apply --from-board --yes` would write",
    "not_approved_because": "why there is nothing to apply yet",
    "instructions": "next step",
}

_APPLY_SCHEMA = {
    "status": "'dry_run' | 'ok' | 'error'",
    "diffs": "{artifact: unified diff} of every artifact the selection edits",
    "actions": "one row per patch and follow-up: {id, type, status: staged | saved | done | "
    "offer, ...}; 'offer' rows (retranslate, manual) are never executed here",
    "saved": "--yes only: artifacts written (each via its save path, history-logged)",
    "stale": "on error: '<id>: change N: why' for each change that no longer fits the book",
    "selection": "the --select tokens applied (with --from-board, the ones read off the page)",
    "instructions": "next step",
}


def _board_is_served(url: str) -> bool:
    """Whether the web UI answers for this project's review page right now."""
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _cmd_board(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    if not synthesize.recommendations_path(project_dir).exists():
        return _print({"status": "error", "error": "no recommendations yet: run the synthesis "
                       "(`prepare`, `fanout`, `commit`) first"})
    base = args.base_url.rstrip("/")
    out = {"status": "ok", "project": project_dir.name,
           "url": f"{base}/book-review/{project_dir.name}",
           "server_running": _board_is_served(f"{base}/book-review/{project_dir.name}"),
           **review_board.summary(project_dir)}
    out["instructions"] = (
        "Give the user url and end the turn: they pick, read the diffs and approve on the "
        "page. When they come back, run `board` again. revise: write each one's changes as "
        "its note asks and store them with `revise`, then send the user back to the page to "
        "pick and approve the revision. approved: run `apply --from-board --yes`. notes: "
        "what they asked for beyond the picks; answer or act on each."
    )
    if not out["server_running"]:
        out["instructions"] = (
            "The web UI is not answering at that address, so the page will not open: ask the "
            "user to start it (`python web_ui/app.py`), or pass --base-url. " + out["instructions"]
        )
    out["_schema"] = _BOARD_SCHEMA
    return _print(out)


def _cmd_apply(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    if args.from_board:
        if args.select:
            return _print({"status": "error", "error": "pass --from-board or --select, not both"})
        if args.yes:
            selection, problem = review_board.approved_selection(project_dir)
            if selection is None:
                return _print({"status": "error", "error": f"nothing to apply: {problem}",
                               "instructions": "Give the user the board url (`board`)."})
        else:
            selection = review_board.load(project_dir)["selection"]
            if not selection:
                return _print({"status": "error", "error": "nothing is picked on the board"})
    elif not args.select:
        return _print({"status": "error", "error": "pass --select <ids> or --from-board"})
    else:
        selection = args.select
    out = apply_selection(project_dir, selection, yes=args.yes)
    if args.from_board and args.yes and out.get("status") == "ok":
        review_board.clear_after_apply(project_dir)
    out["selection"] = selection
    out["_schema"] = _APPLY_SCHEMA
    return _print(out)


_REVISE_SCHEMA = {
    "status": "'ok' | 'error'",
    "id": "the recommendation revised",
    "revision": "how many times it has been revised",
    "problems": "on error: why the revision was refused (fix the file and run again)",
    "instructions": "next step",
}


def _cmd_revise(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    try:
        change = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _print({"status": "error", "problems": [f"cannot read {args.from_file}: {exc}"]})
    out = revise_recommendation(project_dir, args.id, change)
    if out["status"] == "ok":
        out["instructions"] = ("The card now shows the revision beside the original. Tell the "
                               "user what changed and send them back to the page to pick and "
                               "approve it; nothing is applied yet.")
    out["_schema"] = _REVISE_SCHEMA
    return _print(out)


def _cmd_skip(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    recorded = progress.record_review(project_dir, kind="skipped")
    return _print({
        "status": "ok",
        "project": project_dir.name,
        "recorded": recorded,
        "instructions": "The router moves on until new chunks are translated. To stop "
        "it for good: harness.py config-set --key book_review_after_translate --value off "
        "(and book_review_canary).",
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="book_review.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "coverage, owed chapters, last review (no spend, no writes)"),
        ("signals", "run the coded detectors; write signals.json + a report (no spend)"),
        ("skip", "mark the current translations reviewed without reviewing them"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    for name, help_text in (
        ("prepare", "detectors + evidence + synthesis jobs (no spend)"),
        ("fanout", "run the synthesis jobs (subscription)"),
        ("commit", "validate proposals; write recommendations + report (no spend)"),
        ("run", "prepare -> fanout -> commit (subscription; needs --yes)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
        if name in ("prepare", "fanout", "run"):
            p.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None, help="headless CLI")
            p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "default"),
                           default=None, help="default: headless_effort_book_review")
        if name in ("prepare", "run"):
            p.add_argument("--worker-model", default=None, help="pin the synthesis model")
        if name in ("fanout", "run"):
            p.add_argument("--concurrency", type=int, default=None, help="parallel CLI processes")
        if name == "run":
            p.add_argument("--yes", action="store_true", help="confirm the subscription spend")

    apply_p = sub.add_parser("apply", help="apply picked recommendations (dry run unless --yes)")
    apply_p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    apply_p.add_argument("--select", action="append", default=None,
                         help="recommendation ids, comma-separated; R-xxxxxx.N picks one patch")
    apply_p.add_argument("--from-board", action="store_true",
                         help="what the user picked on the review page (--yes needs their approval)")
    apply_p.add_argument("--yes", action="store_true", help="write the changes")

    board_p = sub.add_parser("board", help="the review page's url and what the user picked on it")
    board_p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    board_p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                         help=f"where the web UI is listening (default: {DEFAULT_BASE_URL})")

    revise_p = sub.add_parser("revise", help="rewrite one recommendation as the user's note asked")
    revise_p.add_argument("--project", required=True, help="Project id (under projects/) or path")
    revise_p.add_argument("--id", required=True, help="the recommendation, e.g. R-1a2b3c")
    revise_p.add_argument("--from", dest="from_file", required=True,
                          help='JSON file: {"patches": [...], "why": "...", optional '
                          '"follow_ups": [...] and "rationale"}')

    judge = sub.add_parser(
        "judge", help="run every judge over owed chapters, then editorial pass 2 (subscription)"
    )
    judge.add_argument("--project", required=True, help="Project id (under projects/) or path")
    gate = judge.add_mutually_exclusive_group()
    gate.add_argument("--estimate", action="store_true",
                      help="prepare and project tokens; spawns nothing")
    gate.add_argument("--yes", action="store_true", help="run the headless wave")
    judge.add_argument("--judge", action="append", choices=judging.SWEEP_JUDGES, default=None,
                       help="limit to these judges (repeatable; default: all the book supports)")
    judge.add_argument("--cli", choices=hstate.HEADLESS_CLIS, default=None, help="headless CLI")
    judge.add_argument("--worker-model", default=None, help="pin the judge model")
    judge.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "default"),
                       default=None, help="default: headless_effort_judges")
    judge.add_argument("--prompt-cache", dest="prompt_cache", default=None,
                       choices=["auto", "5m", "1h", "off"], help="Claude prompt-cache TTL")
    judge.add_argument("--concurrency", type=int, default=None,
                       help="parallel CLI processes for both passes (default 10)")
    judge.add_argument("--job-timeout", dest="job_timeout", type=float, default=None,
                       help="seconds one job may run before it is killed, both passes "
                            "(default: 900 on cursor and codex, 1800 on claude)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "status": _cmd_status,
        "judge": _cmd_judge,
        "signals": _cmd_signals,
        "prepare": _cmd_prepare,
        "fanout": _cmd_fanout,
        "commit": _cmd_commit,
        "run": _cmd_run,
        "apply": _cmd_apply,
        "board": _cmd_board,
        "revise": _cmd_revise,
        "skip": _cmd_skip,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
