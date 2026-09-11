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
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

_REASON_LABELS = {
    "no_aligned_sentence": "the es_idx has no alignment row",
    "span_not_in_sentence": "the quoted span is not in the aligned sentence "
    "(so it cannot become an anchor)",
    "missing_fields": "the candidate is missing a required field",
    "already_noted": "this sentence already carries a footnote",
}


def write_candidate_report(project_dir: Path, doc: dict[str, Any]) -> Path:
    """Write ``reports/footnote_candidates_<YYYYmmdd_HHMMSS>.md`` and return its path."""
    project_dir = Path(project_dir)
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"footnote_candidates_{stamp}.md"
    path.write_text(render_candidate_report(doc), encoding="utf-8")
    return path


def render_candidate_report(doc: dict[str, Any]) -> str:
    candidates: list[dict] = doc.get("candidates") or []
    unusable: list[dict] = doc.get("unusable") or []
    chapters = doc.get("chapters") or []

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
        f"- **Usable candidates:** {len(candidates)}",
        f"- **Unusable (refused before you saw them):** {len(unusable)}",
        "",
        "These are **pointers, not notes.** Each one names a claim to check. Nothing",
        "here has been researched and nothing here is a gloss yet — cut the list first,",
        "then research what survives.",
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
