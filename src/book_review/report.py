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


def describe_patch(patch: dict[str, Any]) -> str:
    """One line a human can approve or refuse."""
    kind = patch["type"]
    if kind == "glossary.rekey":
        return f"glossary: rekey `{patch['english']}` → `{patch['new_english']}`"
    if kind == "glossary.add_variant":
        return f"glossary: add `{patch['new_english']}` as another key for `{patch['english']}`"
    if kind == "glossary.edit":
        changes = ", ".join(f"{k} = {json.dumps(v, ensure_ascii=False)}"
                            for k, v in patch.items() if k not in ("type", "english"))
        return f"glossary: edit `{patch['english']}`: {changes}"
    if kind == "glossary.split_sense":
        parts = "; ".join(f"`{e['english']}` → {e['spanish']}" for e in patch["entries"])
        return f"glossary: split `{patch['english']}` into {parts}"
    if kind == "glossary.add":
        return f"glossary: add `{patch['english']}` → {patch['spanish']}"
    if kind == "glossary.remove":
        return f"glossary: remove `{patch['english']}`"
    if kind == "style.replace_text":
        return f"style guide: replace “{_clip(patch['before'], 160)}” with “{_clip(patch['after'], 200)}”"
    if kind == "style.append":
        return f"style guide: append “{_clip(patch['text'], 220)}”"
    if kind == "address.set_direction":
        a, b = (patch["a"], patch["b"]) if patch["direction"] == "a_to_b" else (patch["b"], patch["a"])
        return f"address map: {a}→{b} uses {patch['form']}"
    if kind == "address.replace_global_rules_text":
        return (f"address map global rules: replace “{_clip(patch['before'], 140)}” with "
                f"“{_clip(patch['after'], 180)}”")
    if kind == "judge.dismiss":
        return f"dismiss {len(patch['finding_ids'])} findings as {patch['feedback_type']}"
    if kind in ("rejudge", "retranslate"):
        return f"{kind} {', '.join(patch['chapters'][:10])}" + (f" ({patch['why']})" if patch.get("why") else "")
    if kind == "house.rule_proposal":
        return f"house rule proposal: “{_clip(patch['rule'], 200)}”"
    return f"manual: {_clip(patch.get('description') or '', 240)}"


def render_recommendations(corpus: BookCorpus, doc: dict[str, Any]) -> str:
    recs = doc.get("recommendations") or []
    lines = [
        f"# Book review — {corpus.slug}",
        "",
        f"_{date.today().isoformat()} · {len(recs)} recommendations from "
        f"{doc.get('worker_model') or 'the synthesis model'} ({doc.get('cli') or 'headless'}). "
        "Nothing has been changed. Pick recommendations by id to apply them._",
        "",
    ]
    cov = doc.get("coverage") or {}
    owed = {k: v.get("owed") for k, v in (cov.get("judges") or {}).items() if v.get("owed")}
    if owed:
        lines += ["**Not yet judged:** " + "; ".join(f"{k} owes {len(v)} chapters" for k, v in owed.items()), ""]
    if doc.get("omitted"):
        lines += [f"**Shown in less detail for budget:** "
                  + ", ".join(f"`{o['signal_id']}` ({o['from']}→{o['to']})" for o in doc["omitted"][:12]), ""]
    labels = {1: "Fix before translating more", 2: "Fix before publishing", 3: "Worth doing"}
    for priority in (1, 2, 3):
        group = [r for r in recs if r["priority"] == priority]
        if not group:
            continue
        lines += [f"## {labels[priority]} ({len(group)})", ""]
        for r in group:
            head = f"### {r['id']} · {r['title']}"
            lines.append(head)
            meta = f"_{r['theme']} · confidence {r['confidence']}"
            if r.get("conflicts_with"):
                meta += f" · conflicts with {', '.join(r['conflicts_with'])}"
            lines += [meta + "_", "", r.get("rationale") or "", ""]
            for n, p in enumerate(r["patches"], 1):
                lines.append(f"{n}. {describe_patch(p)}")
            for p in r.get("follow_ups") or []:
                lines.append(f"- then: {describe_patch(p)}")
            if r.get("evidence"):
                shown = ", ".join(f"`{e}`" for e in r["evidence"][:6])
                more = f" and {len(r['evidence']) - 6} more" if len(r["evidence"]) > 6 else ""
                lines.append(f"\nEvidence: {shown}{more}")
            if r.get("problems"):
                lines.append(f"\n_Checks: {'; '.join(r['problems'])}_")
            lines.append("")
    if doc.get("no_action"):
        lines += ["## No action needed", ""]
        lines += [f"- `{n['signal_id']}`: {n['reason']}" for n in doc["no_action"]]
        lines.append("")
    if doc.get("rejected"):
        lines += ["## Proposals that failed validation", ""]
        lines += [f"- {r.get('title') or '(untitled)'}: {'; '.join(r['problems'])}" for r in doc["rejected"]]
        lines.append("")
    lines.append(f"Coded signals behind these: `reports/book_review_signals_{date.today().isoformat()}.md`.")
    return "\n".join(lines).rstrip() + "\n"


def write_recommendations(corpus: BookCorpus, doc: dict[str, Any], *, today: Optional[str] = None) -> str:
    today = today or date.today().isoformat()
    out_dir = reports_dir(corpus.project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"book_review_{today}.md"
    path.write_text(render_recommendations(corpus, doc), encoding="utf-8")
    return str(path)


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
