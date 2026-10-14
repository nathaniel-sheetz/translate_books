"""
Fold a book's scan candidates into the few questions the editor answers.

The scan raises every choice a chapter forces, and the editor confirmed they are
real (19 of 19 in the 2026-10-14 precision skim) but far too many to ask one by
one: 129 subjects for *Midnight*, 169 for *Five Little Peppers*. Most are
instances of a handful of book-wide rules, and most of the rest belong to a step
that already exists: tú/usted pairs to the address map, single terms to the
glossary, anything a house rule answers to nobody.

One job per taxonomy category per book. Its body is that category's candidates,
merged by ``subject_key`` across chapters; the worker returns rules (one
question, many subjects settled), items (a subject decided on its own, routed to
the glossary or the address map), settled subjects, house-rule proposals and
drops. ``consolidate_commit`` checks that every subject is accounted for exactly
once, so a merge can never silently lose a decision.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.decisions import decisions_dir
from src.decisions.scan import house_rules_text, subject_key
from src.decisions.taxonomy import OTHER, format_taxonomy, load_taxonomy
from src.decisions.wave import fanout_error, has_draft, inside, load_manifest, run_wave
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.llm_io import JudgeParseError, load_template, parse_judge_json, prompt_version, render

TEMPLATE = "decisions_consolidate.txt"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_decisions_consolidate``.
COMMAND = "decisions_consolidate"

RULE_DESTINATIONS = ("decisions_gate", "address_map")
ITEM_DESTINATIONS = ("glossary", "address_map")

# What the subject list shows per subject; more is noise to a sorter.
MAX_QUESTIONS = 2
MAX_OPTIONS = 5
MAX_QUOTES = 2


def _manifest_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "consolidate.manifest.json"


def _out_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "consolidated.json"


def _report_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "consolidated.md"


def _scan_candidates_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "candidates.json"


# ---------------------------------------------------------------------------
# Merging the scan's candidates into subjects
# ---------------------------------------------------------------------------

def merge_subjects(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{category: [subject, ...]}``, each subject merged across chapters by key.

    A subject is ``{subject, key, chapters, questions, options, quotes, why_hard,
    house_default}``, displayed under the spelling the scan used most, and the
    list is ordered most-raised first. Display names are unique within a category
    because the worker refers back to subjects by them.
    """
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for c in candidates:
        key = c.get("subject_key") or subject_key(c.get("subject") or "")
        if not key:
            continue
        grouped.setdefault(c.get("category") or OTHER, {}).setdefault(key, []).append(c)

    out: dict[str, list[dict[str, Any]]] = {}
    for category, by_key in grouped.items():
        subjects: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for key, rows in by_key.items():
            name = Counter(r["subject"] for r in rows).most_common(1)[0][0]
            base, n = name, 2
            while name in seen_names:
                name, n = f"{base} ({n})", n + 1
            seen_names.add(name)
            questions = list(dict.fromkeys(r["question"] for r in rows if r.get("question")))
            labels = list(dict.fromkeys(
                o["label"] for r in rows for o in r.get("options") or [] if o.get("label")
            ))
            quotes = list(dict.fromkeys(q for r in rows for q in r.get("quotes") or []))
            subjects.append({
                "subject": name,
                "key": key,
                "chapters": sorted({r["chapter_id"] for r in rows}),
                "questions": questions[:MAX_QUESTIONS],
                "options": labels[:MAX_OPTIONS],
                "quotes": quotes[:MAX_QUOTES],
                "why_hard": next((r["why_hard"] for r in rows if r.get("why_hard")), ""),
                "house_default": all(r.get("house_default") for r in rows),
            })
        subjects.sort(key=lambda s: (-len(s["chapters"]), s["subject"].casefold()))
        out[category] = subjects
    return out


def render_body(category: str, subjects: list[dict[str, Any]], taxonomy: dict[str, Any]) -> str:
    entry = next((c for c in taxonomy.get("categories") or [] if c["id"] == category), None)
    lines = [f"CATEGORY: {category}", ""]
    if entry:
        lines += [format_taxonomy({"categories": [entry]}), ""]
    else:
        lines += ["(no taxonomy entry: the scan placed these under 'other')", ""]
    lines.append(f"SUBJECTS ({len(subjects)}, most-raised first):")
    for s in subjects:
        lines.append("")
        chapters = ", ".join(c.replace("chapter_", "") for c in s["chapters"])
        flag = " · house default" if s["house_default"] else ""
        lines.append(f'- subject: "{s["subject"]}" — {len(s["chapters"])} chapter(s): {chapters}{flag}')
        for q in s["questions"]:
            lines.append(f"  question: {q}")
        if s["options"]:
            lines.append(f"  options seen: {' / '.join(s['options'])}")
        for q in s["quotes"]:
            lines.append(f'  quote: "{q}"')
        if s["why_hard"]:
            lines.append(f"  why hard: {s['why_hard']}")
    lines += ["", "Return the JSON object for this category."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# prepare / fanout
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "jobs": "one per category present in the scan's candidates",
    "subjects": "subjects across all jobs, after merging by subject_key",
    "by_category": "{category: subjects}",
    "manifest_path": "consolidate.manifest.json",
    "effective": "what the wave will run as — quote at the gate",
    "instructions": "next step",
}


def consolidate_prepare(
    project_dir: Path,
    *,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render one prompt per category from ``candidates.json``. No spend."""
    from src.harness import state as hstate
    from src.harness.profile import resolve_profile, usage_log_for

    project_dir = Path(project_dir)

    def error(message: str) -> dict[str, Any]:
        return {"status": "error", "error": message, "_schema": _PREPARE_SCHEMA}

    try:
        scan = json.loads(_scan_candidates_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return error(f"no readable scan candidates ({exc}) — run `scan-commit` first")
    try:
        taxonomy = load_taxonomy(exclude_project=project_dir.name)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return error(f"could not load the decision taxonomy: {exc}")

    merged = merge_subjects(scan.get("candidates") or [])
    if not merged:
        return error("the scan produced no usable candidates; nothing to consolidate")

    prof = resolve_profile(
        project_dir, command=COMMAND, cli=cli, worker_model=worker_model,
        usage_log=usage_log_for(project_dir, COMMAND),
    )
    cfg = hstate.load_config(project_dir)
    rendered = render(load_template(TEMPLATE), {
        "title": cfg.get("title") or project_dir.name,
        "author": cfg.get("author") or "an unknown author",
        "target_language": cfg.get("target_language") or "Spanish",
        "locale": cfg.get("locale") or "unspecified",
        "house_rules": house_rules_text(),
    })
    prefix, marker, _ = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    preamble = prefix if marker else rendered.rstrip("\n") + "\n\n"

    ddir = decisions_dir(project_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    preamble_path = ddir / "preamble.consolidate.txt"
    preamble_path.write_text(preamble, encoding="utf-8")

    jobs: list[dict[str, Any]] = []
    for category in sorted(merged, key=lambda c: -len(merged[c])):
        subjects = merged[category]
        job_id = f"consolidate.{category}"
        body = render_body(category, subjects, taxonomy)
        body_path = ddir / f"{job_id}.body.txt"
        prompt_path = ddir / f"{job_id}.prompt.txt"
        draft_path = ddir / f"{job_id}.draft.json"
        body_path.write_text(body, encoding="utf-8")
        prompt_path.write_text(preamble + body, encoding="utf-8")
        if not keep_drafts:
            draft_path.unlink(missing_ok=True)
        jobs.append({
            "id": job_id,
            "category": category,
            "subjects": {s["subject"]: s["chapters"] for s in subjects},
            "body_path": str(body_path),
            "prompt_path": str(prompt_path),
            "draft_path": str(draft_path),
        })

    manifest = {
        "project": project_dir.name,
        "prepared_at": datetime.now().isoformat(),
        "prompt_version": prompt_version(TEMPLATE),
        "scan_prompt_version": scan.get("prompt_version"),
        "preamble_path": str(preamble_path),
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "jobs": jobs,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "status": "ok",
        "jobs": len(jobs),
        "subjects": sum(len(v) for v in merged.values()),
        "by_category": {c: len(v) for c, v in merged.items()},
        "manifest_path": str(_manifest_path(project_dir)),
        "effective": prof.to_payload(),
        "instructions": "Quote the jobs and `effective` at the gate, then `consolidate-fanout`.",
        "_schema": _PREPARE_SCHEMA,
    }


def consolidate_fanout(project_dir: Path, **kwargs: Any) -> dict[str, Any]:
    """One headless wave over the prepared category jobs."""
    manifest, err = load_manifest(
        _manifest_path(Path(project_dir)),
        missing="no consolidate manifest — run `consolidate-prepare` first",
    )
    if err:
        return fanout_error(err)
    return run_wave(project_dir, manifest, command=COMMAND, **kwargs)


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

def _options(raw: Any) -> list[dict[str, str]]:
    return [
        {"label": str(o.get("label") or "").strip(), "effect": str(o.get("effect") or "").strip()}
        for o in raw or []
        if isinstance(o, dict) and str(o.get("label") or "").strip()
    ]


def _names(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw or [] if str(x).strip()]


def check_draft(draft: dict[str, Any], subjects: dict[str, list[str]]) -> dict[str, Any]:
    """Normalize one category's answer and audit it against its subject list.

    Returns ``{rules, items, settled, proposals, dropped, unknown, unaccounted,
    duplicated}``. References are matched by exact spelling first, then by
    ``subject_key``, so a worker that tidies capitalization still lands.
    """
    by_key = {subject_key(name): name for name in subjects}
    placed: Counter = Counter()
    unknown: list[str] = []

    def resolve(names: list[str]) -> list[str]:
        out = []
        for n in names:
            name = n if n in subjects else by_key.get(subject_key(n))
            if name is None:
                unknown.append(n)
                continue
            placed[name] += 1
            out.append(name)
        return out

    rules = []
    for r in draft.get("rules") or []:
        if not isinstance(r, dict) or not str(r.get("question") or "").strip():
            continue
        dest = r.get("destination") if r.get("destination") in RULE_DESTINATIONS else "decisions_gate"
        covers = resolve(_names(r.get("covers")))
        examples = [e for e in _names(r.get("examples")) if (e in subjects or subject_key(e) in by_key)]
        examples = [e if e in subjects else by_key[subject_key(e)] for e in examples]
        options = _options(r.get("options"))
        rec = r.get("recommendation")
        rules.append({
            "question": str(r["question"]).strip(),
            "options": options,
            "recommendation": rec if rec in {o["label"] for o in options} else None,
            "why": str(r.get("why") or "").strip(),
            "examples": examples[:4],
            "covers": covers,
            "destination": dest,
        })
    items = []
    for it in draft.get("items") or []:
        if not isinstance(it, dict):
            continue
        dest = it.get("destination") if it.get("destination") in ITEM_DESTINATIONS else "glossary"
        names = resolve(_names(it.get("subjects")))
        if not names:
            continue
        items.append({
            "subjects": names,
            "question": str(it.get("question") or "").strip(),
            "options": _options(it.get("options")),
            "why": str(it.get("why") or "").strip(),
            "destination": dest,
        })
    settled = [
        {"subjects": resolve(_names(s.get("subjects"))), "by": str(s.get("by") or ""),
         "answer": str(s.get("answer") or "")}
        for s in draft.get("settled") or [] if isinstance(s, dict)
    ]
    proposals = [
        {"subjects": resolve(_names(p.get("subjects"))), "rule": str(p.get("rule") or "")}
        for p in draft.get("house_rule_proposals") or [] if isinstance(p, dict)
    ]
    dropped = []
    for d in draft.get("dropped") or []:
        if not isinstance(d, dict):
            continue
        names = resolve(_names(d.get("subject")))
        if names:
            dropped.append({"subject": names[0], "reason": str(d.get("reason") or "")})
    return {
        "rules": rules,
        "items": items,
        "settled": [s for s in settled if s["subjects"]],
        "proposals": [p for p in proposals if p["subjects"]],
        "dropped": dropped,
        "unknown": unknown,
        "unaccounted": [n for n in subjects if not placed[n]],
        "duplicated": sorted(n for n, k in placed.items() if k > 1),
    }


_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some categories failed or missing) | 'error'",
    "counts": "{gate_questions, address_map, glossary, settled, house_rule_proposals, "
    "dropped, subjects}",
    "coverage": "{unaccounted, unknown, duplicated} totals — every subject must land once",
    "failed": "list of {id, problem}",
    "missing": "category jobs with no draft",
    "report_path": "consolidated.md — the gate questions and the routed items; READ THIS",
    "instructions": "next step",
}


def consolidate_commit(project_dir: Path) -> dict[str, Any]:
    project_dir = Path(project_dir)
    manifest, err = load_manifest(
        _manifest_path(project_dir), missing="no consolidate manifest — run `consolidate-prepare` first"
    )
    if err:
        return {"status": "error", "error": err, "_schema": _COMMIT_SCHEMA}

    categories: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    for job in manifest["jobs"]:
        try:
            draft_path = inside(project_dir, job.get("draft_path") or "")
        except ValueError as exc:
            failed.append({"id": job.get("id", "?"), "problem": str(exc)})
            continue
        if not has_draft(draft_path):
            missing.append(job["id"])
            continue
        try:
            draft = parse_judge_json(draft_path.read_text(encoding="utf-8"), ("category",))
        except JudgeParseError as exc:
            failed.append({"id": job["id"], "problem": str(exc)})
            continue
        checked = check_draft(draft, job.get("subjects") or {})
        categories.append({"category": job["category"], "subjects": job["subjects"], **checked})

    if not categories and (failed or missing):
        return {"status": "error", "error": "no consolidate draft parsed", "failed": failed,
                "missing": missing, "_schema": _COMMIT_SCHEMA}

    def count(dest_rules: str, dest_items: Optional[str] = None) -> int:
        n = sum(1 for c in categories for r in c["rules"] if r["destination"] == dest_rules)
        if dest_items:
            n += sum(1 for c in categories for i in c["items"] if i["destination"] == dest_items)
        return n

    counts = {
        "subjects": sum(len(c["subjects"]) for c in categories),
        "gate_questions": count("decisions_gate"),
        "address_map": count("address_map", "address_map"),
        "glossary": sum(1 for c in categories for i in c["items"] if i["destination"] == "glossary"),
        "settled": sum(len(s["subjects"]) for c in categories for s in c["settled"]),
        "house_rule_proposals": sum(len(c["proposals"]) for c in categories),
        "dropped": sum(len(c["dropped"]) for c in categories),
    }
    coverage = {
        "unaccounted": sum(len(c["unaccounted"]) for c in categories),
        "unknown": sum(len(c["unknown"]) for c in categories),
        "duplicated": sum(len(c["duplicated"]) for c in categories),
    }
    doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "prompt_version": manifest.get("prompt_version"),
        "worker_model": manifest.get("worker_model"),
        "counts": counts,
        "coverage": coverage,
        "categories": categories,
        "failed": failed,
        "missing": missing,
    }
    _out_path(project_dir).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = _report_path(project_dir)
    report_path.write_text(render_report(doc), encoding="utf-8")
    return {
        "status": "partial" if failed or missing else "ok",
        "counts": counts,
        "coverage": coverage,
        "failed": failed,
        "missing": missing,
        "report_path": str(report_path),
        "instructions": "Read report_path. Re-run `consolidate-fanout --job-ids` for failed or missing.",
        "_schema": _COMMIT_SCHEMA,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _chapters_note(names: list[str], subjects: dict[str, list[str]]) -> str:
    return ", ".join(f"{n} ({len(subjects.get(n, []))} ch.)" for n in names)


def _option_lines(options: list[dict[str, str]], recommendation: Optional[str] = None) -> list[str]:
    out = []
    for o in options:
        mark = " **(recommended)**" if recommendation and o["label"] == recommendation else ""
        out.append(f"   - {o['label']}{mark} — {o['effect']}")
    return out


def render_report(doc: dict[str, Any]) -> str:
    c = doc["counts"]
    cats = doc["categories"]
    lines = [
        f"# Decisions — {doc['project']}",
        "",
        f"{c['subjects']} scanned subjects become **{c['gate_questions']} questions at the "
        f"decisions gate**, {c['address_map']} for the address map, {c['glossary']} glossary "
        f"flags; {c['settled']} subjects settled by house rules, "
        f"{c['house_rule_proposals']} house-rule proposals, {c['dropped']} dropped.",
        "",
        "## Decisions gate",
        "",
    ]
    n = 0
    for cat in cats:
        for r in cat["rules"]:
            if r["destination"] != "decisions_gate":
                continue
            n += 1
            lines.append(f"{n}. **{r['question']}** _({cat['category']}; settles {len(r['covers'])} subjects)_")
            lines += _option_lines(r["options"], r["recommendation"])
            if r["why"]:
                lines.append(f"   _Why:_ {r['why']}")
            if r["examples"]:
                lines.append(f"   _Examples:_ {_chapters_note(r['examples'], cat['subjects'])}")
            lines.append("")
    if not n:
        lines += ["(none)", ""]

    lines += ["## Address map", ""]
    for cat in cats:
        for r in cat["rules"]:
            if r["destination"] == "address_map":
                lines.append(f"- **Rule:** {r['question']} — covers {', '.join(r['covers'])}")
        for it in cat["items"]:
            if it["destination"] == "address_map":
                lines.append(f"- {' / '.join(it['subjects'])}: {it['question']}")
    lines += ["", "## Glossary flags", ""]
    for cat in cats:
        for it in cat["items"]:
            if it["destination"] == "glossary":
                opts = " / ".join(o["label"] for o in it["options"])
                lines.append(f"- **{' / '.join(it['subjects'])}** ({cat['category']}): {it['question']}"
                             + (f" — {opts}" if opts else ""))
    lines += ["", "## Never asked", ""]
    for cat in cats:
        for s in cat["settled"]:
            lines.append(f"- {', '.join(s['subjects'])} — {s['by']}: {s['answer']}")
    lines += ["", "## House-rule proposals", ""]
    for cat in cats:
        for p in cat["proposals"]:
            lines.append(f"- {p['rule']} ({', '.join(p['subjects'])})")
    lines += ["", "## Dropped", ""]
    for cat in cats:
        for d in cat["dropped"]:
            lines.append(f"- {d['subject']} ({cat['category']}): {d['reason']}")
    problems = [
        (cat["category"], key, cat[key])
        for cat in cats for key in ("unaccounted", "unknown", "duplicated") if cat[key]
    ]
    if problems:
        lines += ["", "## Coverage problems", ""]
        for category, key, names in problems:
            lines.append(f"- {category} · {key}: {', '.join(names)}")
    return "\n".join(lines) + "\n"
