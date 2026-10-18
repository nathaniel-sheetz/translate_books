"""
The house consistency report: where a book stands against the house and its siblings.

Deterministic and read-only. It sets the book's own artifacts against:

- the house rules that ``settles`` its style questions, and the siblings' style
  answers;
- its series siblings' address pairs;
- the house lexicon and the siblings' glossaries and committed translations;
- its own glossary, in its own committed translations (a term whose rendering
  never shows up where the English word does);

and lists **promotion candidates**: a rendering two or more books of a group
share that the house lexicon does not have yet. It also reports **drift**:
sibling renderings that changed since this book's precedent snapshot.

Run it at each gate and after translation (``scripts/house_check.py <slug>``).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from src.house import book_slug, project_dir_for, read_body
from src.house.match import count_word

#: A glossary term counts as "not followed" when its renderings appear in fewer
#: than this share of the chunks whose English has the word.
MIN_GLOSSARY_SHARE = 0.8


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _glossary_terms(project_dir: Path) -> list[dict[str, Any]]:
    doc = _read_json(Path(project_dir) / "glossary.json")
    if not isinstance(doc, dict):
        return []
    return [t for t in doc.get("terms") or [] if isinstance(t, dict) and t.get("english")]


def check_style(project_dir: Path, precedent: dict) -> list[dict[str, Any]]:
    """The book's style answers against the house rules and the siblings."""
    from src.harness.state import harness_dir
    from src.house import in_scope
    from src.house.groups import group_ids_of
    from src.house.precedent import _question_defs, normalize_answer, style_votes
    from src.judges.context import load_book_overrides, load_house_rules, rule_settles

    answers = _read_json(harness_dir(project_dir) / "style_answers.json")
    if not isinstance(answers, dict):
        return []
    defs = _question_defs(project_dir)
    off = load_book_overrides(project_dir)
    groups = group_ids_of(book_slug(project_dir))
    house: dict[str, tuple[str, Any]] = {}
    # Every rule in scope, including the ones this book turned off, so the
    # report can say a departure was recorded rather than leave it out.
    for rule in (r for r in load_house_rules() if in_scope(r.get("scope"), groups)):
        for key, value in rule_settles(rule).items():
            if key.startswith("style:"):
                house.setdefault(key[len("style:"):], (str(rule.get("id")), value))
    rows = []
    for qid, raw in answers.items():
        mine, label = normalize_answer(defs.get(qid), raw)
        if qid in house:
            rule_id, value = house[qid]
            theirs, _ = normalize_answer(defs.get(qid), value)
            if theirs != mine:
                rows.append({
                    "question": qid, "answer": label, "against": f"house rule `{rule_id}`",
                    "expected": theirs,
                    "status": "turned off for this book" if rule_id in off else "not recorded as off",
                })
        votes = style_votes(precedent, qid)
        if votes and votes[0]["answer"] != mine:
            total = sum(len(v["slugs"]) for v in votes)
            rows.append({
                "question": qid, "answer": label,
                "against": f"siblings ({len(votes[0]['slugs'])} of {total})",
                "expected": votes[0]["label"], "status": "differs",
            })
    return rows


def check_address(project_dir: Path, precedent: dict) -> list[dict[str, Any]]:
    from src.house.precedent import address_divergence

    amap = _read_json(Path(project_dir) / "address_map.json")
    if not isinstance(amap, dict):
        return []
    return address_divergence(
        [p for p in amap.get("pairs") or [] if isinstance(p, dict)],
        precedent.get("address", {}).get("pairs") or [],
    )


def check_glossary(project_dir: Path, precedent: dict) -> list[dict[str, Any]]:
    """Each lexicon entry and sibling rendering that bears on the book, with its status."""
    from src.house.groups import group_ids_of
    from src.house.lexicon import entries_for
    from src.house.precedent import bare, format_concordance, leading_rendering

    mine = {str(t["english"]).casefold(): t for t in _glossary_terms(project_dir)}
    by_english = precedent.get("glossary") or {}
    rows = []
    lexicon = entries_for(group_ids_of(book_slug(project_dir)), read_body(project_dir))
    covered = set()
    for e in lexicon:
        if not e["spanish"]:
            continue
        key = e["english"].casefold()
        covered.add(key)
        term = mine.get(key)
        rows.append({
            "english": e["english"], "expected": e["spanish"],
            "source": f"house lexicon ({e['scope']})",
            "book": term.get("spanish") if term else None,
            "status": _status(term, e["spanish"]),
            "evidence": format_concordance(by_english.get(key, {})),
        })
    for key, entry in by_english.items():
        if key in covered:
            continue
        lead = leading_rendering(entry)
        term = mine.get(key)
        if lead is None or term is None:
            continue
        status = _status(term, lead["spanish"])
        if status == "agrees" and not entry.get("divergent"):
            continue
        rows.append({
            "english": entry["english"], "expected": lead["spanish"],
            "source": "siblings: " + ", ".join(lead["slugs"]),
            "book": term.get("spanish"), "status": status,
            "evidence": format_concordance(entry),
        })
    for row in rows:
        row["expected"] = bare(row["expected"])
    return rows


#: A sibling must use the English word at least this often before its committed
#: text counts as evidence against the book's rendering.
MIN_SIBLING_USES = 3


def sibling_usage(
    project_dir: Path, precedent: dict, *, chunk_memo: Optional[dict[str, list]] = None
) -> list[dict[str, Any]]:
    """The book's own renderings that its siblings' committed text does not use.

    Catches a split no sibling glossary records: game-legs renders a racing heat
    as manga while gaudenzia's translation says serie, and only gaudenzia's
    chunks show it. Names and places are skipped; they belong to each book.
    """
    from src.house.precedent import bare, concordance, sibling_chunks

    rows = []
    dirs = {s["slug"]: project_dir_for(s["slug"]) for s in precedent.get("siblings") or []}
    dirs = {slug: d for slug, d in dirs.items() if d is not None}
    if not dirs:
        return rows
    chunks = {slug: sibling_chunks(slug, d, chunk_memo) for slug, d in dirs.items()}
    for term in _glossary_terms(project_dir):
        if str(term.get("type") or "") in ("character", "place"):
            continue
        forms = sorted({bare(f) for f in [term.get("spanish") or "", *(term.get("alternatives") or [])] if bare(f)})
        if not forms:
            continue
        against = []
        for slug, rows_ in chunks.items():
            c = concordance(term["english"], forms, rows_)
            if c and c["english"] >= MIN_SIBLING_USES and not c["forms"]:
                against.append(f"{slug} (×{c['english']})")
        if against:
            rows.append({
                "english": term["english"], "rendering": term.get("spanish"),
                "siblings_without_it": against,
            })
    return rows


def _status(term: Optional[dict], expected: str) -> str:
    from src.house.precedent import bare

    if term is None:
        return "missing from the glossary"
    forms = {bare(f).casefold() for f in [term.get("spanish") or "", *(term.get("alternatives") or [])]}
    return "agrees" if bare(expected).casefold() in forms else "differs"


def check_committed(project_dir: Path) -> list[dict[str, Any]]:
    """Glossary terms whose renderings are missing from chunks that have the English word."""
    from src.house.precedent import bare

    chunks = []
    for path in sorted((Path(project_dir) / "chunks").glob("*.json")):
        doc = _read_json(path)
        if isinstance(doc, dict) and doc.get("translated_text"):
            chunks.append((path.stem, str(doc.get("source_text") or ""), str(doc["translated_text"])))
    if not chunks:
        return []
    rows = []
    for term in _glossary_terms(project_dir):
        if str(term.get("type") or "") == "character":
            continue  # names are checked by the dictionary and editorial passes
        forms = [bare(f) for f in [term.get("spanish") or "", *(term.get("alternatives") or [])] if bare(f)]
        hits = [(cid, tr) for cid, src, tr in chunks if count_word(term["english"], src)]
        if not hits or not forms:
            continue
        missing = [cid for cid, tr in hits if not any(count_word(f, tr) for f in forms)]
        share = 1 - len(missing) / len(hits)
        if share < MIN_GLOSSARY_SHARE:
            rows.append({
                "english": term["english"], "rendering": term.get("spanish"),
                "chunks_with_word": len(hits), "chunks_without_rendering": missing,
                "share": round(share, 2),
            })
    return rows


def promotion_candidates(project_dir: Path) -> list[dict[str, Any]]:
    """Renderings shared by two or more books of one group, not yet in the lexicon."""
    from src.house.groups import groups_of
    from src.house.lexicon import load_lexicon
    from src.house.precedent import bare

    in_lexicon = {e["english"].casefold() for e in load_lexicon() if e["spanish"]}
    rows = []
    for gid, group in groups_of(book_slug(project_dir)).items():
        uses: dict[tuple[str, str], list[str]] = defaultdict(list)
        english_of: dict[str, str] = {}
        display: dict[tuple[str, str], str] = {}
        for member in group["members"]:
            member_dir = project_dir_for(member)
            for term in (_glossary_terms(member_dir) if member_dir else []):
                # A domain shares vocabulary: its names and places are each book's own.
                if group["kind"] != "series" and str(term.get("type") or "") in ("character", "place"):
                    continue
                key = str(term["english"]).casefold()
                english_of.setdefault(key, term["english"])
                spanish = bare(str(term.get("spanish") or ""))
                display.setdefault((key, spanish.casefold()), spanish)
                uses[(key, spanish.casefold())].append(member)
        for (key, spanish), members in sorted(uses.items()):
            if len(members) < 2 or key in in_lexicon or not spanish:
                continue
            others = sorted({display[(k, s)] for (k, s) in uses if k == key and s != spanish})
            rows.append({
                "english": english_of[key], "spanish": display[(key, spanish)], "group": gid,
                "books": members, "conflicting": others,
                "scope": f"group:{gid}",
            })
    return rows


def drift(snapshot: Optional[dict], fresh: dict) -> list[dict[str, Any]]:
    """Sibling renderings that changed since this book's precedent snapshot.

    ``fresh`` is the precedent as the siblings stand now; a book with no
    snapshot has nothing to have drifted from.
    """
    from src.house.precedent import leading_rendering

    precedent = snapshot or {}
    if not precedent.get("built_at"):
        return []
    rows = []
    for key, entry in (precedent.get("glossary") or {}).items():
        then = leading_rendering(entry)
        now_entry = (fresh.get("glossary") or {}).get(key)
        now = leading_rendering(now_entry) if now_entry else None
        if then and now and then["spanish"].casefold() != now["spanish"].casefold():
            rows.append({"english": entry["english"], "snapshot": then["spanish"], "now": now["spanish"]})
    return rows


def run_check(project_dir: Path) -> dict[str, Any]:
    from src.house.precedent import build_precedent, cached_precedent

    project_dir = Path(project_dir)
    # One build and one read of the siblings' chunks serve every section. The
    # snapshot is never written here: a check run before drafting would freeze
    # the baseline that drift is measured against.
    chunk_memo: dict[str, list] = {}
    fresh = build_precedent(project_dir, write=False, chunk_memo=chunk_memo)
    snapshot = cached_precedent(project_dir)
    precedent = snapshot or fresh
    glossary = check_glossary(project_dir, precedent)
    report = {
        "project": project_dir.name,
        "precedent_built_at": snapshot.get("built_at") if snapshot else None,
        "siblings": [s["slug"] for s in precedent.get("siblings") or []],
        "style": check_style(project_dir, precedent),
        "address": check_address(project_dir, precedent),
        "glossary": glossary,
        "sibling_usage": sibling_usage(project_dir, precedent, chunk_memo=chunk_memo),
        "committed": check_committed(project_dir),
        "promotion_candidates": promotion_candidates(project_dir),
        "drift": drift(snapshot, fresh),
    }
    report["counts"] = {
        "style": len(report["style"]),
        "address": len(report["address"]),
        "glossary_departures": sum(1 for r in glossary if r["status"] != "agrees"),
        "sibling_usage": len(report["sibling_usage"]),
        "committed": len(report["committed"]),
        "promotion_candidates": len(report["promotion_candidates"]),
        "drift": len(report["drift"]),
    }
    return report


def write_report(project_dir: Path) -> dict[str, Any]:
    """Run the check, write ``reports/house_check.md``, return ``{counts, report_path}``.

    What the harness gates attach to their commit output. Never raises: a
    consistency report that fails must not cost the user a commit.
    """
    try:
        report = run_check(project_dir)
        out = Path(project_dir) / "reports" / "house_check.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(report), encoding="utf-8")
        return {"counts": report["counts"], "report_path": str(out)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"house check failed: {exc}"}


def render_markdown(report: dict[str, Any]) -> str:
    c = report["counts"]
    lines = [
        f"# House check: {report['project']}",
        "",
        f"Siblings: {', '.join(report['siblings']) or '(none)'}; precedent snapshot "
        f"{report['precedent_built_at'] or '(none)'}.",
        "",
        f"{c['style']} style, {c['address']} address, {c['glossary_departures']} glossary, "
        f"{c['sibling_usage']} sibling-usage, {c['committed']} committed-text, {c['drift']} drift; "
        f"{c['promotion_candidates']} promotion candidates.",
        "",
        "## Style answers",
        "",
    ]
    lines += [
        f"- {r['question']}: {r['answer']} — {r['against']} says {r['expected']} ({r['status']})"
        for r in report["style"]
    ] or ["- nothing departs"]
    lines += ["", "## Address map against series siblings", ""]
    lines += [
        f"- {r['direction']}: {r['form']} here, {r['other_form']} in {r['other']}"
        for r in report["address"]
    ] or ["- nothing departs (or no series)"]
    lines += ["", "## Glossary against the house lexicon and siblings", ""]
    for r in report["glossary"]:
        line = f"- {r['english']} → {r['expected']} [{r['source']}]: {r['status']}"
        if r["status"] == "differs":
            line += f" (book: {r['book']})"
        if r["evidence"]:
            line += f" — {r['evidence']}"
        lines.append(line)
    if not report["glossary"]:
        lines.append("- nothing bears on this book")
    lines += ["", "## This book's renderings that sibling translations do not use", ""]
    lines += [
        f"- {r['english']} → {r['rendering']}: absent where the word occurs in "
        f"{', '.join(r['siblings_without_it'])}"
        for r in report["sibling_usage"]
    ] or ["- none"]
    lines += ["", "## Committed text against the glossary", ""]
    lines += [
        f"- {r['english']} → {r['rendering']}: in {round(r['share'] * 100)}% of the "
        f"{r['chunks_with_word']} chunks with the word; missing in "
        f"{', '.join(r['chunks_without_rendering'][:8])}"
        + (" …" if len(r["chunks_without_rendering"]) > 8 else "")
        for r in report["committed"]
    ] or ["- every glossary term is followed (or nothing is translated yet)"]
    lines += ["", "## Promotion candidates (shared by 2+ books of a group, not in the lexicon)", ""]
    lines += [
        f"- {r['english']} → {r['spanish']} [{r['scope']}]: {', '.join(r['books'])}"
        + (f"; also rendered {', '.join(r['conflicting'])}" if r["conflicting"] else "")
        for r in report["promotion_candidates"]
    ] or ["- none"]
    lines += ["", "## Drift since the precedent snapshot", ""]
    lines += [
        f"- {r['english']}: {r['snapshot']} then, {r['now']} now" for r in report["drift"]
    ] or ["- none"]
    return "\n".join(lines) + "\n"
