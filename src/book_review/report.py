"""Render a book review as markdown for the operator.

The coded-only report (``signals`` verb) lists what the detectors found, worst
first, each with a few of the findings behind it, so a human can act on it
before any synthesis model has run.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src.book_review import reports_dir, review_dir
from src.book_review.corpus import BookCorpus, Finding

_SECTIONS = (
    ("dead_glossary_keys", "Glossary entries the translator did not get"),
    ("address_style_conflicts", "Forms of address: the style guide, the map and the judges disagree"),
    ("recurring_rules", "Rules that fire across many chapters"),
    ("false_positive_clusters", "Dismissals bunched on one rule"),
)

_EXAMPLES = 4


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _example(f: Finding) -> str:
    line = f"  - {f.chapter_id} · {f.judge} `{f.rule}`"
    if f.mark:
        line += f" · marked **{f.mark}**"
    line += f": {_clip(f.message, 200)}"
    if f.location:
        line += f"\n    «{_clip(f.location, 120)}»"
        if f.suggestion:
            line += f" → «{_clip(f.suggestion, 120)}»"
    return line


def _metrics_line(signal: dict[str, Any]) -> str:
    m = signal.get("metrics") or {}
    detector = signal["detector"]
    if detector == "dead_glossary_keys":
        if m.get("state") == "absent":
            return "Keys: " + ", ".join(f"`{k}`" for k in m.get("keys", []))
        parts = [f"→ `{m.get('spanish')}`", f"reached {m.get('shipped_chunks')} chunks, "
                 f"missed {m.get('missed_chunks')}"]
        if m.get("reasons"):
            parts.append("why: " + ", ".join(f"{k} ({v})" for k, v in m["reasons"].items()))
        if m.get("blind_spot_findings"):
            parts.append(f"{m['blind_spot_findings']} judge findings blame the translator for it")
        if m.get("context"):
            parts.append(f"context (never shipped): “{_clip(m['context'], 140)}”")
        return "; ".join(parts)
    if detector == "address_style_conflicts":
        if m.get("clause"):
            return f"{m['source']}: “{_clip(m['clause'], 220)}” vs map {m.get('map_forms')}"
        return f"address judge {m.get('address_expected')} vs editorial {m.get('editorial_pushed')}"
    if detector == "recurring_rules":
        rate = m.get("accept_rate")
        return (f"{m.get('label')}; marks {m.get('marks') or 'none'}"
                + (f"; accepted {int(rate * 100)}%" if rate is not None else ""))
    if detector == "false_positive_clusters":
        return f"{m.get('label')}; marks {m.get('marks')}; rules {m.get('rules')}"
    return json.dumps(m, ensure_ascii=False)[:200]


def render_signals(corpus: BookCorpus, result: dict[str, Any]) -> str:
    by_id = {f.id: f for f in corpus.findings}
    lines = [
        f"# Book review — {corpus.slug} (coded signals)",
        "",
        f"_{date.today().isoformat()} · {result['findings']} judge findings "
        f"({', '.join(f'{k} {v}' for k, v in result['findings_by_judge'].items())}). "
        "No model has read these yet; this is what the detectors found._",
        "",
    ]
    cov = result.get("coverage") or {}
    owed = {k: v.get("owed") for k, v in (cov.get("judges") or {}).items() if v.get("owed")}
    if owed or cov.get("unverified_editorial_chunks"):
        lines.append("**Not yet seen:** " + "; ".join(
            [f"{k} owes {len(v)} chapters" for k, v in owed.items()]
            + ([f"{cov['unverified_editorial_chunks']} editorial chunks await pass 2"]
               if cov.get("unverified_editorial_chunks") else [])
        ))
        lines.append("")
    if not cov.get("address_map", True):
        lines.append("_No address map: the address judge and the address checks did not run._\n")

    for detector, heading in _SECTIONS:
        signals = [s for s in result["signals"] if s["detector"] == detector]
        lines.append(f"## {heading} ({len(signals)})")
        lines.append("")
        if not signals:
            lines.append("_Nothing found._\n")
            continue
        for s in signals:
            lines.append(f"### {s['title']}")
            lines.append(_metrics_line(s))
            if s.get("chapters"):
                chapters = s["chapters"]
                shown = ", ".join(chapters[:12]) + (" …" if len(chapters) > 12 else "")
                lines.append(f"Chapters: {shown}")
            examples = [by_id[i] for i in s.get("finding_ids", []) if i in by_id][:_EXAMPLES]
            for f in examples:
                lines.append(_example(f))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_signals(corpus: BookCorpus, result: dict[str, Any], *, today: Optional[str] = None) -> dict[str, str]:
    """Write ``signals.json`` and the dated coded-only report; return both paths."""
    today = today or date.today().isoformat()
    work = review_dir(corpus.project_dir)
    work.mkdir(parents=True, exist_ok=True)
    signals_path = work / "signals.json"
    signals_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    out_dir = reports_dir(corpus.project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"book_review_signals_{today}.md"
    report_path.write_text(render_signals(corpus, result), encoding="utf-8")
    return {"signals_path": str(signals_path), "report_path": str(report_path)}
