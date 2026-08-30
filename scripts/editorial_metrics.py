#!/usr/bin/env python3
"""Score the editorial judge against the human marks it has accumulated.

The sibling of ``scripts/replay_dictionary_marks.py`` and
``scripts/replay_grammar_marks.py``, with one structural difference: those two
**re-run** their checker over the corpus, because enchant and LanguageTool are
local. Re-running an LLM judge costs tokens, so this script never does. It scores
what is already persisted in ``evaluations/<chunk>.json`` against what is already
marked in ``evaluations/_feedback.jsonl``. It is free, and it is the reason the
threshold can be tuned rather than guessed.

Marks join to findings by ``issue_key``, which for this judge is the explicit
``finding_key`` — a hash of ``rule`` plus the excerpt. That is what makes the
join survive a re-judge: the derived key hashes ``message``, which an LLM rewords
every run, so a message-keyed mark would silently stop matching its own finding.

Four groups of numbers:

1. **Volume.** Findings per chunk and per 1,000 words, and the share of chunks
   that came back clean. A judge that finds something everywhere has failed
   however good the individual findings look, so the clean share is a headline
   number, not a footnote.

2. **Precision.** ``resolved / (resolved + false_positive)`` over the marked
   findings, overall and per category and per rule. This is the number the whole
   feature lives or dies on. For reference, the same measure over the existing
   corpus: dictionary 8%, grammar 19%, dialogue 55%, address 66%.

3. **Adjudication.** What the second pass changed — retract rate, reclassify
   rate, and how often an attached English window was actually used. If source
   checking fires rarely and changes nothing, tighten the gate or drop it for
   some categories; if it retracts often, it has paid for itself.

4. **Anchoring.** The share of findings whose excerpt still appears verbatim in
   the chunk, counted only on chunks whose text has not drifted since the judge
   ran. An unanchored finding lands in the reader's overflow bin instead of on
   its sentence.

``--write-examples`` turns the marked corpus into the few-shot bank the judge
reads back as ``<calibration_examples>``: the findings a human accepted, and the
ones they dismissed. That is the Stage 3 loop, and it is the only part of this
script that writes anything.

Usage:
    python scripts/editorial_metrics.py
    python scripts/editorial_metrics.py --project pollyanna --out report.json
    python scripts/editorial_metrics.py --project pollyanna --write-examples
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backfill_feedback_keys import discover_projects, resolve_project  # noqa: E402
from web_ui.evaluations import chunk_text_sha, issue_key  # noqa: E402

JUDGE_NAME = "editorial"

#: How many examples of each kind to write into the few-shot bank. Enough to set
#: a threshold, few enough that the cacheable prefix stays small.
DEFAULT_EXAMPLES_PER_KIND = 8

#: Longest excerpt reproduced in the few-shot bank.
_EXAMPLE_EXCERPT_CHARS = 220


def load_marks(project_dir: Path) -> dict[str, dict[str, str]]:
    """``{chunk_id: {issue_key: feedback_type}}`` for editorial marks only.

    Keyed by content, never by position. Records written before ``issue_key``
    existed carry no key and are skipped: they can only be matched by an index
    into a list this judge rewrites on every run, and a mark pointing at whatever
    now occupies slot 3 is worse than no mark.
    """
    path = project_dir / "evaluations" / "_feedback.jsonl"
    marks: dict[str, dict[str, str]] = collections.defaultdict(dict)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return marks

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("eval_name") != JUDGE_NAME:
            continue
        key = record.get("issue_key")
        feedback = record.get("feedback_type")
        if key and feedback:
            # Later marks win: a reviewer who relabels a finding has changed
            # their mind, and the file is append-only.
            marks[str(record.get("chunk_id"))][str(key)] = str(feedback)
    return marks


def _iter_editorial_results(project_dir: Path):
    """Yield ``(chunk_id, result, chunk, payload)`` per persisted editorial verdict."""
    evaluations = project_dir / "evaluations"
    if not evaluations.exists():
        return
    for path in sorted(evaluations.glob("*.json")):
        chunk_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        result = (payload.get("judges") or {}).get(JUDGE_NAME)
        if not isinstance(result, dict):
            continue
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        try:
            chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            chunk = {}
        yield chunk_id, result, chunk, payload


def analyse_project(project_dir: Path) -> dict[str, Any]:
    """Every metric for one book."""
    marks = load_marks(project_dir)

    chunks = clean_chunks = 0
    words = findings = 0
    anchor_ok = anchor_miss = 0
    by_category: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_rule: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    labels: collections.Counter = collections.Counter()
    adjudication = collections.Counter()
    verified_chunks = 0
    examples: list[dict[str, Any]] = []

    for chunk_id, result, chunk, payload in _iter_editorial_results(project_dir):
        chunks += 1
        translated = str(chunk.get("translated_text") or "")
        words += len(translated.split())
        issues = [i for i in (result.get("issues") or []) if isinstance(i, dict)]
        findings += len(issues)
        if not issues:
            clean_chunks += 1

        metadata = result.get("metadata") or {}
        if metadata.get("verified"):
            verified_chunks += 1
            adjudication["adjudicated"] += int(metadata.get("candidates_adjudicated") or 0)
            adjudication["confirmed"] += int(metadata.get("confirmed") or 0)
            adjudication["reclassified"] += int(metadata.get("reclassified") or 0)
            adjudication["retracted"] += int(metadata.get("retracted_count") or 0)
            adjudication["source_requested"] += int(metadata.get("source_requested") or 0)
            adjudication["source_attached"] += int(metadata.get("source_attached") or 0)
            adjudication["source_used"] += int(metadata.get("source_used") or 0)

        # Anchoring is only meaningful where the judged text is the current text.
        ledger = (payload.get("eval_runs") or {}).get(JUDGE_NAME) or {}
        fresh = bool(translated) and ledger.get("text_sha") == chunk_text_sha(translated)

        chunk_marks = marks.get(chunk_id, {})
        for issue in issues:
            category = str(issue.get("category") or "UNKNOWN")
            rule = str(issue.get("rule_id") or "other")
            by_category[category]["findings"] += 1
            by_rule[rule]["findings"] += 1

            excerpt = str(issue.get("location") or "")
            if fresh and excerpt:
                if excerpt in translated:
                    anchor_ok += 1
                else:
                    anchor_miss += 1

            label = chunk_marks.get(issue_key(JUDGE_NAME, issue))
            if label:
                labels[label] += 1
                by_category[category][label] += 1
                by_rule[rule][label] += 1
                examples.append(
                    {
                        "label": label,
                        "category": category,
                        "rule": rule,
                        "severity": issue.get("severity"),
                        "excerpt": excerpt[:_EXAMPLE_EXCERPT_CHARS],
                        "message": str(issue.get("message") or ""),
                        "chunk_id": chunk_id,
                    }
                )

    return {
        "project": project_dir.name,
        "volume": {
            "chunks_judged": chunks,
            "words_judged": words,
            "findings": findings,
            "findings_per_chunk": round(findings / chunks, 2) if chunks else 0.0,
            "findings_per_1000_words": round(findings / words * 1000, 2) if words else 0.0,
            "clean_chunks": clean_chunks,
            "clean_chunk_pct": round(clean_chunks / chunks * 100, 1) if chunks else 0.0,
        },
        "precision": _precision_block(labels),
        "by_category": {k: _precision_block(v, v["findings"]) for k, v in by_category.items()},
        "by_rule": {k: _precision_block(v, v["findings"]) for k, v in by_rule.items()},
        "adjudication": _adjudication_block(adjudication, verified_chunks, chunks),
        "anchoring": {
            "checked": anchor_ok + anchor_miss,
            "anchored": anchor_ok,
            "unanchored": anchor_miss,
            "anchor_pct": round(anchor_ok / (anchor_ok + anchor_miss) * 100, 1)
            if (anchor_ok + anchor_miss)
            else None,
        },
        "_examples": examples,
    }


def _precision_block(counter: collections.Counter, findings: int = 0) -> dict[str, Any]:
    resolved = counter.get("resolved", 0)
    false_positive = counter.get("false_positive", 0)
    labelled = resolved + false_positive
    block: dict[str, Any] = {
        "labelled": labelled,
        "resolved": resolved,
        "false_positive": false_positive,
        "bad_message": counter.get("bad_message", 0),
        "missing_context_gap": counter.get("missing_context_gap", 0),
        "accept_pct": round(resolved / labelled * 100, 1) if labelled else None,
    }
    if findings:
        block["findings"] = findings
    return block


def _adjudication_block(
    counter: collections.Counter, verified: int, chunks: int
) -> dict[str, Any]:
    adjudicated = counter.get("adjudicated", 0)
    attached = counter.get("source_attached", 0)
    return {
        "verified_chunks": verified,
        "unverified_chunks": chunks - verified,
        "adjudicated": adjudicated,
        "confirmed": counter.get("confirmed", 0),
        "reclassified": counter.get("reclassified", 0),
        "retracted": counter.get("retracted", 0),
        "retract_pct": round(counter.get("retracted", 0) / adjudicated * 100, 1)
        if adjudicated
        else None,
        "reclassify_pct": round(counter.get("reclassified", 0) / adjudicated * 100, 1)
        if adjudicated
        else None,
        "source_requested": counter.get("source_requested", 0),
        "source_attached": attached,
        "source_used": counter.get("source_used", 0),
        # The number that decides whether the second pass earns its cost: of the
        # findings that got an English window, how many were actually settled by
        # it. Low and stable means tighten the gate.
        "source_used_pct": round(counter.get("source_used", 0) / attached * 100, 1)
        if attached
        else None,
    }


def merge(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Corpus-wide rollup across per-book reports."""
    volume = collections.Counter()
    labels = collections.Counter()
    adjudication = collections.Counter()
    anchor = collections.Counter()
    by_category: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_rule: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for report in reports:
        for key in ("chunks_judged", "words_judged", "findings", "clean_chunks"):
            volume[key] += report["volume"][key]
        for key in ("resolved", "false_positive", "bad_message", "missing_context_gap"):
            labels[key] += report["precision"][key]
        for key, value in report["adjudication"].items():
            if isinstance(value, int):
                adjudication[key] += value
        for key in ("checked", "anchored", "unanchored"):
            anchor[key] += report["anchoring"][key]
        for name, block in report["by_category"].items():
            for key, value in block.items():
                if isinstance(value, int):
                    by_category[name][key] += value
        for name, block in report["by_rule"].items():
            for key, value in block.items():
                if isinstance(value, int):
                    by_rule[name][key] += value

    chunks = volume["chunks_judged"]
    words = volume["words_judged"]
    return {
        "volume": {
            **dict(volume),
            "findings_per_chunk": round(volume["findings"] / chunks, 2) if chunks else 0.0,
            "findings_per_1000_words": round(volume["findings"] / words * 1000, 2)
            if words
            else 0.0,
            "clean_chunk_pct": round(volume["clean_chunks"] / chunks * 100, 1)
            if chunks
            else 0.0,
        },
        "precision": _precision_block(labels),
        "by_category": {k: _precision_block(v, v["findings"]) for k, v in by_category.items()},
        "by_rule": {k: _precision_block(v, v["findings"]) for k, v in by_rule.items()},
        "adjudication": _adjudication_block(
            adjudication,
            adjudication.get("verified_chunks", 0),
            chunks,
        ),
        "anchoring": {
            **dict(anchor),
            "anchor_pct": round(anchor["anchored"] / anchor["checked"] * 100, 1)
            if anchor["checked"]
            else None,
        },
    }


def render_examples(examples: list[dict[str, Any]], per_kind: int) -> str:
    """Render the few-shot bank the judge reads as ``<calibration_examples>``.

    Dismissed examples come first and are the more valuable half: they are the
    threshold, stated in the reviewer's own decisions rather than in adjectives.
    """
    accepted = [e for e in examples if e["label"] == "resolved"][:per_kind]
    dismissed = [e for e in examples if e["label"] == "false_positive"][:per_kind]
    if not accepted and not dismissed:
        return ""

    lines: list[str] = []
    if dismissed:
        lines.append(
            "BELOW THE THRESHOLD — a reviewer saw each of these and said it was "
            "not worth reporting. Do not report findings like them."
        )
        for example in dismissed:
            lines.append(f'- [{example["category"]}/{example["rule"]}] "{example["excerpt"]}"')
            lines.append(f'  reported as: {example["message"]}')
        lines.append("")
    if accepted:
        lines.append(
            "AT OR ABOVE THE THRESHOLD — a reviewer agreed each of these was a real "
            "defect and fixed it. Findings like them are worth reporting."
        )
        for example in accepted:
            lines.append(f'- [{example["category"]}/{example["rule"]}] "{example["excerpt"]}"')
            lines.append(f'  reported as: {example["message"]}')
    return "\n".join(lines)


def _fmt_pct(value: Optional[float]) -> str:
    return "  n/a" if value is None else f"{value:5.1f}%"


def _print_report(name: str, report: dict[str, Any]) -> None:
    volume = report["volume"]
    precision = report["precision"]
    adjudication = report["adjudication"]
    anchoring = report["anchoring"]
    print(f"\n=== {name} ===")
    print(
        f"  volume       {volume['chunks_judged']:4d} chunks  "
        f"{volume['findings']:4d} findings  "
        f"{volume['findings_per_chunk']:.2f}/chunk  "
        f"{volume['findings_per_1000_words']:.2f}/1k words  "
        f"clean {volume['clean_chunk_pct']:.1f}%"
    )
    print(
        f"  precision    {precision['labelled']:4d} labelled  "
        f"accept {_fmt_pct(precision['accept_pct'])}  "
        f"(resolved {precision['resolved']}, false_positive {precision['false_positive']})"
    )
    print(
        f"  adjudication {adjudication['adjudicated']:4d} adjudicated  "
        f"retract {_fmt_pct(adjudication['retract_pct'])}  "
        f"reclassify {_fmt_pct(adjudication['reclassify_pct'])}  "
        f"source used {_fmt_pct(adjudication['source_used_pct'])} "
        f"of {adjudication['source_attached']} attached"
    )
    print(
        f"  anchoring    {anchoring['checked']:4d} checked   "
        f"anchored {_fmt_pct(anchoring['anchor_pct'])}"
    )
    if report["by_category"]:
        print("  by category:")
        for category, block in sorted(
            report["by_category"].items(), key=lambda kv: -kv[1].get("findings", 0)
        ):
            print(
                f"    {category:18s} {block.get('findings', 0):4d} findings  "
                f"{block['labelled']:3d} labelled  accept {_fmt_pct(block['accept_pct'])}"
            )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument("--out", help="Write the full report as JSON to this path")
    parser.add_argument(
        "--write-examples",
        action="store_true",
        help="Write editorial_examples.txt into each project from its marked findings",
    )
    parser.add_argument(
        "--examples-per-kind",
        type=int,
        default=DEFAULT_EXAMPLES_PER_KIND,
        help=f"Examples of each kind in the bank (default {DEFAULT_EXAMPLES_PER_KIND})",
    )
    args = parser.parse_args(argv)

    projects_root = _REPO_ROOT / "projects"
    if args.project:
        project_dirs = [resolve_project(projects_root, slug) for slug in args.project]
    else:
        project_dirs = discover_projects(projects_root)

    reports = []
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            print(f"skipping missing project: {project_dir}", file=sys.stderr)
            continue
        report = analyse_project(project_dir)
        if report["volume"]["chunks_judged"] == 0:
            continue
        reports.append(report)

    if not reports:
        print("No persisted editorial results found. Run the judge first:")
        print("  python scripts/run_judges.py run --project <slug> --judge editorial \\")
        print("      --scope chapter:chapter_01 --persist --confirm")
        return 0

    for report in reports:
        _print_report(report["project"], report)
    if len(reports) > 1:
        _print_report("ALL BOOKS", merge(reports))

    if args.write_examples:
        for report in reports:
            text = render_examples(report["_examples"], args.examples_per_kind)
            project_dir = resolve_project(projects_root, report["project"])
            path = project_dir / "editorial_examples.txt"
            if not text:
                print(f"\n{report['project']}: no marked findings yet — no examples written")
                continue
            path.write_text(text + "\n", encoding="utf-8")
            print(f"\n{report['project']}: wrote {path}")

    if args.out:
        payload = {
            "projects": [{k: v for k, v in r.items() if k != "_examples"} for r in reports],
            "all": merge(reports) if len(reports) > 1 else None,
        }
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
