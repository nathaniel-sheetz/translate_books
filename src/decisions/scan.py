"""
The decisions scan: one job per English chapter, read against the taxonomy.

Shaped like :mod:`src.footnote_pass.scan` and :mod:`src.triage.pass_`
(prepare / fanout / commit), with three differences that follow from running
*before* translation:

**The body is the English source.** There is no translation yet and no
alignment, so a candidate cannot be anchored to an ``es_idx``. It carries short
verbatim ``quotes`` instead, and ``scan_commit`` checks each one against the
chapter text: a candidate with no quote that is found is ``unusable``.

**Only house-wide rules go into the prompt.** ``rules_for(...,
audience=AUDIENCE_DECISIONS)`` (house rules in scope, every enforce mode) and
never ``load_style_rules``: on a finished book the per-book ``style_rules.json`` holds
the very decisions the scan is meant to find, and a replay that shows it to the
scanner measures nothing. A new book has no sidecar yet either way.

**The book's own taxonomy examples are held out.** See
:func:`src.decisions.taxonomy.load_taxonomy`; the count held out is on the
manifest so a replay can show it did.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.decisions import decisions_dir
from src.decisions.taxonomy import OTHER, category_ids, format_taxonomy, load_taxonomy
from src.decisions.wave import fanout_error, has_draft, inside, load_manifest, run_wave
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.llm_io import (
    JudgeParseError,
    load_template,
    parse_judge_json,
    prompt_version,
    render,
)

logger = logging.getLogger(__name__)

TEMPLATE = "decisions_scan.txt"

# Wave type, for ``state.COMMAND_EFFORT_DEFAULTS`` / ``headless_effort_decisions_scan``.
COMMAND = "decisions_scan"

# A chapter longer than this is scanned in paragraph-aligned windows. The longest
# chapter in the three replay books is 6.3k words, so this rarely fires.
WINDOW_WORDS = 8000

REQUIRED_FIELDS = ("chapter_id", "candidates")
CANDIDATE_FIELDS = ("category", "subject", "question", "quotes")

UNUSABLE_FIELDS = "missing_fields"
UNUSABLE_CATEGORY = "unknown_category"
UNUSABLE_QUOTE = "no_quote_found"


def _manifest_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "scan.manifest.json"


def _candidates_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "candidates.json"


def _report_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "candidates.md"


def rulings_path(project_dir: Path) -> Path:
    return decisions_dir(project_dir) / "rulings.jsonl"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def chapter_ids(project_dir: Path) -> list[str]:
    """Body chapters in book order, front and back matter skipped when known."""
    from src.harness.address_sample import _chapter_ids_in_order

    return _chapter_ids_in_order(Path(project_dir))


def split_windows(text: str, max_words: int = WINDOW_WORDS) -> list[str]:
    """``text`` as one window, or several on paragraph breaks, each at most ``max_words``.

    A single paragraph longer than ``max_words`` stays whole in its own window.
    """
    if len(text.split()) <= max_words:
        return [text]
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    windows: list[list[str]] = [[]]
    count = 0
    for para in paragraphs:
        n = len(para.split())
        if windows[-1] and count + n > max_words:
            windows.append([])
            count = 0
        windows[-1].append(para)
        count += n
    return ["\n\n".join(w) for w in windows]


def book_signals(project_dir: Path) -> str:
    """The present ``text_features.json`` features, one line each, or a placeholder."""
    path = Path(project_dir) / "text_features.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(no text features measured for this book)"
    lines: list[str] = []
    total = doc.get("total_words")
    if total:
        lines.append(f"- length: {total} words")
    for name, feat in (doc.get("features") or {}).items():
        if not isinstance(feat, dict) or not feat.get("present"):
            continue
        line = f"- {name}: {feat.get('count', '?')}"
        evidence = [str(e).strip() for e in feat.get("evidence") or [] if str(e).strip()]
        if evidence:
            sample = evidence[0].replace("\n", " ")
            line += f' (e.g. "{sample[:140]}")'
        lines.append(line)
    return "\n".join(lines) or "(no text features measured for this book)"


def house_rules_text(project_dir: Optional[Path] = None) -> str:
    """Every house rule in scope for the book, whatever its ``enforce`` mode.

    The decisions stage reads the whole house layer: a draft-only default or a
    mechanical fix settles a question as surely as a prompt rule does. Never the
    book's own sidecar rules (see the module docstring).
    """
    from src.judges.context import AUDIENCE_DECISIONS, rules_for

    lines = []
    for rule in rules_for(project_dir, None, audience=AUDIENCE_DECISIONS):
        rule_id = str(rule.get("id") or "").strip()
        text = str(rule.get("rule") or "").strip()
        if not rule_id or not text:
            continue
        note = str(rule.get("note") or "").strip()
        scope = str(rule.get("scope") or "").strip()
        line = f'- "{rule_id}": {text}' + (f" ({note})" if note else "")
        if scope and scope != "every_book":
            line += f" [{scope}]"
        lines.append(line)
    return "\n".join(lines) or "(none)"


#: How many sibling-glossary renderings the decisions prompts list. The
#: lexicon is listed whole; precedent is evidence, so a long series is capped.
MAX_PRECEDENT_TERMS = 80


def house_vocabulary_text(project_dir: Optional[Path]) -> str:
    """The house lexicon and the group siblings' choices that bear on this book.

    A subject they cover is settled (``by: "house_lexicon"`` or
    ``by: "precedent"``) unless something in this book argues for departing:
    "copy the sibling books' horse vocabulary?" is a standing practice, not a
    question.
    """
    if project_dir is None:
        return "(no book given)"
    from src.house import book_slug, read_body
    from src.house.groups import group_ids_of
    from src.house.lexicon import entries_for, format_entry
    from src.house.precedent import format_concordance, leading_rendering, load_precedent, style_votes

    source = read_body(project_dir)
    parts: list[str] = []
    lexicon = entries_for(group_ids_of(book_slug(project_dir)), source)
    if lexicon:
        parts.append(
            "House lexicon (every book in scope renders these the same way; a word with "
            "no house rendering yet is still decided per book):\n"
            + "\n".join(f"- {format_entry(e)}" for e in lexicon)
        )
    try:
        precedent = load_precedent(project_dir)
    except Exception as exc:  # noqa: BLE001 - precedent is evidence, not a prerequisite
        logger.warning("No precedent for %s: %s", project_dir, exc)
        precedent = {}
    sibs = precedent.get("siblings") or []
    if sibs:
        names = ", ".join(f"{s['slug']} ({s['kind']})" for s in sibs)
        lines = []
        for entry in list((precedent.get("glossary") or {}).values())[:MAX_PRECEDENT_TERMS]:
            lead = leading_rendering(entry)
            if lead is None:
                continue
            line = f"- {entry['english']} → {lead['spanish']} ({', '.join(lead['slugs'])})"
            if entry.get("divergent"):
                others = sorted({r["spanish"] for r in entry["renderings"]} - {lead["spanish"]})
                line += f"; also rendered {', '.join(others)}"
            evidence = format_concordance(entry)
            if evidence:
                line += f"; {evidence}"
            lines.append(line)
        style_lines = []
        for qid in sorted(precedent.get("style") or {}):
            votes = style_votes(precedent, qid)
            total = sum(len(v["slugs"]) for v in votes)
            style_lines.append(
                f"- {qid}: " + "; ".join(f"{v['label']} ({len(v['slugs'])} of {total})" for v in votes)
            )
        block = f"Earlier books in this book's groups: {names}."
        if lines:
            block += "\nRenderings already in use there, for terms this book also has:\n" + "\n".join(lines)
        if style_lines:
            block += "\nStyle answers there:\n" + "\n".join(style_lines)
        parts.append(block)
    return "\n\n".join(parts) or "(none: the book is in no group and no house lexicon entry occurs in it)"


def house_defaults(project_dir: Optional[Path]) -> dict[str, dict]:
    """``{category: {answer, rule_id}}`` for every taxonomy category a house rule settles."""
    from src.judges.context import settled_by_house

    return settled_by_house(project_dir, "decisions")


def load_rulings(project_dir: Path) -> list[dict[str, Any]]:
    """Rows of ``rulings.jsonl``, skipping anything unreadable."""
    path = rulings_path(project_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("subject"):
            rows.append(row)
    return rows


def ruled_text(rulings: list[dict[str, Any]]) -> str:
    if not rulings:
        return "(none yet)"
    return "\n".join(
        f"- [{r.get('category') or '?'}] {r['subject']}: {r.get('choice') or 'ruled'}"
        for r in rulings
    )


def build_preamble(project_dir: Path, *, taxonomy: dict[str, Any]) -> str:
    from src.harness import state as hstate

    cfg = hstate.load_config(project_dir)
    rendered = render(
        load_template(TEMPLATE),
        {
            "title": cfg.get("title") or Path(project_dir).name,
            "author": cfg.get("author") or "an unknown author",
            "target_language": cfg.get("target_language") or "Spanish",
            "locale": cfg.get("locale") or "unspecified",
            "book_signals": book_signals(project_dir),
            "taxonomy": format_taxonomy(taxonomy, house_defaults=house_defaults(project_dir)),
            "house_rules": house_rules_text(project_dir),
            "house_vocabulary": house_vocabulary_text(project_dir),
            "ruled": ruled_text(load_rulings(project_dir)),
        },
    )
    prefix, marker, _suffix = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    return prefix if marker else rendered.rstrip("\n") + "\n\n"


def render_body(chapter_id: str, text: str, *, window: Optional[tuple[int, int]] = None) -> str:
    lines = [f"CHAPTER: {chapter_id}"]
    if window:
        lines.append(f"PART: {window[0]} of {window[1]} (the chapter is split; report what this part raises)")
    lines += [f"WORDS: {len(text.split())}", "", text.strip(), ""]
    lines.append(
        "Return the JSON object for this chapter. An empty `candidates` list is a "
        "valid and common answer."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

_PREPARE_SCHEMA = {
    "status": "'ok' | 'error'",
    "jobs": "number of jobs (one per chapter; long chapters split into windows)",
    "chapters": "chapter ids in scope",
    "words": "English words across the jobs",
    "manifest_path": "scan.manifest.json (fanout and commit read this)",
    "taxonomy_path": "the taxonomy the prompts were rendered from",
    "held_out_examples": "taxonomy examples dropped because they came from this book",
    "rulings": "subjects already ruled, listed in the prompt so they are not re-proposed",
    "effective": "what the wave will run as, with provenance per field — quote at the gate",
    "instructions": "next step",
}


def scan_prepare(
    project_dir: Path,
    *,
    chapters: Optional[list[str]] = None,
    worker_model: Optional[str] = None,
    cli: Optional[str] = None,
    keep_drafts: bool = False,
) -> dict[str, Any]:
    """Render one prompt per chapter (or window) and a manifest. No spend."""
    from src.harness.profile import resolve_profile, usage_log_for
    from src.utils.source_text import load_chapter_source_text

    project_dir = Path(project_dir)

    def error(message: str) -> dict[str, Any]:
        return {"status": "error", "error": message, "_schema": _PREPARE_SCHEMA}

    try:
        taxonomy = load_taxonomy(exclude_project=project_dir.name)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return error(f"could not load the decision taxonomy: {exc}")

    available = chapter_ids(project_dir)
    if not available:
        return error(f"no chapters found in {project_dir} — run `setup` + `split` first")
    if chapters:
        unknown = sorted(set(chapters) - set(available))
        if unknown:
            return error(f"chapters not found: {unknown}")
        wanted = [c for c in available if c in set(chapters)]
    else:
        wanted = available

    prof = resolve_profile(
        project_dir,
        command=COMMAND,
        cli=cli,
        worker_model=worker_model,
        usage_log=usage_log_for(project_dir, COMMAND),
    )

    ddir = decisions_dir(project_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    preamble = build_preamble(project_dir, taxonomy=taxonomy)
    preamble_path = ddir / "preamble.scan.txt"
    preamble_path.write_text(preamble, encoding="utf-8")

    jobs: list[dict[str, Any]] = []
    skipped_empty: list[str] = []
    total_words = 0
    for chapter_id in wanted:
        text, _mtime, _kind = load_chapter_source_text(project_dir, chapter_id)
        if not text.strip():
            skipped_empty.append(chapter_id)
            continue
        windows = split_windows(text)
        for i, window_text in enumerate(windows, start=1):
            job_id = chapter_id if len(windows) == 1 else f"{chapter_id}.w{i}"
            window = (i, len(windows)) if len(windows) > 1 else None
            body = render_body(chapter_id, window_text, window=window)
            body_path = ddir / f"{job_id}.scan.body.txt"
            prompt_path = ddir / f"{job_id}.scan.prompt.txt"
            draft_path = ddir / f"{job_id}.scan.draft.json"
            body_path.write_text(body, encoding="utf-8")
            prompt_path.write_text(preamble + body, encoding="utf-8")
            if not keep_drafts:
                draft_path.unlink(missing_ok=True)
            words = len(window_text.split())
            total_words += words
            jobs.append(
                {
                    "id": job_id,
                    "chapter_id": chapter_id,
                    "words": words,
                    "body_path": str(body_path),
                    "prompt_path": str(prompt_path),
                    "draft_path": str(draft_path),
                }
            )

    rulings = load_rulings(project_dir)
    manifest = {
        "project": project_dir.name,
        "prepared_at": datetime.now().isoformat(),
        "prompt_version": prompt_version(TEMPLATE),
        "taxonomy_path": taxonomy["path"],
        "taxonomy_version": taxonomy.get("version"),
        "categories": category_ids(taxonomy),
        "held_out_examples": taxonomy["held_out_examples"],
        "rulings": len(rulings),
        "chapters": wanted,
        "words": total_words,
        "preamble_path": str(preamble_path),
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "effort": prof.effort,
        "jobs": jobs,
    }
    _manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out = {
        "status": "ok",
        "jobs": len(jobs),
        "chapters": wanted,
        "words": total_words,
        "manifest_path": str(_manifest_path(project_dir)),
        "taxonomy_path": taxonomy["path"],
        "held_out_examples": taxonomy["held_out_examples"],
        "rulings": len(rulings),
        "preamble_tokens_est": len(preamble) // 4,
        "effective": prof.to_payload(),
        "instructions": (
            "Quote jobs, words and `effective` at the usage gate. Then run `scan-fanout` "
            "for a headless wave, or spawn one `decision-scan-worker` per job "
            "(prompt_path -> draft_path). Then `scan-commit`."
        ),
        "_schema": _PREPARE_SCHEMA,
    }
    if skipped_empty:
        out["skipped_empty"] = skipped_empty
    return out


# ---------------------------------------------------------------------------
# fanout
# ---------------------------------------------------------------------------

def _load_manifest(project_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    return load_manifest(
        _manifest_path(project_dir), missing="no decisions scan manifest — run `scan-prepare` first"
    )


def scan_fanout(
    project_dir: Path,
    *,
    job_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless wave over the prepared chapter jobs."""
    manifest, err = _load_manifest(Path(project_dir))
    if err:
        return fanout_error(err)
    return run_wave(
        project_dir, manifest, command=COMMAND, job_ids=job_ids, concurrency=concurrency,
        cli=cli, cli_bin=cli_bin, effort=effort, runner=runner,
    )


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

_QUOTE_FOLD = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "—": "-", "–": "-", "…": "...", " ": " ",
})


def normalize_for_match(text: str) -> str:
    """Fold quotes, dashes and whitespace so a faithful copy matches the source."""
    text = unicodedata.normalize("NFC", text).translate(_QUOTE_FOLD)
    return re.sub(r"\s+", " ", text).strip().casefold()


def subject_key(subject: str) -> str:
    """A merge key for subjects: case, articles, arrows and punctuation folded."""
    key = normalize_for_match(subject)
    key = re.sub(r"\s*(->|=>|→|>)\s*", " -> ", key)
    key = re.sub(r"^(the|a|an)\s+", "", key)
    key = re.sub(r"[\"'.,;:!?()]", "", key)
    return re.sub(r"\s+", " ", key).strip()


def parse_scan_draft(raw: str, *, chapter_id: str) -> list[dict[str, Any]]:
    data = parse_judge_json(raw, REQUIRED_FIELDS)
    echoed = str(data.get("chapter_id") or "").strip()
    if echoed and echoed != chapter_id:
        raise JudgeParseError(f"chapter mismatch: draft echoed {echoed!r}, expected {chapter_id!r}")
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        raise JudgeParseError(f"'candidates' must be a list, got {type(candidates).__name__}")
    return [c for c in candidates if isinstance(c, dict)]


def validate_candidate(
    candidate: dict[str, Any], *, chapter_text_norm: str, categories: set[str]
) -> tuple[dict[str, Any], Optional[str]]:
    """``(row, None)`` for a usable candidate, ``(row, reason)`` otherwise."""
    quotes = candidate.get("quotes")
    if isinstance(quotes, str):
        quotes = [quotes]
    quotes = [str(q).strip() for q in quotes or [] if str(q).strip()]
    options = [
        {"label": str(o.get("label") or "").strip(), "effect": str(o.get("effect") or "").strip()}
        for o in candidate.get("options") or []
        if isinstance(o, dict) and str(o.get("label") or "").strip()
    ]
    row = {
        "category": str(candidate.get("category") or "").strip(),
        "subject": str(candidate.get("subject") or "").strip(),
        "question": str(candidate.get("question") or "").strip(),
        "options": options,
        "why_hard": str(candidate.get("why_hard") or "").strip(),
        "house_default": bool(candidate.get("house_default")),
    }
    row["subject_key"] = subject_key(row["subject"])
    blank = [f for f in ("category", "subject", "question") if not row[f]]
    if blank or not quotes:
        row["quotes"] = quotes
        return row, UNUSABLE_FIELDS
    if row["category"] not in categories and row["category"] != OTHER:
        row["quotes"] = quotes
        return row, UNUSABLE_CATEGORY
    found = [q for q in quotes if normalize_for_match(q) in chapter_text_norm]
    row["quotes"] = found
    row["quotes_dropped"] = len(quotes) - len(found)
    if not found:
        row["quotes"] = quotes
        return row, UNUSABLE_QUOTE
    return row, None


_COMMIT_SCHEMA = {
    "status": "'ok' | 'partial' (some jobs failed or missing) | 'error' (nothing parsed)",
    "counts": "{jobs, usable, unusable, failed, missing}",
    "volume": "{words, candidates, subjects, per_10k_words: {candidates, subjects}} — "
    "subjects are distinct (category, subject_key) pairs across chapters",
    "by_category": "{category: {candidates, subjects}}",
    "failed": "list of {id, problem}",
    "missing": "job ids with no draft",
    "candidates_path": "candidates.json",
    "report_path": "candidates.md — the candidates grouped by subject; READ THIS to relay",
    "instructions": "next step",
}


def scan_commit(project_dir: Path) -> dict[str, Any]:
    """Parse drafts, verify every quote against the chapter, write candidates + report."""
    from src.utils.source_text import load_chapter_source_text

    project_dir = Path(project_dir)
    manifest, err = _load_manifest(project_dir)
    if err:
        return {"status": "error", "error": err, "_schema": _COMMIT_SCHEMA}
    categories = set(manifest.get("categories") or [])

    usable: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    parsed = 0
    text_cache: dict[str, str] = {}

    for job in manifest["jobs"]:
        job_id, chapter_id = job.get("id"), job.get("chapter_id")
        if not job_id or not chapter_id:
            failed.append({"id": str(job_id or "?"), "problem": "malformed manifest entry"})
            continue
        try:
            draft = inside(project_dir, job.get("draft_path") or "")
        except ValueError as exc:
            failed.append({"id": job_id, "problem": str(exc)})
            continue
        if not has_draft(draft):
            missing.append(job_id)
            continue
        try:
            candidates = parse_scan_draft(draft.read_text(encoding="utf-8"), chapter_id=chapter_id)
        except JudgeParseError as exc:
            failed.append({"id": job_id, "problem": str(exc)})
            continue
        parsed += 1
        if chapter_id not in text_cache:
            text, _m, _k = load_chapter_source_text(project_dir, chapter_id)
            text_cache[chapter_id] = normalize_for_match(text)
        for candidate in candidates:
            row, reason = validate_candidate(
                candidate, chapter_text_norm=text_cache[chapter_id], categories=categories
            )
            row = {"job_id": job_id, "chapter_id": chapter_id, **row}
            if reason:
                unusable.append({**row, "reason": reason})
            else:
                usable.append(row)

    counts = {
        "jobs": len(manifest["jobs"]),
        "usable": len(usable),
        "unusable": len(unusable),
        "failed": len(failed),
        "missing": len(missing),
    }
    if not parsed and (failed or missing):
        return {
            "status": "error",
            "error": "no scan draft parsed — candidates.json left as it was",
            "counts": counts,
            "failed": failed,
            "missing": missing,
            "_schema": _COMMIT_SCHEMA,
        }

    words = int(manifest.get("words") or 0)
    subjects: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in usable:
        subjects.setdefault((row["category"], row["subject_key"]), []).append(row)
    by_category: dict[str, dict[str, int]] = {}
    for (category, _key), rows in subjects.items():
        slot = by_category.setdefault(category, {"candidates": 0, "subjects": 0})
        slot["candidates"] += len(rows)
        slot["subjects"] += 1

    def per_10k(n: int) -> Optional[float]:
        return round(n * 10000 / words, 1) if words else None

    volume = {
        "words": words,
        "candidates": len(usable),
        "subjects": len(subjects),
        "subjects_excluding_house_default": sum(
            1 for rows in subjects.values() if not all(r["house_default"] for r in rows)
        ),
        "per_10k_words": {"candidates": per_10k(len(usable)), "subjects": per_10k(len(subjects))},
    }
    doc = {
        "project": project_dir.name,
        "committed_at": datetime.now().isoformat(),
        "prompt_version": manifest.get("prompt_version"),
        "taxonomy_path": manifest.get("taxonomy_path"),
        "held_out_examples": manifest.get("held_out_examples"),
        "worker_model": manifest.get("worker_model"),
        "effort": manifest.get("effort"),
        "volume": volume,
        "by_category": by_category,
        "candidates": usable,
        "unusable": unusable,
        "failed": failed,
        "missing": missing,
    }
    _candidates_path(project_dir).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = _report_path(project_dir)
    report_path.write_text(render_report(doc, subjects), encoding="utf-8")

    return {
        "status": "partial" if failed or missing else "ok",
        "counts": counts,
        "volume": volume,
        "by_category": by_category,
        "failed": failed,
        "missing": missing,
        "unusable_by_reason": _count(r["reason"] for r in unusable),
        "candidates_path": str(_candidates_path(project_dir)),
        "report_path": str(report_path),
        "instructions": (
            "Read report_path. Re-run `scan-fanout --job-ids` for anything failed or "
            "missing, then commit again."
        ),
        "_schema": _COMMIT_SCHEMA,
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def render_report(doc: dict[str, Any], subjects: dict[tuple[str, str], list[dict[str, Any]]]) -> str:
    """Candidates grouped by category, then subject, most-raised subject first."""
    vol = doc["volume"]
    lines = [
        f"# Decisions scan — {doc['project']}",
        "",
        f"{vol['candidates']} candidates in {vol['subjects']} subjects over "
        f"{vol['words']} words ({vol['per_10k_words']['subjects']} subjects per 10k words). "
        f"Model {doc.get('worker_model')}, effort {doc.get('effort')}; "
        f"{doc.get('held_out_examples', 0)} taxonomy examples from this book held out.",
        "",
    ]
    by_cat: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    for (category, key), rows in subjects.items():
        by_cat.setdefault(category, []).append((key, rows))
    for category in sorted(by_cat, key=lambda c: -sum(len(r) for _k, r in by_cat[c])):
        entries = sorted(by_cat[category], key=lambda kr: -len(kr[1]))
        lines += [f"## {category} ({len(entries)} subjects)", ""]
        for _key, rows in entries:
            first = rows[0]
            chapters = sorted({r["chapter_id"] for r in rows})
            flag = " · house default" if all(r["house_default"] for r in rows) else ""
            lines.append(f"### {first['subject']} — {len(chapters)} chapter(s){flag}")
            lines.append(f"{first['question']}")
            if first.get("why_hard"):
                lines.append(f"*Why hard:* {first['why_hard']}")
            for opt in first.get("options") or []:
                lines.append(f"- **{opt['label']}** — {opt['effect']}")
            lines.append(f"> {first['quotes'][0]}  ({first['chapter_id']})")
            lines.append(f"Chapters: {', '.join(chapters)}")
            lines.append("")
    if doc.get("unusable"):
        lines += ["## Unusable", ""]
        for row in doc["unusable"]:
            lines.append(f"- {row['chapter_id']} [{row['reason']}] {row.get('subject')}: {row.get('question')}")
    return "\n".join(lines) + "\n"
