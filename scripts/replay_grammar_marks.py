"""Replay the grammar evaluator over chunks that already carry human marks.

Two jobs in one pass, both driven by ``evaluations/_feedback.jsonl``:

1. **Derive rule ids for the existing marked corpus.** Those marks predate the
   ``Issue.rule_id`` field, so the only way to learn which LanguageTool rule
   produced a given dismissal is to re-run the checker over the same text and
   join the new findings back onto the stored ones. Message strings are the
   join key; they are stable as long as the chunk text and the LanguageTool
   version have not moved.

2. **Measure precision against the human labels.** ``resolved`` means the human
   fixed a real defect; ``false_positive`` means it was noise. A rule that has
   never once produced a resolved finding can be dropped with zero recall cost.

The two jobs cannot use the same join, and getting that wrong biases the answer
badly. A ``resolved`` finding was *fixed*, so it usually no longer reproduces on
the current text -- scoring only findings that still replay counted 3 of the 18
known real defects and made every rule look worse than it is. So:

* the replay is used only to learn a **message-prefix -> rule_id lexicon**
  (LanguageTool's message is a deterministic function of the rule, once the
  trailing ``Context: '...'`` quote is stripped);
* that lexicon is then applied to *every* stored marked finding, including the
  resolved ones whose text has since changed, and precision is computed from
  those stored labels.

Costs nothing to run -- LanguageTool is local. Chunks whose text has drifted
since the evaluation ran are still replayed -- a rule's message is the same
whatever it fired on -- and are only counted under ``text_drifted``.

Usage:
    python scripts/replay_grammar_marks.py
    python scripts/replay_grammar_marks.py --project the-little-duke --min-false 2
    python scripts/replay_grammar_marks.py --simulate-ignore RULE_A,RULE_B
    python scripts/replay_grammar_marks.py --out report.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.evaluators.grammar_eval import GrammarEvaluator  # noqa: E402
from src.models import Chunk, Glossary  # noqa: E402
from scripts.backfill_feedback_keys import (  # noqa: E402
    discover_projects,
    eval_ran_at,
    resolve_project,
)
from web_ui.evaluations import chunk_text_sha, issue_key  # noqa: E402

# Only these two labels are ground truth for precision. bad_message and
# missing_context_gap say the finding was real but poorly reported, which is a
# different axis and would muddy the count whichever side it was folded into.
REAL = "resolved"
NOISE = "false_positive"


def load_marks(project_dir: Path) -> dict[str, list[tuple[str, Any, str, Any]]]:
    """chunk_id -> [(identity_kind, identity, feedback_type, ts)] in file order.

    ``identity_kind`` is ``"key"`` when the record carries an ``issue_key`` and
    ``"index"`` otherwise. Preferring the content hash matters here as much as
    it does in the reader: ``issue_index`` is a position in a list the evaluator
    rewrites on every run, so a stale index attributes one rule's dismissal to
    whichever rule now occupies the slot -- and this script is what decides
    which rules get suppressed on the strength of those dismissals.

    File order is preserved rather than collapsed here; the caller resolves each
    identity to a stored finding first, so that two records naming the same
    finding by different identities dedupe to one, latest-wins.
    """
    path = project_dir / "evaluations" / "_feedback.jsonl"
    marks: dict[str, list[tuple[str, Any, str, Any]]] = collections.defaultdict(list)
    if not path.exists():
        return marks
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("eval_name") != "grammar":
                continue
            chunk_id = rec.get("chunk_id")
            if not chunk_id:
                continue
            feedback = rec.get("feedback_type")
            ts = rec.get("ts")
            key = rec.get("issue_key")
            if key:
                marks[chunk_id].append(("key", key, feedback, ts))
                continue
            idx = rec.get("issue_index")
            if isinstance(idx, int):
                marks[chunk_id].append(("index", idx, feedback, ts))
    return marks


def rule_message_prefix(message: str) -> str:
    """The rule's own text, with the quoted context tail stripped.

    LanguageTool renders one message per rule and appends
    ``Context: '...snippet...'`` plus an optional ``(found N time(s))`` counter.
    Removing both leaves a string that identifies the rule, which is what lets a
    lexicon learned on replayable chunks be applied to stored findings whose
    text has since been edited.
    """
    head = re.split(r"\s*Context:\s*'", message or "")[0]
    head = re.sub(r"\s*\(found \d+ time\(s\)\)\s*$", "", head)
    return head.strip()


def stored_grammar_issues(evaluation: dict[str, Any]) -> Optional[list[dict]]:
    for result in evaluation.get("results") or []:
        if result.get("eval_name") == "grammar":
            return result.get("issues") or []
    return None


def learn_lexicon(
    project_dir: Path, evaluator: GrammarEvaluator, verbose: bool = False
) -> tuple[dict[str, collections.Counter], collections.Counter]:
    """Re-run the grammar evaluator to learn message-prefix -> rule_id.

    Only chunks that carry marks are replayed (they are the ones whose stored
    findings need ids), but *every* finding the re-run produces contributes to
    the lexicon -- not just the marked ones -- because a rule seen anywhere
    teaches its id everywhere.
    """
    lexicon: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    stats: collections.Counter = collections.Counter()
    marks = load_marks(project_dir)
    if not marks:
        return lexicon, stats

    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        try:
            glossary = Glossary(**json.loads(glossary_path.read_text(encoding="utf-8")))
        except Exception:
            glossary = None
    # Mirror _build_context's grammar-relevant defaults. skip_spelling is what
    # hands unknown-word spelling to the dictionary evaluator; replaying without
    # it would resurrect a class of findings that current runs never emit.
    # apply_default_ignores=False turns off GrammarEvaluator's own gates: this
    # script is what measures those rules, so it has to observe the evaluator
    # before its own conclusions are applied, or the next run would report the
    # suppressed rules as simply absent.
    context: dict[str, Any] = {
        "skip_spelling": True,
        "apply_default_ignores": False,
    }
    if glossary is not None:
        context["glossary"] = glossary

    for chunk_id in sorted(marks):
        eval_path = project_dir / "evaluations" / f"{chunk_id}.json"
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not eval_path.exists() or not chunk_path.exists():
            stats["missing_files"] += 1
            continue
        try:
            evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
            chunk_raw = json.loads(chunk_path.read_text(encoding="utf-8"))
        except Exception:
            stats["unreadable"] += 1
            continue

        text = chunk_raw.get("translated_text") or ""
        if not text.strip():
            stats["empty_translation"] += 1
            continue

        # Drifted text still teaches the lexicon (a rule's message is the same
        # whatever it fired on), so unlike the stored-mark pass this is only a
        # note, not a skip.
        recorded_sha = (
            (evaluation.get("eval_runs") or {}).get("grammar", {}).get("text_sha")
        )
        if recorded_sha and recorded_sha != chunk_text_sha(text):
            stats["text_drifted"] += 1

        try:
            chunk = Chunk(**chunk_raw)
        except Exception:
            stats["chunk_parse_failed"] += 1
            continue
        try:
            result = evaluator.evaluate(chunk, dict(context))
        except Exception as exc:  # pragma: no cover - environment dependent
            stats["evaluator_error"] += 1
            if verbose:
                print(f"  ! {chunk_id}: {exc}", file=sys.stderr)
            continue

        for issue in result.issues:
            if issue.rule_id:
                lexicon[rule_message_prefix(issue.message)][issue.rule_id] += 1
                stats["lexicon_observations"] += 1
        stats["chunks_replayed"] += 1
    return lexicon, stats


def collect_stored_marks(project_dir: Path) -> tuple[list[tuple[str, str]], int]:
    """Every usable grammar mark as (message_prefix, feedback_type), plus the
    number dropped as stale.

    Reads the stored findings rather than re-running anything, so a defect the
    human already fixed still counts -- which is the whole point: those are the
    true positives, and they are exactly what a replay-only join loses.
    """
    rows: list[tuple[str, str]] = []
    stale = 0
    marks = load_marks(project_dir)
    for chunk_id, marked in sorted(marks.items()):
        eval_path = project_dir / "evaluations" / f"{chunk_id}.json"
        if not eval_path.exists():
            continue
        try:
            evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        old = stored_grammar_issues(evaluation)
        if not old:
            continue

        # First key wins so a duplicated finding keeps the position the reader
        # would have marked; unresolvable identities are simply dropped.
        by_key: dict[str, int] = {}
        for idx, issue in enumerate(old):
            if isinstance(issue, dict):
                by_key.setdefault(issue_key("grammar", issue), idx)

        # A mark with no key can only be placed positionally, and a position is
        # only meaningful if the evaluator has not re-run since the mark was
        # made. Attributing a stale one would credit its label to whichever rule
        # now occupies the slot -- and a borrowed false_positive is how a rule
        # that does catch real defects ends up suppressed.
        ran_at = eval_ran_at(evaluation, "grammar")

        resolved: dict[int, str] = {}
        for kind, ident, feedback, ts in marked:
            if feedback not in (REAL, NOISE):
                continue
            if kind == "key":
                idx = by_key.get(ident)
            elif ran_at and isinstance(ts, str) and ts < ran_at:
                stale += 1
                continue
            elif 0 <= ident < len(old):
                idx = ident
            else:
                idx = None
            if idx is None:
                continue
            resolved[idx] = feedback

        for idx, feedback in resolved.items():
            message = (old[idx] or {}).get("message") or ""
            rows.append((rule_message_prefix(message), feedback))
    return rows, stale


def precision(real: int, false: int) -> float:
    total = real + false
    return (100.0 * real / total) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument(
        "--min-false",
        type=int,
        default=2,
        help="A rule joins the candidate ignore list when it has 0 resolved "
        "findings and at least this many false positives (default: 2)",
    )
    parser.add_argument(
        "--simulate-ignore",
        help="Comma-separated rule ids to score as if they were suppressed",
    )
    parser.add_argument("--out", help="Write the full report as JSON to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    projects_root = REPO_ROOT / "projects"
    if args.project:
        project_dirs = [resolve_project(projects_root, slug) for slug in args.project]
    else:
        # Shared with the backfill so the two agree on what the corpus is:
        # hidden group directories are included, .bak snapshots are not.
        project_dirs = discover_projects(projects_root)

    print("Starting LanguageTool (first run downloads the JAR)...", file=sys.stderr)
    evaluator = GrammarEvaluator()

    lexicon: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    stored: list[tuple[str, str]] = []
    totals: collections.Counter = collections.Counter()
    for project_dir in project_dirs:
        if not project_dir.exists():
            print(f"skip (missing): {project_dir.name}", file=sys.stderr)
            continue
        learned, stats = learn_lexicon(project_dir, evaluator, verbose=args.verbose)
        for prefix, ids in learned.items():
            lexicon[prefix].update(ids)
        rows, stale = collect_stored_marks(project_dir)
        stored.extend(rows)
        totals.update(stats)
        totals["stored_mark_stale"] += stale
        print(
            f"  {project_dir.name:<30} replayed {stats['chunks_replayed']:>3} chunks, "
            f"stored marks {len(rows):>3}",
            file=sys.stderr,
        )

    # A prefix that maps to more than one rule id cannot be attributed: several
    # LanguageTool rules share the message "Posible error de concordancia.", and
    # taking the majority would credit one rule with another's dismissals and
    # could put it on the ignore list on borrowed evidence. Drop those marks
    # instead -- an under-covered measurement is recoverable, a wrong
    # suppression is not.
    ambiguous = {p: dict(c) for p, c in lexicon.items() if len(c) > 1}
    resolved_lexicon = {
        p: c.most_common(1)[0][0] for p, c in lexicon.items() if len(c) == 1
    }

    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unmapped: collections.Counter = collections.Counter()
    for prefix, feedback in stored:
        rule_id = resolved_lexicon.get(prefix)
        if rule_id is None:
            unmapped[feedback] += 1
            if prefix in ambiguous:
                totals["stored_mark_ambiguous"] += 1
            else:
                totals["stored_mark_unmapped"] += 1
            continue
        counts[rule_id][feedback] += 1
        totals["stored_mark_mapped"] += 1

    candidates = sorted(
        rule_id
        for rule_id, c in counts.items()
        if c[REAL] == 0 and c[NOISE] >= args.min_false
    )
    real_all = sum(c[REAL] for c in counts.values())
    false_all = sum(c[NOISE] for c in counts.values())
    ignored = set(candidates)
    kept = {r: c for r, c in counts.items() if r not in ignored}
    real_kept = sum(c[REAL] for c in kept.values())
    false_kept = sum(c[NOISE] for c in kept.values())

    print()
    print("=== rules by volume (ground truth from _feedback.jsonl) ===")
    print(f"{'real':>5}{'false':>7}{'prec':>7}  rule_id")
    for rule_id, c in sorted(
        counts.items(), key=lambda kv: -(kv[1][REAL] + kv[1][NOISE])
    ):
        print(
            f"{c[REAL]:>5}{c[NOISE]:>7}{precision(c[REAL], c[NOISE]):>6.0f}%  {rule_id}"
        )

    print()
    print(f"=== candidate ignore list (0 real, >= {args.min_false} false) ===")
    for rule_id in candidates:
        print(f"  {rule_id}  ({counts[rule_id][NOISE]} false positives)")
    print(f"  -> {len(candidates)} of {len(counts)} rules")

    print()
    print("=== precision ===")
    print(
        f"  as-is:              {real_all} real / {false_all} false"
        f"   {precision(real_all, false_all):.0f}%"
    )
    print(
        f"  with ignore list:   {real_kept} real / {false_kept} false"
        f"   {precision(real_kept, false_kept):.0f}%"
    )
    print(f"  real defects lost:  {real_all - real_kept}   (must be 0)")

    if args.simulate_ignore:
        sim = {r.strip() for r in args.simulate_ignore.split(",") if r.strip()}
        s_kept = {r: c for r, c in counts.items() if r not in sim}
        s_real = sum(c[REAL] for c in s_kept.values())
        s_false = sum(c[NOISE] for c in s_kept.values())
        print()
        print(f"=== simulating --simulate-ignore ({len(sim)} rules) ===")
        print(
            f"  {s_real} real / {s_false} false   {precision(s_real, s_false):.0f}%"
            f"   real defects lost: {real_all - s_real}"
        )

    print()
    print("=== replay coverage ===")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")
    print(f"  lexicon entries: {len(resolved_lexicon)}")
    if ambiguous:
        print(f"  AMBIGUOUS prefixes (mapped to >1 rule id): {len(ambiguous)}")
        for prefix, ids in list(ambiguous.items())[:5]:
            print(f"    {ids}  <- {prefix[:70]}")
    if unmapped:
        print(
            f"  stored marks with no lexicon entry: {dict(unmapped)}"
            "  (rule did not fire anywhere on current text)"
        )

    if args.out:
        report = {
            "rules": {
                r: {"real": c[REAL], "false": c[NOISE]} for r, c in counts.items()
            },
            "candidates": candidates,
            "min_false": args.min_false,
            "precision_as_is": precision(real_all, false_all),
            "precision_with_ignore": precision(real_kept, false_kept),
            "real_lost": real_all - real_kept,
            "coverage": dict(totals),
            "lexicon_size": len(resolved_lexicon),
            "ambiguous_prefixes": ambiguous,
            "unmapped_marks": dict(unmapped),
        }
        Path(args.out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
