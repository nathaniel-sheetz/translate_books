#!/usr/bin/env python3
"""
Replay the quotation judge against the user's marks on the board.

Everything goes to gitignored ``comparisons/quotation-judge/``: the items carry NBLA
verse text and the labels carry the user's comments.

    build     freeze the gold items and labels from the two boards   (no spend)
    prepare   render one split's jobs under runs/<split>-<run>/      (no spend)
    fanout    run them headless                                      (no API spend)
    score     confusion matrix and a confidence sweep                (no spend)

**The splits.** ``dev`` is short-sermons plus plain-thoughts chapters 01, 02, 03 and
08: the prompt may be tuned on it. ``held`` is plain-thoughts chapters 04 to 07, clicked
marks only. A held-out item shows the judge exactly what a dev item does, but its text,
labels and comments are not printed here, and ``score`` writes a stamp the first time
it runs on ``held``: any later tuning makes that set dev data.

**What a card's label is.** The toss-up tick wins; then a revision or a comment makes
it ``partial``, unless the comment only restores the translator's words, which is a
``reject``; a card fixed by hand is ``partial``; then the state. A card still on its
default ``Apply`` in a dev chapter counts as accepted, because the user read those
chapters and clicked the cards around it (held-out chapters count clicks only). A
pending card with no mark has no label and is left out.
``comparisons/quotation-judge/overrides.json`` can set ``{key: {label, fix, restore}}``
for what the board cannot express.

**What the judge sees.** A revised card shows the wave's original proposal, not the
revision, rebuilt from its map draft; an applied card shows the text from before it was
applied. The marks are about those.

    python -X utf8 scripts/quotation_judge_gold.py build
    python -X utf8 scripts/quotation_judge_gold.py prepare --split dev --run sonnet --worker-model claude-sonnet-5-5 --cli claude
    python -X utf8 scripts/quotation_judge_gold.py fanout --split dev --run sonnet
    python -X utf8 scripts/quotation_judge_gold.py score --split dev --run sonnet
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.quotations import board as qboard  # noqa: E402
from src.quotations import judge as qjudge  # noqa: E402
from src.quotations import map as qmap  # noqa: E402
from src.quotations import scan as qscan  # noqa: E402
from src.quotations.apply import applied_cards, latest_decisions  # noqa: E402
from src.quotations.index import load_index  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = _REPO_ROOT / "comparisons" / "quotation-judge"

PLAIN = "plain-thoughts"
SHORT = "short-sermons-to-little-children"
BOOKS = (SHORT, PLAIN)
DEV_PLAIN = {"chapter_01", "chapter_02", "chapter_03", "chapter_08"}
HELD_PLAIN = {"chapter_04", "chapter_05", "chapter_06", "chapter_07"}
SPLITS = ("dev", "held")
#: Whose config and usage log a wave runs under; the jobs hold both books.
WAVE_PROJECT = PLAIN

FLOORS = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
#: A high-confidence apply on one of these is a wrong apply.
NOT_APPLY = {"reject", "partial"}


def split_of(book: str, chapter_id: str) -> Optional[str]:
    if book == SHORT or chapter_id in DEV_PLAIN:
        return "dev"
    return "held" if chapter_id in HELD_PLAIN else None


def _bare(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.casefold(), flags=re.UNICODE).strip()


def label_of(card: dict[str, Any], choice: Optional[dict[str, Any]], *, revision: Optional[dict[str, Any]],
             original: list[dict[str, Any]], clicks_only: bool) -> tuple[Optional[str], str]:
    """``(label, fix)`` for one card; a ``None`` label means the card says nothing."""
    ask = (choice or {}).get("ask") or ""
    fix = ask or ((revision or {}).get("ask") or "")
    if choice is None:
        if clicks_only or qboard.default_choice(card)["state"] != qboard.STATE_APPROVE:
            return None, ""
        return "apply", ""
    if choice.get("toss_up"):
        return "toss_up", fix
    if fix:
        if any(_bare(fix) == _bare(e["old"]) for e in original if e["old"] != e["new"]):
            return "reject", fix
        return "partial", fix
    if choice.get("hand"):
        return "partial", "fixed by hand in the book"
    return {qboard.STATE_APPROVE: "apply", qboard.STATE_REJECT: "reject"}.get(choice.get("state")), ""


def map_time_rows(project_dir: Path, chapter_id: str, cards: list[dict[str, Any]],
                  applied: set[str], overrides: dict[str, dict[str, Any]]) -> dict[int, str]:
    """The chapter's rows as the mapper saw them: every applied edit swapped back.

    Text the user then rewrote by hand can't be worked out from the card, so its
    override names it: ``restore: [text now, text at map time]``.
    """
    rows = {r["es_idx"]: r.get("es") or "" for r in qscan.load_rows(project_dir, chapter_id) if "es_idx" in r}
    for over in overrides.values():
        if over.get("restore"):
            now, then = over["restore"]
            rows = {i: es.replace(now, then) for i, es in rows.items()}
    for card in cards:
        if card["chapter_id"] != chapter_id or card["key"] not in applied:
            continue
        for edit in card.get("quote_edits") or []:
            if edit["old"] == edit["new"]:
                continue
            # Realigning after the apply can leave the words a row or two away.
            at = edit["es_idx"]
            for i in (at, at + 1, at - 1, at + 2, at - 2):
                row = rows.get(i) or ""
                if edit["new"] in row and (edit["old"] not in row or edit["old"] in edit["new"]):
                    rows[i] = row.replace(edit["new"], edit["old"], 1)
                    break
    return rows


class _FrozenText(qmap.ChapterText):
    """Map-time rows, for rebuilding a proposal whose words have since left the book."""

    def __init__(self, project_dir: Path, chapter_id: str, rows: dict[int, str]):
        super().__init__(project_dir, chapter_id)
        for idx, es in rows.items():
            if idx in self.rows:
                self.rows[idx] = {**self.rows[idx], "es": es}

    def paragraph(self, es_idx: int, old: str) -> str:
        return " ".join(filter(None, (self.es(es_idx - 1), self.es(es_idx))))


def draft_mappings(project_dir: Path) -> dict[str, dict[str, Any]]:
    """``{card_key: the wave's mapping}``, from the drafts the manifest names."""
    manifest = json.loads((qmap._manifest_path(project_dir)).read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for job in manifest["jobs"]:
        chapter_id = job.get("chapter_id") or job["id"]
        draft = project_dir / ".harness" / "quotations" / f"{chapter_id}.map.draft.json"
        by_id = qmap.parse_map_draft(draft.read_text(encoding="utf-8"), chapter_id=chapter_id)
        for cid, key in (job.get("candidates") or {}).items():
            if cid in by_id:
                out[key] = by_id[cid]
    return out


def original_edits(project_dir: Path, card: dict[str, Any], rows: dict[int, str], cand: dict[str, Any],
                   mappings: dict[str, dict[str, Any]], index: Any) -> list[dict[str, Any]]:
    """The wave's proposal for a revised card; the card's own edits for any other."""
    if not card.get("revised"):
        return card.get("quote_edits") or []
    mapping = mappings.get(card["key"])
    if mapping is None:
        return []
    text = _FrozenText(project_dir, card["chapter_id"], rows)
    built = qmap.build_card(cand, mapping, text, qmap._Claims(), index.proper,
                            fix_caps=not index.doc.get("smallcaps_resolved"))
    return built["quote_edits"]


def build_book(book: str, overrides: dict[str, dict[str, Any]]) -> tuple[list[dict], list[dict], Counter]:
    project_dir = _REPO_ROOT / "projects" / book
    doc = qboard.load_mapped(project_dir)
    saved = qboard.load(project_dir)["choices"]
    choices = qboard.effective_choices(doc, saved, latest_decisions(project_dir))
    resolved = qmap.load_resolved(project_dir) or {}
    cands = {c["key"]: c for c in resolved.get("candidates") or []}
    revisions = qmap.load_revisions(project_dir)
    mappings = draft_mappings(project_dir)
    index = load_index(resolved.get("version") or "nbla")
    applied = applied_cards(project_dir)
    cards = doc["cards"]
    rows_of: dict[str, dict[int, str]] = {}

    items, gold = [], []
    stats: Counter = Counter()
    for card in cards:
        split = split_of(book, card["chapter_id"])
        if split is None or card.get("status") != qmap.CARD_PROPOSED:
            continue
        rows = rows_of.setdefault(card["chapter_id"],
                                  map_time_rows(project_dir, card["chapter_id"], cards, applied, overrides))
        edits = original_edits(project_dir, card, rows, cands.get(card["key"]) or {}, mappings, index)
        if not any(e["old"] != e["new"] for e in edits):
            stats[f"{split}: no text change or no original"] += 1
            continue
        choice = choices[card["key"]] if card["key"] in saved else None
        label, fix = label_of(card, choice, revision=revisions.get(card["key"]), original=edits,
                              clicks_only=split == "held")
        over = overrides.get(card["key"])
        if over:
            label, fix = over.get("label", label), over.get("fix", fix)
        if label is None:
            stats[f"{split}: no label"] += 1
            continue
        view_card = {**card, "drift": (mappings.get(card["key"]) or {}).get("drift", card.get("drift"))
                     if card.get("revised") else card.get("drift")}
        item = {**qjudge.item_view(view_card, edits, rows, cands.get(card["key"], {}).get("verses") or []),
                "book": book}
        stats[f"{split}: edits not found in their row"] += sum(1 for e in item["edits"] if not e["found"])
        items.append((split, item))
        gold.append((split, {"key": card["key"], "book": book, "chapter_id": card["chapter_id"],
                             "ref": card.get("ref"), "label": label, "fix": fix,
                             "saved": card["key"] in saved}))
    return items, gold, stats


def _split_path(kind: str, split: str) -> Path:
    return OUT / f"{kind}.{split}.json"


def _load_split(kind: str, split: str) -> list[dict[str, Any]]:
    return json.loads(_split_path(kind, split).read_text(encoding="utf-8"))


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_build(_args: argparse.Namespace) -> int:
    overrides_path = OUT / "overrides.json"
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
    by_split: dict[str, dict[str, list]] = {s: {"items": [], "gold": []} for s in SPLITS}
    stats: Counter = Counter()
    for book in BOOKS:
        items, gold, book_stats = build_book(book, overrides)
        stats.update({f"{book[:5]} {k}": v for k, v in book_stats.items()})
        for split, item in items:
            by_split[split]["items"].append(item)
        for split, row in gold:
            by_split[split]["gold"].append(row)
    report: dict[str, Any] = {"status": "ok", "stats": dict(stats), "splits": {}}
    for split, parts in by_split.items():
        _write(_split_path("items", split), parts["items"])
        _write(_split_path("gold", split), parts["gold"])
        report["splits"][split] = {
            "cards": len(parts["gold"]),
            "labels": dict(Counter(g["label"] for g in parts["gold"])),
            "by_book": dict(Counter(g["book"][:5] for g in parts["gold"])),
            "place": dict(Counter(e["place"] for i in parts["items"] for e in i["edits"])),
            "blocked": sum(1 for i in parts["items"] if i["blocks"]),
        }
    held_refs = {i["ref"] for i in by_split["held"]["items"]}
    used = {ex["ref"] for ex in qjudge.load_examples() if ex.get("ref")}
    report["examples_also_in_held"] = sorted(used & held_refs)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _root(split: str, run: str) -> Path:
    return OUT / "runs" / f"{split}-{run}"


def cmd_prepare(args: argparse.Namespace) -> int:
    items = _load_split("items", args.split)
    out = qjudge.write_jobs(
        _root(args.split, args.run), items, project_dir=_REPO_ROOT / "projects" / WAVE_PROJECT,
        worker_model=args.worker_model, cli=args.cli, items_per_job=args.items_per_job,
        keep_drafts=args.keep_drafts,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_fanout(args: argparse.Namespace) -> int:
    out = qjudge.run_jobs(
        _root(args.split, args.run), project_dir=_REPO_ROOT / "projects" / WAVE_PROJECT,
        concurrency=args.concurrency, effort=args.effort,
        job_ids=[j.strip() for j in args.job_ids.split(",") if j.strip()] if args.job_ids else None,
    )
    print(json.dumps({k: v for k, v in out.items() if k != "_schema"}, ensure_ascii=False, indent=2))
    counts = out.get("counts") or {}
    return 1 if out.get("error") or (counts.get("failed") and not counts.get("wrote")) else 0


def score(gold: list[dict[str, Any]], verdicts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Confusion, and per floor how many high-confidence verdicts were wrong."""
    rows = [(g, verdicts[g["key"]]) for g in gold if g["key"] in verdicts]
    confusion = Counter(f"{g['label']} -> {v['verdict']}" for g, v in rows)
    sweep = []
    for floor in FLOORS:
        applies = [(g, v) for g, v in rows if v["verdict"] == "apply" and v["confidence"] >= floor]
        rejects = [(g, v) for g, v in rows if v["verdict"] == "reject" and v["confidence"] >= floor]
        sweep.append({
            "floor": floor,
            "apply": len(applies),
            "apply_wrong": sum(1 for g, _v in applies if g["label"] in NOT_APPLY),
            "reject": len(rejects),
            "reject_wrong": sum(1 for g, _v in rejects if g["label"] == "apply"),
            "share_decided": round((len(applies) + len(rejects)) / len(rows), 3) if rows else None,
        })
    return {
        "scored": len(rows),
        "unanswered": len(gold) - len(rows),
        "exact": sum(1 for g, v in rows if g["label"] == v["verdict"]),
        "confusion": dict(sorted(confusion.items())),
        "by_book_exact": {
            book: {"cards": sum(1 for g, _v in rows if g["book"] == book),
                   "exact": sum(1 for g, v in rows if g["book"] == book and g["label"] == v["verdict"])}
            for book in sorted({g["book"] for g in gold})
        },
        "clamped": sum(1 for _g, v in rows if v.get("model_verdict")),
        "sweep": sweep,
    }


def cmd_score(args: argparse.Namespace) -> int:
    root = _root(args.split, args.run)
    got = qjudge.collect(root)
    if got["status"] == "error":
        print(json.dumps(got, ensure_ascii=False, indent=2))
        return 1
    # A card shown to the model as an example says nothing about how it does.
    shown = {ex.get("key") for ex in qjudge.load_examples()}
    gold = [g for g in _load_split("gold", args.split) if g["key"] not in shown]
    if args.split == "held":
        stamp = OUT / "held.scored.json"
        prompt = got["manifest"].get("prompt_version")
        if stamp.exists():
            earlier = json.loads(stamp.read_text(encoding="utf-8"))
            if earlier.get("prompt_version") != prompt and not args.rescore:
                print(json.dumps({"status": "error", "error": (
                    "this set was already scored with another prompt, so it is dev data now. "
                    "Pass --rescore only to read it as that."), "earlier": earlier}, indent=2))
                return 1
        else:
            _write(stamp, {"prompt_version": prompt, "run": args.run,
                           "at": datetime.now().isoformat(timespec="seconds")})
    report = {"status": got["status"], "failed": got["failed"], "missing": got["missing"],
              "model": got["manifest"].get("worker_model"), **score(gold, got["verdicts"])}
    wrong = []
    for row in gold:
        verdict = got["verdicts"].get(row["key"])
        if verdict and row["label"] != verdict["verdict"]:
            wrong.append({**row, **{f"judge_{k}": verdict.get(k) for k in ("verdict", "confidence", "fix", "reason")}})
    wrong.sort(key=lambda w: -w["judge_confidence"])
    _write(root / "wrong.json", wrong)
    report["wrong_path"] = str(root / "wrong.json")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="freeze the gold items and labels")
    prepare = sub.add_parser("prepare", help="render one split's jobs")
    fanout = sub.add_parser("fanout", help="run them headless")
    score_p = sub.add_parser("score", help="confusion matrix and confidence sweep")
    for p in (prepare, fanout, score_p):
        p.add_argument("--split", choices=SPLITS, required=True)
        p.add_argument("--run", required=True, help="a name for this run, e.g. sonnet")
    prepare.add_argument("--worker-model", required=True)
    prepare.add_argument("--cli", required=True)
    prepare.add_argument("--items-per-job", type=int, default=qjudge.DEFAULT_ITEMS_PER_JOB)
    prepare.add_argument("--keep-drafts", action="store_true")
    fanout.add_argument("--concurrency", type=int, default=None)
    fanout.add_argument("--effort", default=None)
    fanout.add_argument("--job-ids", default=None)
    score_p.add_argument("--rescore", action="store_true", help="read a spent held-out set as dev data")
    args = parser.parse_args(argv)
    return {"build": cmd_build, "prepare": cmd_prepare, "fanout": cmd_fanout, "score": cmd_score}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
