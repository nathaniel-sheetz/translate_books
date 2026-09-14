"""
The dated candidate report — the relay artifact for a scan.

One report per ``scan-commit``, in ``projects/<slug>/reports/``, alongside the
annotation-review reports. Unlike those it is written in **English**: a candidate is
a detection note addressed to the editor ("the author asserts X, which is a century
out of date"), not prose for the book's reader. The gloss that eventually does get
published is drafted later, in the conversation, and never appears here.

The report carries what the shortlist gate (G2) needs to cut on — the category, the
claim, the sentence, and the English source beside it — and the ``unusable`` section,
which is the honest accounting of what the wave proposed that could not be anchored.

The English is the part to understand. By default the scanner never saw it
(``scan.render_body``); ``scan_commit`` attaches it from the alignment afterwards.
So this page is not a transcript of what the worker read — it is the first place
the claim and the source appear together, which is why the header says which mode
the scan ran in and the preamble says what to do about it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_REASON_LABELS = {
    "no_aligned_sentence": "the es_idx has no alignment row",
    "span_not_in_sentence": "the quoted span is not in the aligned sentence "
    "(so it cannot become an anchor)",
    "missing_fields": "the candidate is missing a required field",
    "already_noted": "this sentence already carries a footnote",
    "duplicate_span": "the wave already proposed this span on this sentence",
}


def write_candidate_report(
    project_dir: Path, doc: dict[str, Any], *, stamp: Optional[str] = None
) -> Path:
    """Write ``reports/footnote_candidates_<YYYYmmdd_HHMMSS>.md`` and return its path.

    ``stamp`` is the same ``run_id`` the ``proposed`` ledger rows carry, so a
    commit that straddles a second still joins the markdown file to those rows.
    """
    project_dir = Path(project_dir)
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"footnote_candidates_{stamp}.md"
    path.write_text(render_candidate_report(doc), encoding="utf-8")
    return path


def render_candidate_report(doc: dict[str, Any]) -> str:
    candidates: list[dict] = doc.get("candidates") or []
    unusable: list[dict] = doc.get("unusable") or []
    chapters = doc.get("chapters") or []
    spanish_only = (doc.get("source_text") or "es") != "both"

    by_category: dict[str, int] = {}
    by_chapter: dict[str, list[dict]] = {}
    for row in candidates:
        by_category[row.get("category") or "?"] = (
            by_category.get(row.get("category") or "?", 0) + 1
        )
        by_chapter.setdefault(row.get("chapter_id") or "?", []).append(row)

    lines = [
        "# Footnote candidates",
        "",
        f"- **Project:** {doc.get('project')}",
        f"- **Generated:** {doc.get('committed_at')}",
        f"- **Profile:** `{doc.get('profile_path')}`",
        f"- **Chapters scanned:** {len(chapters)}"
        + (f" ({chapters[0]} … {chapters[-1]})" if chapters else ""),
        f"- **Worker model:** {doc.get('worker_model')}",
        f"- **Scan read:** "
        + ("the Spanish alone" if spanish_only else "the Spanish and the English"),
        f"- **Usable candidates:** {len(candidates)}",
        f"- **Unusable (refused before you saw them):** {len(unusable)}",
        "",
        "These are **pointers, not notes.** Each one names a claim to check. Nothing",
        "here has been researched and nothing here is a gloss yet — cut the list first,",
        "then research what survives.",
        "",
    ]
    if spanish_only:
        lines += [
            "The scanner read the Spanish alone, so it could not check what the author",
            "actually asserted. The **EN** line under each candidate is attached here",
            "from the alignment — that check belongs to you, before the research is",
            "spent. A claim the source does not support is a cut, not a note.",
            "",
        ]

    if by_category:
        lines += ["## By category", "", "| Category | Candidates |", "|---|---|"]
        for category, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {category} | {count} |")
        lines.append("")

    lines += ["## Candidates", ""]
    if not candidates:
        lines += [
            "_None. The scan found nothing in scope worth a note, which is a valid",
            "and common result — relay it as an answer, not a failure._",
            "",
        ]
    for chapter_id in sorted(by_chapter):
        lines += [f"### {chapter_id}", ""]
        for row in sorted(by_chapter[chapter_id], key=lambda r: r.get("es_idx") or 0):
            lines += [
                f"#### es_idx {row.get('es_idx')} — {row.get('category')}",
                "",
                f"- **Span:** `{row.get('quoted_span')}`",
                # Copy this into the decisions file: it is what makes the ledger
                # join back to this exact candidate rather than guess from the
                # sentence, which two candidates can share.
                f"- **Candidate key:** `{row.get('candidate_key')}`",
                f"- **Claim to check:** {row.get('claim')}",
                f"- **Why:** {row.get('why')}",
                f"- **ES:** {row.get('es_sentence')}",
                f"- **EN:** {row.get('en_sentence')}",
                "",
            ]

    lines += ["## Unusable", ""]
    if not unusable:
        lines += ["_None — every candidate validated against the alignment._", ""]
    else:
        lines += [
            "Refused at commit time and never offered as choices. A span the scanner",
            "quoted but the sentence does not contain is the one to watch: it means the",
            "worker paraphrased instead of quoting.",
            "",
            "| Chapter | es_idx | Span | Reason |",
            "|---|---|---|---|",
        ]
        for row in unusable:
            reason = row.get("reason") or "?"
            label = _REASON_LABELS.get(reason, reason)
            span = (row.get("quoted_span") or "").replace("|", "\\|")
            lines.append(
                f"| {row.get('chapter_id')} | {row.get('es_idx')} | `{span}` | {label} |"
            )
        lines.append("")

    failed = doc.get("failed") or []
    missing = doc.get("missing") or []
    if failed or missing:
        lines += ["## Chapters not scanned", ""]
        for row in failed:
            lines.append(f"- `{row.get('chapter_id')}` — {row.get('problem')}")
        for chapter_id in missing:
            lines.append(f"- `{chapter_id}` — no draft on disk; re-run the wave")
        lines.append("")

    return "\n".join(lines)


# ── the decision report ─────────────────────────────────────────────────────
# The candidate report above is the Gate 2 page: pointers, before research. This
# one is the Gate 3 page and the record of what landed — the published gloss,
# verbatim, with the marker shown where it will actually fall.
#
# The 2026-09-11 fabre2 run is why it exists. Gate 3 was run through
# `AskUserQuestion` option labels, which cannot hold an 80-word Spanish sentence
# plus a two-sentence gloss, so the operator was asked to approve copy they had
# not seen and cancelled the dialogue. A markdown file can hold it; a picker
# cannot.

_OUTCOME_LABELS = {
    "added": "written",
    "planned": "planned (nothing written)",
    "refused": "REFUSED — not on disk",
    "duplicate": "already on the book",
    "dropped": "dropped",
    "omitted": "not decided",
    "invalid": "malformed decision row",
}

_PROBLEM_LABELS = {
    "no_aligned_sentence": "no alignment row carries that es_idx",
    "sentence_not_in_body": "the aligned sentence is not in chapters/<id>.txt",
    "empty_gloss": "nothing left once the [bracket] is stripped — publishes silently",
    "multi_anchor": "more than one bracket; the extras publish verbatim",
    "no_chapter_body": "no chapters/<id>.txt",
    "duplicate": "an active note on this sentence already holds this text",
    "anchor_not_found": "the marker falls to the end of the sentence",
    "ambiguous_anchor": "the anchor recurs; the marker takes the first hit",
    "sentence_drifted": "es_idx now names a different sentence",
    "candidate_key_mismatch": "the candidate_key names another sentence; the ledger "
    "joins this note to neither",
}


def _fence(text: str) -> str:
    """Quote a gloss as a fenced block so brackets and rayas survive markdown.

    Same helper and same reason as ``src/annotations/report.py:_fence``: the whole
    point of this page is that the text is verbatim. Copied rather than imported —
    ``footnote_pass`` depends on ``src.annotations`` for data (``store``), not for
    presentation.
    """
    body = text or ""
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}\n{body}\n{fence}"


def _decision_report_filename(stamp: str, *, dry_run: bool) -> str:
    """``footnote_decisions_<stamp>[_proposal].md``.

    Two names, so a proposal can never be mistaken for a record of what landed by
    someone scrolling ``reports/``. Shared ``footnote_`` prefix so both sort beside
    ``footnote_candidates_<stamp>.md``.
    """
    suffix = "_proposal" if dry_run else ""
    return f"footnote_decisions_{stamp}{suffix}.md"


def write_decision_report(project_dir: Path, doc: dict[str, Any]) -> Path:
    """Write the run's decision report and return its path."""
    project_dir = Path(project_dir)
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / _decision_report_filename(
        doc.get("run_id") or "", dry_run=bool(doc.get("dry_run"))
    )
    path.write_text(render_decision_report(doc), encoding="utf-8")
    return path


def _problem_lines(row: dict[str, Any]) -> list[str]:
    lines = []
    for problem in row.get("problems") or []:
        code = problem.get("code") or "?"
        label = _PROBLEM_LABELS.get(code, code)
        kind = "warning" if problem.get("warning") else "refused"
        lines.append(f"- **{code}** ({kind}) — {label}")
        detail = (problem.get("detail") or "").strip()
        if detail:
            lines.append(f"  - {detail}")
    if row.get("suggested_anchor"):
        lines.append(f"- **Try this anchor instead:** `{row['suggested_anchor']}`")
    return lines


def render_decision_report(doc: dict[str, Any]) -> str:
    """One renderer for both states, branching on ``dry_run``.

    Deliberately one function: the page the operator approved and the page that
    records what landed must not drift apart in layout, for the same reason
    ``_preview`` is computed through ``endnotes._injection_point`` itself.
    """
    dry_run = bool(doc.get("dry_run"))
    rows = doc.get("rows") or []
    counts = doc.get("counts") or {}
    keeps = [r for r in rows if r.get("verdict") == "keep"]
    drops = [r for r in rows if r.get("verdict") == "drop"]
    invalid = [r for r in rows if r.get("verdict") == "invalid"]
    undecided = [r for r in rows if r.get("verdict") == "undecided"]

    # `Kept` counts decisions, beside Dropped and Not decided. The breakdown is
    # what stops a refused keep reading as a landed note in the header.
    kept_parts = []
    for outcome, word in (
        ("added", "written"),
        ("planned", "planned"),
        ("duplicate", "duplicate"),
        ("refused", "refused"),
    ):
        n = sum(1 for r in keeps if r.get("outcome") == outcome)
        if n:
            kept_parts.append(f"{n} {word}")
    kept = f"{len(keeps)} ({' · '.join(kept_parts)})" if kept_parts else str(len(keeps))

    title = "Footnote decisions — proposed" if dry_run else "Footnote decisions"
    lines = [
        f"# {title}",
        "",
        f"- **Project:** {doc.get('project')}",
        f"- **Run:** `{doc.get('run_id')}` · {doc.get('written_at')}",
        f"- **Kept:** {kept} · **Dropped:** {counts.get('dropped', 0)} · "
        f"**Not decided:** {counts.get('undecided', 0)}",
    ]
    if doc.get("worker_model"):
        lines.append(
            f"- **Candidates proposed by:** {doc.get('worker_model')} "
            f"(committed {doc.get('candidates_committed_at')})"
        )
    lines.append("")
    if dry_run:
        lines += [
            "> **`--dry-run`. Nothing is on disk.** This is the review page: every",
            "> gloss below is printed in full, with the marker shown where it will",
            "> actually fall. Read the text itself before approving it — that is the",
            "> whole reason this file exists rather than a picker.",
            "",
        ]

    lines += ["## Notes", ""]
    if not keeps:
        lines += ["_No notes in this run._", ""]
    for row in keeps:
        outcome = _OUTCOME_LABELS.get(row.get("outcome"), row.get("outcome") or "?")
        if row.get("outcome") == "duplicate" and not row.get("existing_sub_id"):
            # Only a dry run lands here: the collision is with an earlier row of
            # this same file, which is not on the book yet.
            outcome = "repeats an earlier note in this run"
        heading = f"### {row.get('chapter_id')} · es_idx {row.get('es_idx')} — {outcome}"
        if row.get("sub_id"):
            heading += f" (`{row['sub_id']}`)"
        lines += [heading, ""]
        candidate = row.get("candidate") or {}
        if candidate.get("category"):
            lines.append(f"- **Category:** {candidate['category']}")
        if candidate.get("claim"):
            lines.append(f"- **Claim checked:** {candidate['claim']}")
        if row.get("anchor"):
            lines.append(f"- **Anchor:** `{row['anchor']}`")
        if row.get("join") and row.get("join") != "none":
            lines.append(
                f"- **Candidate:** `{row.get('candidate_key')}` ({row['join']} match)"
            )
        for source in row.get("sources") or []:
            lines.append(f"- **Source:** {source}")
        if row.get("existing_sub_id"):
            lines.append(f"- **Already on the book as:** `{row['existing_sub_id']}`")
        lines.append("")
        if row.get("es_text"):
            lines += ["**Sentence:**", "", _fence(row["es_text"]), ""]
        preview = row.get("injection_preview") or ""
        if preview:
            lines += ["**Marker lands here:**", "", _fence(preview), ""]
        lines += ["**Note:**", "", _fence(row.get("content") or ""), ""]
        problems = _problem_lines(row)
        if problems:
            lines += problems + [""]

    lines += ["## Dropped", ""]
    if not drops:
        lines += ["_Nothing was dropped in this run._", ""]
    else:
        lines += [
            "Decided against, and recorded in the ledger. A later scan does not read",
            "the ledger yet, so the same span can come back as a new candidate.",
            "",
            "| Chapter | es_idx | Stage | Reason |",
            "|---|---|---|---|",
        ]
        for row in drops:
            reason = (row.get("reason") or "—").replace("|", "\\|")
            lines.append(
                f"| {row.get('chapter_id')} | {row.get('es_idx')} | "
                f"{row.get('stage')} | {reason} |"
            )
        lines.append("")

    if undecided:
        lines += [
            "## Not decided",
            "",
            "Candidates from `candidates.json` in chapters this run touched that appear",
            "in neither a keep nor a drop. **`scan-commit` replaces that file**, so",
            "anything left here is lost on the next scan — record it as a drop with a",
            "reason. On a sentence with several candidates, a keep or drop only counts",
            "for the one whose `candidate_key` it carries.",
            "",
            "| Chapter | es_idx | Claim |",
            "|---|---|---|",
        ]
        for row in undecided:
            candidate = row.get("candidate") or {}
            claim = (candidate.get("claim") or "—").replace("|", "\\|")
            lines.append(f"| {row.get('chapter_id')} | {row.get('es_idx')} | {claim} |")
        lines.append("")

    if invalid:
        lines += ["## Malformed decision rows", ""]
        for row in invalid:
            lines.append(
                f"- `{row.get('chapter_id')}` es_idx {row.get('es_idx')} — "
                f"{row.get('problem')}"
            )
        lines.append("")

    if dry_run:
        lines += [
            "---",
            "",
            "Nothing was written. Re-run `add` without `--dry-run` to land these.",
            "",
        ]
    else:
        lines += [
            "---",
            "",
            f"Appended to `{doc.get('annotations_path')}`. Run `verify`, then",
            "`python scripts/harness.py epub --project <id>` — the log line",
            "`Endnotes section appended (N notes)` must rise by exactly the number",
            "written above. That line is the only end-to-end proof.",
            "",
        ]
    return "\n".join(lines)
