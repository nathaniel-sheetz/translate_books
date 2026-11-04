#!/usr/bin/env python3
"""
Score the quotation scan against a hand-made gold table.

The gold set is the 222-row CSV built by hand for short-sermons
(``Quotation, Line, Citation in text, Actual KJV reference, NBLA text``; UTF-8 or
CP1252). ``Line`` is the 1-based line of the project's ``source.txt``, which is
how each row is placed in a chapter. Only the English side is scored, so the
already-corrected Spanish does not matter.

Gold kinds are read off the table: "Not biblical" is ``not_scripture``;
"Paraphrase" / "cf." / "(paraphrase)" is ``paraphrase``; a row whose source line
is the quotation followed by a dash and its reference is an ``epigraph``;
everything else is ``quotation``. ``written`` means the author wrote the
reference (``Citation in text``).

A gold row is **found** when a candidate in the same chapter covers its words
(at least 60% of the gold words, with the candidate not mostly about something
else), or contains it. A written reference is also found when a candidate in
the chapter names an overlapping verse, which is the deterministic floor.
A candidate is **true** when it matches some gold row.

    python -X utf8 scripts/quotation_gold.py --project short-sermons-to-little-children \\
        --gold "C:/path/to/short_sermons_quotations_with_nbla.csv"
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.decisions.scan import normalize_for_match  # noqa: E402
from src.quotations import quotations_dir  # noqa: E402
from src.quotations import scan as qscan  # noqa: E402
from src.quotations.refs import parse_ref, verse_key  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

GOLD_COVER = 0.6
CAND_FOCUS = 0.3


def read_gold(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(io.StringIO(text)))


def gold_kind(row: dict, line: str) -> str:
    ref = (row.get("Actual KJV reference") or "").strip().lower()
    if "not biblical" in ref:
        return "not_scripture"
    if ref.startswith("paraphrase") or "(paraphrase" in ref or ref.startswith("cf."):
        return "paraphrase"
    quote = " ".join((row.get("Quotation") or "").split())
    flat = " ".join(line.split())
    if quote and flat.startswith(quote) and re.match(r"^\s*[—–-]", flat[len(quote):]):
        return "epigraph"
    return "quotation"


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+(?:'[a-z]+)?", normalize_for_match(text)))


def covers(gold_text: str, cand_text: str) -> float:
    """How well ``cand_text`` covers ``gold_text``: 0 when it does not."""
    g, c = normalize_for_match(gold_text).strip(" .,;:!?\"'"), normalize_for_match(cand_text)
    if g and (g in c or c.strip(" .,;:!?\"'") in g and len(c) >= 0.6 * len(g)):
        return 1.0
    gw, cw = words(gold_text), words(cand_text)
    if not gw or not cw:
        return 0.0
    inter = len(gw & cw)
    if inter / len(gw) >= GOLD_COVER and inter / len(cw) >= CAND_FOCUS:
        return inter / len(gw)
    return 0.0


def _gold_verse_keys(ref) -> list[str]:
    """Verse keys for a gold reference; a whole chapter ("Psalm 23") is every verse in it."""
    if ref is None:
        return []
    keys = ref.verse_keys()
    for chapter in ref.whole_chapters:
        keys += [verse_key(ref.book, chapter, v) for v in range(1, 177)]
    return keys


def place_gold(project_dir: Path, gold: list[dict]) -> list[dict]:
    """Attach ``chapter_id``, ``kind`` and ``written`` to every gold row."""
    lines = (project_dir / "source.txt").read_text(encoding="utf-8").split("\n")
    chapters = {
        cid: normalize_for_match(qscan.chapter_english(project_dir, cid))
        for cid in qscan.chapter_ids(project_dir)
    }
    out = []
    for row in gold:
        try:
            line = lines[int(row["Line"]) - 1]
        except (ValueError, IndexError, KeyError):
            line = ""
        probe = normalize_for_match(line)[:120]
        chapter = next((cid for cid, text in chapters.items() if probe and probe in text), None)
        if chapter is None:
            quote = normalize_for_match(row.get("Quotation") or "")[:80]
            chapter = next((cid for cid, text in chapters.items() if quote and quote in text), None)
        ref = parse_ref(row.get("Actual KJV reference") or "")
        out.append({
            "quotation": row.get("Quotation") or "",
            "line": row.get("Line"),
            "chapter_id": chapter,
            "kind": gold_kind(row, line),
            "written": bool((row.get("Citation in text") or "").strip()),
            "citation": (row.get("Citation in text") or "").strip(),
            "ref": row.get("Actual KJV reference") or "",
            "verse_keys": _gold_verse_keys(ref),
        })
    return out


def score(gold: list[dict], candidates: list[dict]) -> dict:
    by_chapter: dict[str, list[int]] = defaultdict(list)
    for i, cand in enumerate(candidates):
        by_chapter[cand["chapter_id"]].append(i)

    cand_matched: set[int] = set()
    rows = []
    for g in gold:
        best, best_score = None, 0.0
        for i in by_chapter.get(g["chapter_id"], []):
            cand = candidates[i]
            s = covers(g["quotation"], cand.get("en_span") or "")
            if s > best_score:
                best, best_score = i, s
        union: list[int] = []
        if best is None:
            # A long quotation the wave split (Psalm 23 verse by verse): found
            # when candidates lying inside it cover enough of it together.
            gw = words(g["quotation"])
            covered: set[str] = set()
            for i in by_chapter.get(g["chapter_id"], []):
                cw = words(candidates[i].get("en_span") or "")
                if gw and cw and len(cw & gw) / len(cw) >= GOLD_COVER:
                    union.append(i)
                    covered |= cw & gw
            if gw and len(covered) / len(gw) >= GOLD_COVER:
                best = union[0]
                cand_matched.update(union)
            else:
                union = []
        by_ref = None
        if best is None and g["written"] and g["verse_keys"]:
            for i in by_chapter.get(g["chapter_id"], []):
                if set(g["verse_keys"]) & set(candidates[i].get("verse_keys") or []):
                    by_ref = i
                    break
        found = best if best is not None else by_ref
        if best is not None:
            cand_matched.add(best)
        ref_ok = None
        if found is not None and g["verse_keys"]:
            picked = union or [found]
            ref_ok = any(set(g["verse_keys"]) & set(candidates[i].get("verse_keys") or []) for i in picked)
        rows.append({
            **g,
            "found": found is not None,
            "found_by": ("union" if union else "text") if best is not None else ("ref" if by_ref is not None else None),
            "candidate": found,
            "pred_kind": candidates[found]["kind"] if found is not None else None,
            "ref_ok": ref_ok,
        })
    # A candidate that covers a gold row only partly still counts as true.
    for i, cand in enumerate(candidates):
        if i in cand_matched:
            continue
        for g in gold:
            if g["chapter_id"] == cand["chapter_id"] and covers(cand.get("en_span") or "", g["quotation"]):
                cand_matched.add(i)
                break

    def recall(filter_fn) -> dict:
        sel = [r for r in rows if filter_fn(r)]
        hit = sum(1 for r in sel if r["found"])
        return {"gold": len(sel), "found": hit, "recall": round(hit / len(sel), 3) if sel else None}

    recall_by_kind = {k: recall(lambda r, k=k: r["kind"] == k) for k in sorted({r["kind"] for r in rows})}
    scripture = lambda r: r["kind"] != "not_scripture"  # noqa: E731
    out = {
        "gold_rows": len(rows),
        "unplaced_gold": sum(1 for r in rows if r["chapter_id"] is None),
        "recall_by_kind": recall_by_kind,
        "recall_written": recall(lambda r: scripture(r) and r["written"]),
        "recall_written_by_text": {
            **recall(lambda r: scripture(r) and r["written"]),
            "found": sum(1 for r in rows if scripture(r) and r["written"] and r["found_by"] in ("text", "union")),
        },
        "recall_unwritten": recall(lambda r: scripture(r) and not r["written"]),
        "recall_unwritten_quotation_epigraph": recall(
            lambda r: r["kind"] in ("quotation", "epigraph") and not r["written"]
        ),
        "recall_quotation_epigraph": recall(lambda r: r["kind"] in ("quotation", "epigraph")),
        "ref_accuracy": {
            "checked": sum(1 for r in rows if r["ref_ok"] is not None),
            "correct": sum(1 for r in rows if r["ref_ok"]),
        },
    }
    precision_by_kind = {}
    for kind in sorted({c["kind"] for c in candidates}):
        idx = [i for i, c in enumerate(candidates) if c["kind"] == kind]
        true = sum(1 for i in idx if i in cand_matched)
        precision_by_kind[kind] = {
            "candidates": len(idx),
            "match_gold": true,
            "precision": round(true / len(idx), 3) if idx else None,
        }
    out["precision_by_kind"] = precision_by_kind
    origin = Counter(c.get("origin", "wave") for c in candidates)
    out["candidates"] = {"total": len(candidates), "by_origin": dict(origin)}
    out["kind_confusion"] = dict(Counter(
        f"{r['kind']}->{r['pred_kind']}" for r in rows if r["found"]
    ))
    return {"summary": out, "rows": rows, "unmatched_candidates": sorted(set(range(len(candidates))) - cand_matched)}


def write_report(path: Path, result: dict, candidates: list[dict]) -> None:
    lines = ["# Quotation scan vs gold", "", "```json",
             json.dumps(result["summary"], ensure_ascii=False, indent=2), "```", "",
             "## Gold rows not found", ""]
    for r in result["rows"]:
        if not r["found"]:
            tag = "written" if r["written"] else "unwritten"
            lines.append(f"- `{r['chapter_id']}` line {r['line']} [{r['kind']}, {tag}] "
                         f"{r['ref']}: {r['quotation'][:140]}")
    lines += ["", "## Wrong reference on a found row", ""]
    for r in result["rows"]:
        if r["ref_ok"] is False:
            cand = candidates[r["candidate"]]
            lines.append(f"- `{r['chapter_id']}` gold {r['ref']} / scan {cand.get('ref')}: {r['quotation'][:100]}")
    lines += ["", "## Candidates matching no gold row", ""]
    for i in result["unmatched_candidates"]:
        c = candidates[i]
        lines.append(f"- `{c['chapter_id']}` [{c['kind']}, {c.get('origin')}] {c.get('ref')}: {c['en_span'][:140]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--gold", required=True, help="the hand-made quotation CSV")
    args = parser.parse_args(argv)

    project_dir = Path(args.project)
    if not project_dir.is_dir():
        project_dir = _REPO_ROOT / "projects" / args.project
    cands_path = qscan.candidates_path(project_dir)
    if not cands_path.exists():
        print(json.dumps({"status": "error", "error": f"no {cands_path}; run scan-commit first"}))
        return 1
    candidates = json.loads(cands_path.read_text(encoding="utf-8"))["candidates"]
    gold = place_gold(project_dir, read_gold(Path(args.gold)))
    result = score(gold, candidates)
    report = quotations_dir(project_dir) / "gold_report.md"
    write_report(report, result, candidates)
    print(json.dumps({"status": "ok", **result["summary"], "report_path": str(report)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
