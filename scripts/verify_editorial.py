#!/usr/bin/env python3
"""Non-interactive CLI for the editorial judge's adjudication pass.

``run_judges.py`` runs the editorial judge and persists its candidates. This
script gives those candidates a second opinion — CONFIRM, RETRACT or
RECLASSIFY — with the English original attached to the ones that asked for it,
and rewrites ``evaluations/<chunk>.json`` with the adjudicated set.

It is a sibling pipeline rather than an eighth ``run_judges.py`` subcommand, for
the same reason ``review_annotations.py`` is: the unit here is a *chunk's
candidate set*, not a ``JudgeTarget``, and the prompt carries per-candidate
context that the ``Judge`` seams have no slot for. It reuses this repo's
plumbing — ``llm_io`` for the call and the JSON parse, ``harness.headless`` for
the subscription wave, the same draft/commit split — rather than being forced
through a shape that does not fit.

Five subcommands:

  * ``status``  — **read-only**. Which chunks carry unverified candidates, and
                  with ``--drafts``, how far a prepared wave has got. No spend,
                  no writes.
  * ``run``     — **API backend**. Adjudicate now, one call per chunk, behind a
                  dollar cost gate.
  * ``prepare`` — **draft backend**, phase 1. Render one prompt file per chunk
                  plus a manifest, for a headless wave or spawned workers. Zero
                  spend, and the wave's **consent gate**: it returns
                  ``effective`` + ``usage_summary``, and takes the ``--cli`` /
                  ``--worker-model`` / ``--effort`` that decide them.
  * ``fanout``  — **draft backend**, opt-in headless wave over the manifest.
                  Inherits the profile ``prepare`` consented to; the flags are
                  overrides, not the primary knobs.
  * ``commit``  — **draft backend**, phase 2. Parse the drafts and persist.

Every command prints exactly one JSON object; every command but ``status``
mirrors it to ``<project>/.harness/editorial/last_output.json`` and prints an
``OUTPUT_JSON:`` pointer to stderr.

Examples:
    python scripts/verify_editorial.py status  --project pollyanna
    python scripts/verify_editorial.py status  --project pollyanna --drafts
    python scripts/verify_editorial.py run     --project pollyanna \\
        --scope chapter:chapter_01 --persist --confirm
    python scripts/verify_editorial.py prepare --project pollyanna --scope book \\
        --cli cursor --quiet
    python scripts/verify_editorial.py fanout  --project pollyanna
    python scripts/verify_editorial.py commit  --project pollyanna --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every raya/guillemet/accent in the JSON we print — and pass 2 quotes the same
# Spanish excerpts pass 1 does. ``run_judges.py`` forces UTF-8 for exactly this
# reason; the hasattr guard keeps it safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time. Every pass-2 command used to open with a RequestsDependencyWarning
# — the same "stderr that is not an error" the judges CLI already paid to kill,
# and every such line trains a reader to skim the stream real progress goes to.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.harness import state as hstate  # noqa: E402
from src.judges import editorial_wave as wave  # noqa: E402
from src.judges.scope import ScopeError  # noqa: E402

logger = logging.getLogger(__name__)

# Re-exported rather than redefined. The wave module is where pass 2 actually
# lives now — this file is the argparse shell over it — and two copies of
# ``JUDGE_NAME`` or of the concurrency default is exactly the drift the move was
# meant to end. The parser's help text quotes the same number the launcher uses.
JUDGE_NAME = wave.JUDGE_NAME
work_dir = wave.work_dir
collect_pending = wave.collect_pending
_DEFAULT_CONCURRENCY = wave.DEFAULT_CONCURRENCY
_TOKENS_PER_VERDICT = wave.TOKENS_PER_VERDICT


# ---------------------------------------------------------------------------
# Project + IO helpers


def _resolve_project(arg: str) -> Path:
    """Project dir for ``--project``: a path, a flat slug, or a nested one.

    Routed through :func:`harness.state.resolve_project_dir` rather than a local
    lookup, so a book filed under a grouping folder
    (``projects/.macdonald/photogen-nycteris``) answers to its bare slug here
    exactly as it does everywhere else in the harness. The local version globbed
    one level, and ``glob("*")`` skips dot-directories — which is precisely where
    the grouped books live, so the first command of every session 404'd.
    """
    try:
        return hstate.resolve_project_dir(arg)
    except FileNotFoundError as exc:
        raise SystemExit(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        ) from exc


def _write_output_artifact(project_dir: Optional[Path], payload: dict) -> None:
    """Mirror the result to ``.harness/editorial/last_output.json``.

    Best-effort by design: the artifact must never break a command. Read it back
    from there rather than filtering captured stdout through a second
    interpreter — that is where the raya mojibake comes from on Windows.
    """
    if project_dir is None:
        return
    try:
        directory = work_dir(project_dir)
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / "last_output.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # Say where it landed, on stderr, the way ``run_judges.py`` does. Without
        # the pointer a caller has to match on the JSON body to know a command
        # finished, and has to guess the path to read the full payload back.
        print(f"OUTPUT_JSON: {out}", file=sys.stderr)
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("Could not write last_output.json: %s", exc)


def _emit(project_dir: Optional[Path], payload: dict) -> int:
    _write_output_artifact(project_dir, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") in {"ok", "cost_exceeded"} else 1


# ---------------------------------------------------------------------------
# Commands
#
# Each one resolves the project, calls the matching :mod:`src.judges.editorial_wave`
# function, and emits what came back. The payloads are the wave's, unchanged: a
# divergence here would mean this CLI and the Review tab reporting different
# things about the same run, which is the drift the move exists to prevent.


def _scopes(args: argparse.Namespace) -> list[str]:
    """The ``--scope`` flags, or the default.

    Repeatable, because the wave has always taken a list and the Review tab
    passes one per ticked chapter. Before 0.53.1.0 argparse was single-valued
    here, so repeating the flag was last-wins: seven ``--scope chapter:`` flags
    staged the seventh chapter alone.
    """
    return list(args.scope) if args.scope else ["book"]


def _warn(payload: dict, prefix: str = "") -> None:
    """Relay the wave's warnings to stderr, where a skimmed stdout cannot hide them.

    A scope that resolves to nothing is one of them. ``collect_pending`` demotes
    an unresolvable scope to ``skipped`` and only raises when *every* scope fails
    — safe while the CLI passed exactly one, which it no longer does. A typo
    beside six good chapters would otherwise narrow the wave silently, reported
    only inside a JSON body nobody reads line by line.
    """
    for warning in payload.get("warnings") or []:
        print(f"{prefix}{warning}", file=sys.stderr)


def cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    # ``None`` as the sidecar target: ``status`` is the one read-only command,
    # and writing last_output.json for it would clobber the record of whatever
    # mutating command ran before it.
    payload = wave.status(
        project_dir, _scopes(args), include_verified=args.force, drafts=args.drafts
    )
    # ``status`` is where an operator reads the pending set off the screen, so a
    # scope that resolved to nothing has to be louder than a row in ``skipped``.
    _warn(payload, prefix="[status] warning: ")
    return _emit(None, payload)


def cmd_run(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    payload = wave.run_api(
        project_dir,
        _scopes(args),
        persist=args.persist,
        confirm=args.confirm,
        force=args.force,
        cost_limit=args.cost_limit,
        model=args.model,
        provider=args.provider,
    )
    _warn(payload, prefix="[run] warning: ")
    return _emit(project_dir, payload)


def cmd_prepare(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    payload = wave.prepare(
        project_dir,
        _scopes(args),
        model=args.model,
        provider=args.provider,
        cli=args.cli,
        worker_model=args.worker_model,
        effort=args.effort,
        force=args.force,
        keep_drafts=args.keep_drafts,
        quiet=args.quiet,
    )
    # Also on stderr: a mis-resolved CLI is worth seeing even when the caller
    # only skims stdout, and this is the last moment before the wave.
    _warn(payload, prefix="[prepare] warning: ")
    return _emit(project_dir, payload)


def cmd_fanout(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    payload = wave.fanout(
        project_dir,
        cli=args.cli,
        cli_bin=args.cli_bin,
        worker_model=args.worker_model,
        effort=args.effort,
        concurrency=args.concurrency,
        # Printed the moment the profile resolves rather than from the returned
        # payload: a wave on the wrong CLI can run for ten minutes, and a warning
        # that arrives with the result is a warning that arrived too late.
        on_profile=lambda prof: _warn(prof),
    )
    return _emit(project_dir, payload)


def cmd_commit(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    return _emit(
        project_dir,
        wave.commit(project_dir, persist=args.persist),
    )


# ---------------------------------------------------------------------------
# argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, scope: bool = True) -> None:
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
        if scope:
            # ``default=None``, not ``"book"``: an ``append`` action appends *to*
            # its default, so a non-None one would make every explicit scope read
            # ``["book", ...]`` — the whole project, quietly. ``_scopes`` supplies
            # the default instead.
            p.add_argument(
                "--scope",
                action="append",
                default=None,
                metavar="SCOPE",
                help="chunk:<id>, chapter:<id>, chapter:<first>..<last> (inclusive) "
                "or book. Repeatable — the wave unions them (default: book)",
            )
        p.add_argument("--model", default=None, help="Adjudicator model override")
        p.add_argument("--provider", default=None, help="Adjudicator provider override")
        p.add_argument("--verbose", action="store_true", help="Enable debug logging")

    p_status = sub.add_parser("status", help="read-only: which chunks await adjudication")
    common(p_status)
    p_status.add_argument("--force", action="store_true", help="Include already-verified chunks")
    p_status.add_argument(
        "--drafts",
        action="store_true",
        help="Also report manifest entries with/without a draft (is the wave still going?)",
    )

    p_run = sub.add_parser("run", help="API backend: adjudicate now, one call per chunk")
    common(p_run)
    p_run.add_argument("--persist", action="store_true", help="Write the adjudicated result")
    p_run.add_argument("--confirm", action="store_true", help="Proceed past the cost gate")
    p_run.add_argument("--cost-limit", type=float, default=0.50, help="Max estimated USD")
    p_run.add_argument("--force", action="store_true", help="Re-verify already-verified chunks")

    p_prepare = sub.add_parser("prepare", help="draft backend: render prompts + manifest")
    common(p_prepare)
    p_prepare.add_argument("--keep-drafts", action="store_true", help="Do not clear old drafts")
    p_prepare.add_argument("--force", action="store_true", help="Re-verify verified chunks")
    # The same four ``run_judges.py prepare`` carries. This is the consent
    # surface: whichever CLI is going to run the wave has to be known here, or
    # the tokens quoted are for a wave that never happens.
    p_prepare.add_argument("--cli", default=None, choices=["claude", "cursor"])
    p_prepare.add_argument("--worker-model", default=None, help="Pin the worker model")
    p_prepare.add_argument("--effort", default=None, help="Per-run effort override")
    p_prepare.add_argument(
        "--quiet",
        action="store_true",
        help="Omit entries[] from stdout, keeping manifest + effective + usage_summary",
    )

    p_fanout = sub.add_parser("fanout", help="draft backend: headless wave over the manifest")
    common(p_fanout, scope=False)
    p_fanout.add_argument("--cli", default=None, choices=["claude", "cursor"])
    p_fanout.add_argument("--cli-bin", default=None, help="Path to the CLI binary")
    p_fanout.add_argument("--worker-model", default=None, help="Pin the worker model")
    p_fanout.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max parallel headless processes "
        f"(default: the manifest's batch_size, else {_DEFAULT_CONCURRENCY})",
    )
    p_fanout.add_argument("--effort", default=None, help="Per-run effort override")

    p_commit = sub.add_parser("commit", help="draft backend: parse drafts and persist")
    common(p_commit, scope=False)
    p_commit.add_argument("--persist", action="store_true", help="Write the adjudicated results")
    # No ``--brief`` here, unlike ``run_judges.py commit``. Pass 2's results[] is
    # nine counts per chunk, not an EvalResult per (target, judge) — there is no
    # flood to stop, and dropping it took the per-chunk breakdown with it while
    # ``rollup`` cannot reconstruct one. The findings that moved are in
    # ``verdict_detail``, which is only as long as the wave actually changed.

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    handlers = {
        "status": cmd_status,
        "run": cmd_run,
        "prepare": cmd_prepare,
        "fanout": cmd_fanout,
        "commit": cmd_commit,
    }
    try:
        return handlers[args.command](args)
    except ScopeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
