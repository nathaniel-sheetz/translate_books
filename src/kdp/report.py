"""
The dated KDP report: the form to copy, the evidence behind it, what to check.

Written to ``projects/<slug>/reports/kdp_<YYYYMMDD_HHMMSS>.md`` like every
other skill report. It is the G1 gate's artifact: the operator approves (or
edits) the listing having read this, so every recommendation in it sits next
to the evidence that produced it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.kdp import work_dir
from src.kdp.listing import kdp_form, planned_writes
from src.kdp.validate import OWN_COPYRIGHT, check


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cell(value: Any) -> str:
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")


def _form_value(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(f"{i}. {v}" for i, v in enumerate(value, 1)) or "_(none)_"
    if isinstance(value, dict):
        return "\n".join(
            f"- **{k}:** " + (", ".join(f"{kk}: {vv}" for kk, vv in v.items())
                              if isinstance(v, dict) else str(v))
            for k, v in value.items()
        ) or "_(none)_"
    return str(value) if str(value).strip() else "_(blank)_"


def render(
    project_dir: Path,
    listing: dict[str, Any],
    defaults: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now()
    kdp = work_dir(project_dir)
    facts = _read(kdp / "facts.json") or {}
    comps = _read(kdp / "comps.json") or {}
    categories_evidence = _read(kdp / "categories.json") or {}
    suggestions = _read(kdp / "suggestions.json") or {}
    verdict = check(listing, defaults, facts=facts)
    plan = planned_writes(project_dir, listing, defaults)

    title = f"{listing.get('title') or project_dir.name} {listing.get('descriptor') or ''}".strip()
    out = [f"# KDP listing — {title}", "",
           f"_{now:%Y-%m-%d %H:%M} · `{project_dir.name}` · marketplace "
           f"{listing.get('marketplace') or 'Amazon.com'}_", ""]

    status = "**No errors.**" if verdict["ok"] else f"**{len(verdict['errors'])} error(s) — apply will refuse.**"
    out += [f"Check: {status} {len(verdict['warnings'])} warning(s).", ""]

    # ── the form ──
    out += ["## KDP form", "", "Copy into KDP in this order (eBook Details, then Content, then Pricing).", ""]
    for row in kdp_form(listing, defaults):
        out += [f"### {row['field']}", ""]
        if row["field"] == "Description":
            out += ["```html", str(row["value"]), "```", ""]
        else:
            out += [_form_value(row["value"]), ""]

    # ── categories ──
    out += ["## Categories", ""]
    by_node = {c.get("node"): c for c in categories_evidence.get("categories") or []}
    for i, cat in enumerate(listing.get("categories") or [], 1):
        path = " › ".join(cat.get("path") or [cat.get("name") or ""])
        out += [f"**{i}. {path}**" + (f" (node {cat['node']})" if cat.get("node") else ""), ""]
        if cat.get("rationale"):
            out += [cat["rationale"], ""]
        ev = by_node.get(cat.get("node"))
        if ev:
            sample = ", ".join(
                f"#{s['category_rank']} → overall #{s['overall']:,}" if s.get("overall") else
                f"#{s['category_rank']} → overall ?"
                for s in ev.get("sample") or []
            )
            out += [f"Demand: {sample}" if sample else "Demand: not sampled", ""]
        used_by = [c for c in comps.get("listings") or []
                   if any(r.get("node") == cat.get("node") for r in (c.get("bsr") or {}).get("categories") or [])]
        if used_by:
            out += [f"Ranked in it: {len(used_by)} of {len(comps.get('listings') or [])} comparable books.", ""]
    alternatives = listing.get("category_alternatives") or []
    if alternatives:
        out += ["Alternatives:", ""]
        out += [f"- {' › '.join(a.get('path') or [a.get('name') or ''])}"
                + (f" — {a['why']}" if a.get("why") else "") for a in alternatives]
        out.append("")

    # ── keywords ──
    out += ["## Keywords", "", "| # | Phrase | Chars | Evidence |", "|---|---|---|---|"]
    phrases = {p["phrase"]: p for p in suggestions.get("phrases") or []}
    for i, kw in enumerate(listing.get("keywords") or [], 1):
        phrase = kw.get("phrase") if isinstance(kw, dict) else str(kw)
        evidence = kw.get("evidence", "") if isinstance(kw, dict) else ""
        hit = phrases.get(phrase)
        if hit and not evidence:
            evidence = f"autocomplete: {hit['hits']} prefix(es), best position {hit['best_position']}"
        out.append(f"| {i} | {_cell(phrase)} | {len(phrase or '')} | {_cell(evidence)} |")
    out.append("")

    # ── positioning ──
    positioning = listing.get("positioning") or {}
    if positioning:
        out += ["## Reader positioning", ""]
        for key, value in positioning.items():
            value = ", ".join(value) if isinstance(value, list) else value
            out.append(f"- **{key.replace('_', ' ')}:** {value}")
        out.append("")

    # ── comps ──
    listings = comps.get("listings") or []
    if listings:
        out += ["## Comparable books", "",
                "| ASIN | Title | Publisher | Reading age | Overall BSR | Ratings | Price |",
                "|---|---|---|---|---|---|---|"]
        for c in listings:
            out.append("| " + " | ".join(_cell(v) for v in (
                c.get("asin"), (c.get("title") or "")[:70], c.get("publisher"), c.get("reading_age"),
                f"{c['bsr']['overall']:,}" if (c.get("bsr") or {}).get("overall") else "",
                c.get("ratings"), c.get("price"))) + " |")
        out.append("")
        counts: dict[tuple, int] = {}
        for c in listings:
            if c.get("language") and c["language"] != "Spanish":
                continue
            for r in (c.get("bsr") or {}).get("categories") or []:
                if r.get("store") == "digital-text":
                    counts[(r.get("node"), r.get("name"))] = counts.get((r.get("node"), r.get("name")), 0) + 1
        if counts:
            out += ["Kindle categories the Spanish-language comparable books rank in:", ""]
            for (node, name), n in sorted(counts.items(), key=lambda kv: -kv[1])[:12]:
                out.append(f"- {name} (node {node}): {n}")
            out.append("")

    # ── an owned-copyright claim and what it rests on ──
    if verdict["rights_mode"] == OWN_COPYRIGHT:
        basis = listing.get("copyright_basis") or {}
        authorship = facts.get("authorship") or {}
        footnotes = facts.get("footnotes") or {}
        out += ["## Copyright basis", "",
                "Publishing Rights is answered **I own the copyright**. KDP files translations "
                "of public-domain works under public domain, so this is the case to make if "
                "KDP asks.", "",
                basis.get("statement") or "_(no statement)_", ""]
        if basis.get("original_content"):
            out += ["Original content in this book:", ""]
            out += [f"- {item}" for item in basis["original_content"]]
            out.append("")
        if authorship.get("words_with_draft"):
            out += [f"- Final text vs. the model's drafts: {authorship['changed_words']:,} of "
                    f"{authorship['words_with_draft']:,} words changed "
                    f"({authorship['changed_fraction']:.1%})."]
        out += [f"- Sentence edits saved in the reader: {authorship.get('reader_saves_typed', 0)} typed, "
                f"{authorship.get('reader_saves_from_finding', 0)} from a checker's finding; "
                f"{authorship.get('model_fixes', 0)} applied by model judges.",
                f"- Editorial footnotes: {footnotes.get('editorial', 0)}.", ""]

    # ── compliance ──
    out += ["## Compliance", ""]
    for issue in verdict["errors"]:
        out.append(f"- ❌ `{issue['code']}` ({issue['field']}): {issue['detail']}")
    for issue in verdict["warnings"]:
        out.append(f"- ⚠️ `{issue['code']}` ({issue['field']}): {issue['detail']}")
    for flag in facts.get("rights_flags") or []:
        out.append(f"- ⚖️ `{flag['code']}`: {flag['detail']}")
    for item in listing.get("manual_checks") or []:
        out.append(f"- ☐ {item}")
    if out[-1] == "":
        out.append("- Nothing flagged.")
    out.append("")

    # ── what apply writes ──
    out += ["## What `apply` writes to the book", "", "`project.json`:", ""]
    for key, value in plan["project_json"].items():
        shown = json.dumps(value, ensure_ascii=False)
        out.append(f"- `{key}`: {shown if len(shown) < 160 else shown[:157] + '…'}")
    out.append("")
    if plan["edition_notice"]:
        out += [f"`edition_notice.json` — {plan['edition_notice']['heading'] or 'Créditos'}:", ""]
        out += ["> " + line if line else ">" for line in plan["edition_notice"]["body"].splitlines()]
        out.append("")
    return "\n".join(out)


def write(project_dir: Path, listing: dict[str, Any], defaults: dict[str, Any],
          *, now: Optional[datetime] = None) -> Path:
    now = now or datetime.now()
    path = Path(project_dir) / "reports" / f"kdp_{now.strftime('%Y%m%d_%H%M%S')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(project_dir, listing, defaults, now=now), encoding="utf-8")
    return path
