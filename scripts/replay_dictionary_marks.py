"""Replay the dictionary evaluator over the corpus and score it against human marks.

The sibling of ``scripts/replay_grammar_marks.py``, and deliberately simpler
than it. Grammar needed a message-prefix -> rule_id lexicon because a
``resolved`` finding no longer reproduces once the human has fixed the text, so
the label could not be joined back to whatever produced it. Here the join key is
the flagged token itself, carried in ``Issue.term`` (and recoverable from the
message for legacy rows via ``web_ui.evaluations.issue_term``). A token is a
token: no drift, no lexicon.

Three numbers, in increasing order of what they can veto:

1. **Recall guard.** For every ``resolved`` mark -- the human agreed the word
   was really misspelled -- check that the *current* checker still flags its
   token. Any token newly accepted is a real defect lost, and the answer must be
   0. This is the one that can veto a change.

2. **Volume.** Findings the current code produces over every evaluated chunk,
   with the morphological fallback off and then on. Off/on isolates the
   fallback's contribution; the tokenizer's is not switchable, so a true
   before/after needs the old evaluator, which is what ``--count-only`` is for::

       git stash push src/evaluators/dictionary_eval.py src/utils/text_utils.py
       python scripts/replay_dictionary_marks.py --count-only    # baseline
       git stash pop
       python scripts/replay_dictionary_marks.py --count-only    # after

   ``--count-only`` touches nothing the old evaluator lacks, so it runs against
   either revision. The findings *stored on disk* are also reported, but only as
   context: they were produced by assorted past revisions over text that has
   since been edited (see ``text_drifted``), so they are not a baseline.

3. **Precision.** ``resolved`` / (``resolved`` + ``false_positive``) over the
   marked corpus, before and after -- "before" being every labeled mark, "after"
   being the ones the current checker still produces.

Costs nothing to run: enchant is a local lookup, not an API call.

Usage:
    python scripts/replay_dictionary_marks.py
    python scripts/replay_dictionary_marks.py --project pollyanna
    python scripts/replay_dictionary_marks.py --no-replay      # marks only, fast
    python scripts/replay_dictionary_marks.py --count-only     # volume only
    python scripts/replay_dictionary_marks.py --out after.json
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.evaluators.dictionary_eval import DictionaryEvaluator  # noqa: E402
from src.models import Chunk, Glossary  # noqa: E402
from scripts.backfill_feedback_keys import (  # noqa: E402
    discover_projects,
    eval_ran_at,
    resolve_project,
)
from web_ui.evaluations import chunk_text_sha, issue_key, issue_term  # noqa: E402

EVAL_NAME = "dictionary"

# Only these two labels are ground truth. bad_message and missing_context_gap
# say the finding was real but poorly reported, a different axis entirely.
REAL = "resolved"
NOISE = "false_positive"


def load_marks(project_dir: Path) -> dict[str, list[tuple[str, Any, str, Any]]]:
    """chunk_id -> [(identity_kind, identity, feedback_type, ts)] in file order.

    ``identity_kind`` is ``"key"`` when the record carries an ``issue_key`` and
    ``"index"`` otherwise. Preferring the content hash matters as much here as
    in the reader: ``issue_index`` is a position in a list the evaluator
    rewrites on every run, so a stale index attributes one word's dismissal to
    whichever word now occupies the slot.
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
            if rec.get("eval_name") != EVAL_NAME:
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


def stored_issues(evaluation: dict[str, Any]) -> list[dict]:
    for result in evaluation.get("results") or []:
        if result.get("eval_name") == EVAL_NAME:
            return result.get("issues") or []
    return []


def collect_labeled_terms(project_dir: Path) -> tuple[list[tuple[str, str]], collections.Counter]:
    """Every usable dictionary mark as ``(term, feedback_type)``.

    Reads the *stored* findings rather than re-running anything, so a word the
    human already corrected in the text still counts -- those are the true
    positives, and a replay-only join is exactly what loses them.
    """
    rows: list[tuple[str, str]] = []
    stats: collections.Counter = collections.Counter()
    for chunk_id, marked in sorted(load_marks(project_dir).items()):
        eval_path = project_dir / "evaluations" / f"{chunk_id}.json"
        if not eval_path.exists():
            stats["mark_missing_eval"] += 1
            continue
        try:
            evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        except Exception:
            stats["mark_unreadable_eval"] += 1
            continue
        old = stored_issues(evaluation)
        if not old:
            stats["mark_empty_eval"] += len(marked)
            continue

        # First key wins, so a duplicated finding keeps the position the reader
        # would have marked.
        by_key: dict[str, int] = {}
        for idx, issue in enumerate(old):
            if isinstance(issue, dict):
                by_key.setdefault(issue_key(EVAL_NAME, issue), idx)

        # A mark with no key can only be placed positionally, and a position is
        # meaningful only if the evaluator has not re-run since the mark was
        # made. Attributing a stale one would credit its label to whichever word
        # now sits in the slot.
        ran_at = eval_ran_at(evaluation, EVAL_NAME)

        resolved: dict[int, str] = {}
        for kind, ident, feedback, ts in marked:
            if feedback not in (REAL, NOISE):
                continue
            if kind == "key":
                idx = by_key.get(ident)
            elif ran_at and isinstance(ts, str) and ts < ran_at:
                stats["mark_stale_index"] += 1
                continue
            elif 0 <= ident < len(old):
                idx = ident
            else:
                idx = None
            if idx is None:
                stats["mark_unresolvable"] += 1
                continue
            resolved[idx] = feedback

        for idx, feedback in resolved.items():
            term = issue_term(EVAL_NAME, old[idx] or {})
            if not term:
                stats["mark_no_term"] += 1
                continue
            rows.append((term, feedback))
    return rows, stats


#: Structural markers ``evaluate()`` blanks before it tokenizes anything --
#: ``[IMAGE:...]``, ``[CAPTION]``, ``[FOOTNOTE:N]``. A mark made before the
#: blanking existed was recorded against the bare word left over from the
#: marker, so that is the surface form stored on disk; the marker's brackets and
#: index are long gone and no amount of re-blanking recovers them. Matching the
#: token is therefore the only way ``still_flagged`` can agree with the
#: evaluator, and agreeing is the whole point -- a harness that reports a
#: finding the evaluator no longer emits cannot attribute the removal.
_BLANKED_MARKER_TOKENS = frozenset({"IMAGE", "CAPTION", "FOOTNOTE"})


def still_flagged(evaluator: DictionaryEvaluator, term: str) -> bool:
    """Would the current checker report *term*?

    Runs the real tokenizer over the stored surface form first, because the
    tokenizer is half of what changed: a mark recorded against ``_sí_`` now
    tokenizes to ``sí``, which is a word, and the finding is correctly gone.

    The glossary is deliberately *not* consulted. Glossary membership is a
    per-book editorial decision that keeps changing under the corpus, so folding
    it in here would credit the code for removals a human made by adding a name
    to a list.
    """
    tokens = [word for word, _ in evaluator._tokenize_with_positions(term)]
    for token in tokens:
        if evaluator._is_special_case(token):
            continue
        if token in _BLANKED_MARKER_TOKENS:
            continue
        if evaluator._check_spanish_word(token):
            continue
        return True
    return False


def removal_bucket(evaluator: DictionaryEvaluator, term: str) -> str:
    """Mechanical reason the current checker no longer reports *term*.

    Only the causes that can be read off the token are named; anything else is
    ``other`` rather than guessed at.
    """
    if "_" in term:
        return "markdown_underscore"
    if any(
        token in _BLANKED_MARKER_TOKENS
        for token, _ in evaluator._tokenize_with_positions(term)
    ):
        return "blanked_marker"
    if any(ch.isdigit() for ch in term):
        return "digits"
    tokens = [w for w, _ in evaluator._tokenize_with_positions(term)]
    for token in tokens:
        if evaluator._is_special_case(token):
            continue
        if evaluator._check_spanish_word(token, apply_morphology=False):
            continue
        # Raw lookup still rejects it, so the fallback is what accepted it.
        return "morphology"
    return "other"


def load_glossary(project_dir: Path) -> Optional[Glossary]:
    path = project_dir / "glossary.json"
    if not path.exists():
        return None
    try:
        return Glossary(**json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def replay_project(
    project_dir: Path,
    evaluator: DictionaryEvaluator,
    verbose: bool = False,
    count_only: bool = False,
) -> collections.Counter:
    """Re-run the dictionary evaluator over every evaluated chunk in the project.

    With *count_only*, runs the evaluator exactly once per chunk with an empty
    context -- nothing the old revision of the evaluator does not already
    support, so the same command measures either side of the change.
    """
    stats: collections.Counter = collections.Counter()
    eval_dir = project_dir / "evaluations"
    if not eval_dir.is_dir():
        return stats
    glossary = load_glossary(project_dir)

    for eval_path in sorted(eval_dir.glob("*.json")):
        if eval_path.name.startswith("_"):
            continue
        try:
            evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        except Exception:
            stats["unreadable"] += 1
            continue
        old = stored_issues(evaluation)
        if not any(
            r.get("eval_name") == EVAL_NAME for r in (evaluation.get("results") or [])
        ):
            continue
        stats["evaluations"] += 1
        stats["stored_findings"] += len(old)

        chunk_path = project_dir / "chunks" / f"{eval_path.stem}.json"
        if not chunk_path.exists():
            stats["missing_chunk"] += 1
            continue
        try:
            chunk_raw = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunk = Chunk(**chunk_raw)
        except Exception:
            stats["chunk_parse_failed"] += 1
            continue
        if not (chunk.translated_text or "").strip():
            stats["empty_translation"] += 1
            continue

        # Drift is reported, not skipped: the replay is measuring the checker,
        # and a checker's behavior on the current text is the thing to measure.
        recorded_sha = (
            (evaluation.get("eval_runs") or {}).get(EVAL_NAME, {}).get("text_sha")
        )
        if recorded_sha and recorded_sha != chunk_text_sha(chunk.translated_text):
            stats["text_drifted"] += 1

        base: dict[str, Any] = {}
        if glossary is not None:
            base["glossary"] = glossary
        try:
            if count_only:
                stats["replay_current"] += len(evaluator.evaluate(chunk, dict(base)).issues)
            else:
                off = evaluator.evaluate(chunk, dict(base, apply_morphology=False))
                on = evaluator.evaluate(chunk, dict(base, apply_morphology=True))
                stats["replay_no_morphology"] += len(off.issues)
                stats["replay_with_morphology"] += len(on.issues)
                stats["replay_current"] += len(on.issues)
        except Exception as exc:  # pragma: no cover - environment dependent
            stats["evaluator_error"] += 1
            if verbose:
                print(f"  ! {eval_path.stem}: {exc}", file=sys.stderr)
            continue
        stats["replayed"] += 1
    return stats


def precision(real: int, false: int) -> float:
    total = real + false
    return (100.0 * real / total) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument(
        "--no-replay",
        action="store_true",
        help="Score the marked corpus only; skip the full re-run over every chunk",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Count findings over every evaluated chunk and stop. Uses nothing "
        "the pre-change evaluator lacks, so the same command produces the "
        "before and after numbers (see the module docstring)",
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

    evaluator = DictionaryEvaluator()

    labeled: list[tuple[str, str]] = []
    totals: collections.Counter = collections.Counter()
    for project_dir in project_dirs:
        if not project_dir.exists():
            print(f"skip (missing): {project_dir.name}", file=sys.stderr)
            continue
        rows: list[tuple[str, str]] = []
        if not args.count_only:
            rows, stats = collect_labeled_terms(project_dir)
            labeled.extend(rows)
            totals.update(stats)
        replay = collections.Counter()
        if not args.no_replay:
            replay = replay_project(
                project_dir, evaluator, verbose=args.verbose, count_only=args.count_only
            )
            totals.update(replay)
        print(
            f"  {project_dir.name:<34} marks {len(rows):>4}"
            f"   evals {replay['evaluations']:>4}"
            f"   stored {replay['stored_findings']:>5}"
            f" -> {replay['replay_current']:>5}",
            file=sys.stderr,
        )

    if args.count_only:
        print()
        print("=== findings over the whole corpus (current code) ===")
        print(f"  evaluated chunks replayed: {totals['replayed']}")
        print(f"  findings:                  {totals['replay_current']}")
        print(f"  (stored on disk, for context only: {totals['stored_findings']})")
        if args.out:
            report = {
                "count_only": True,
                "evaluated_chunks": totals["replayed"],
                "findings": totals["replay_current"],
                "stored_findings": totals["stored_findings"],
                "coverage": dict(totals),
            }
            Path(args.out).write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"\nwrote {args.out}")
        return 0

    # --- 1. recall guard -----------------------------------------------------
    real_terms = sorted({t for t, f in labeled if f == REAL})
    lost = [t for t in real_terms if not still_flagged(evaluator, t)]

    print()
    print("=== recall guard: resolved marks (real defects the human fixed) ===")
    print(f"  distinct resolved tokens: {len(real_terms)}")
    print(f"  NEWLY ACCEPTED (must be 0): {len(lost)}")
    for term in lost:
        print(f"    ! {term}   ({removal_bucket(evaluator, term)})")

    # --- 2. precision over the marked corpus --------------------------------
    before = collections.Counter(f for _, f in labeled)
    after = collections.Counter(
        f for t, f in labeled if still_flagged(evaluator, t)
    )
    removed_by_bucket: collections.Counter = collections.Counter()
    for term, feedback in labeled:
        if not still_flagged(evaluator, term):
            removed_by_bucket[removal_bucket(evaluator, term)] += 1

    print()
    print("=== precision over the marked corpus ===")
    print(
        f"  before:  {before[REAL]} real / {before[NOISE]} false"
        f"   {precision(before[REAL], before[NOISE]):.1f}%"
    )
    print(
        f"  after:   {after[REAL]} real / {after[NOISE]} false"
        f"   {precision(after[REAL], after[NOISE]):.1f}%"
    )
    print(f"  marks removed: {len(labeled) - sum(after.values())} of {len(labeled)}")
    for bucket, count in removed_by_bucket.most_common():
        print(f"    {bucket:<22} {count}")

    # --- 3. volume over the whole corpus ------------------------------------
    if not args.no_replay:
        off = totals["replay_no_morphology"]
        on = totals["replay_with_morphology"]

        def pct(n: int, of: int) -> str:
            return f"{100.0 * n / of:.1f}%" if of else "n/a"

        print()
        print("=== findings over the whole corpus (current code) ===")
        print(f"  morphology OFF:  {off}")
        print(
            f"  morphology ON:   {on}"
            f"   (the fallback removes {off - on}, {pct(off - on, off)})"
        )
        print(
            f"  stored on disk:  {totals['stored_findings']}"
            "   <- context only, not a baseline: assorted past revisions over "
            "text that has since been edited"
        )
        print("  for a real before/after, see --count-only in the module docstring")

    print()
    print("=== coverage ===")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")

    if args.out:
        report = {
            "resolved_terms": real_terms,
            "resolved_terms_newly_accepted": lost,
            "precision_before": precision(before[REAL], before[NOISE]),
            "precision_after": precision(after[REAL], after[NOISE]),
            "marks_before": dict(before),
            "marks_after": dict(after),
            "removed_by_bucket": dict(removed_by_bucket),
            "findings_morphology_off": totals["replay_no_morphology"],
            "findings_morphology_on": totals["replay_with_morphology"],
            "coverage": dict(totals),
        }
        Path(args.out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")

    return 1 if lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
