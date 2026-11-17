#!/usr/bin/env python3
"""
The last mile to a Kindle listing: facts, Amazon evidence, the KDP form.

This is the store's half of a book and it runs second. The book's own pages
(title page, copyright page, ``dc:description``) are written by
``scripts/book_metadata.py``; ``check`` holds the listing to them, and ``apply``
writes only ``kdp_listing.json``.

Subcommands, each printing exactly one JSON object:

    facts         book + edition facts -> .harness/kdp/facts.json      (one cached fetch)
    suggest       Amazon autocomplete for keyword seeds                (HTTP, no browser)
    comps         comparable books' BSR ladders, reading age, price    (headless browser)
    category      a category's tree position and rank-to-sales signal  (headless browser)
    check         validate a listing against KDP's rules and the book  (no network)
    report        the dated report for the approval gate               (no network)
    apply         write kdp_listing.json
    verify-epub   read the built Kindle EPUB back and confirm the metadata landed
    published     record the live listing's ASIN and date in kdp_listing.json

The agent drafts the listing (``.harness/kdp/listing.json``); these commands
gather what it is drafted from and refuse what KDP would refuse. ``comps`` and
``category`` need Playwright (``requirements-kdp.txt``); everything else runs
on the base install. Each command's result is also kept in
``.harness/kdp/last_output.<command>.json``.

Typical flow (the kdp-publish skill drives it, with a STOP gate before apply):

    python scripts/book_metadata.py apply  --project bambi-a-life-in-the-woods   (first)
    python scripts/kdp_publish.py facts    --project bambi-a-life-in-the-woods
    python scripts/kdp_publish.py suggest  --project bambi-a-life-in-the-woods \
        --seeds-file projects/bambi-a-life-in-the-woods/.harness/kdp/seeds.txt --expand
    python scripts/kdp_publish.py comps    --project bambi-a-life-in-the-woods \
        --query "bambi en español" --query "novelas de animales para niños"
    python scripts/kdp_publish.py category --project bambi-a-life-in-the-woods --node 8407404011
    python scripts/kdp_publish.py check    --project bambi-a-life-in-the-woods
    python scripts/kdp_publish.py report   --project bambi-a-life-in-the-woods
    python scripts/kdp_publish.py apply    --project bambi-a-life-in-the-woods --dry-run
    python scripts/kdp_publish.py verify-epub --project bambi-a-life-in-the-woods
    python scripts/kdp_publish.py published --project bambi-a-life-in-the-woods --asin B0XXXXXXXX
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every accent in the
# JSON we print. The hasattr guard keeps this safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time so it can't corrupt the JSON an agent parses.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.kdp import load_defaults, work_dir  # noqa: E402

_OUTPUT_DIR: Path | None = None
_COMMAND = ""


def _die(message: str) -> None:
    """Exit with one JSON error object — never a bare traceback."""
    raise SystemExit(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))


def _resolve_project(arg: str) -> Path:
    """A book's project dir, or a collection's dir (a volume is listed like a book)."""
    from src import collection as collection_mod

    try:
        return collection_mod.resolve_book_or_collection(arg)
    except FileNotFoundError as exc:
        _die(str(exc))
        raise AssertionError("unreachable")  # pragma: no cover


def _emit(payload: dict) -> None:
    """Print one JSON object, after mirroring it to ``.harness/kdp/``.

    The same contract as ``footnote_pass.py``: a file on disk is ``Read``-able
    without a second interpreter in the loop. ``last_output.<command>.json``
    keeps each command's result, so two commands running at once (``suggest``
    in the background, say) cannot overwrite each other; ``last_output.json`` is
    whichever finished last. Best-effort by design.
    """
    if _OUTPUT_DIR is not None:
        try:
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            out = _OUTPUT_DIR / f"last_output.{_COMMAND}.json"
            for path in (_OUTPUT_DIR / "last_output.json", out):
                path.write_text(text, encoding="utf-8")
            print(f"OUTPUT_JSON: {out}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - a convenience, never a hard dependency
            print(f"warning: could not write last_output.json: {exc}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        _die(f"unreadable JSON {path}: {exc}")


def _listing(project_dir: Path, arg: str | None) -> tuple[dict, Path]:
    from src.kdp import listing as kdp_listing

    path = Path(arg) if arg else work_dir(project_dir) / "listing.json"
    if not path.exists():
        _die(f"no listing at {path}: draft one first (see the kdp-publish skill, step 4)")
    try:
        return kdp_listing.load(path), path
    except (ValueError, OSError) as exc:
        _die(f"unreadable listing {path}: {exc}")
        raise AssertionError("unreachable")  # pragma: no cover


# ── commands ────────────────────────────────────────────────────────────────

def cmd_facts(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import facts as kdp_facts

    from src.kdp.listing import book_state

    facts = kdp_facts.gather(project_dir, fetch=not args.no_fetch)
    catalog = facts.get("catalog") or {}
    book = book_state(project_dir)
    return {
        "status": "ok",
        "facts_path": facts["_path"],
        # The listing reads the edition; without one, book-metadata runs first.
        "edition_applied": book["edition_applied"],
        "book": {k: book[k] for k in ("title", "subtitle", "translator", "edition", "endnotes")},
        "site": facts["site"],
        "existing_listing": facts["existing_listing"],
        "title": facts["titles"]["spanish_title"] or facts["titles"]["project_title"],
        "author": facts["author"],
        "gutenberg_id": facts["source"]["gutenberg_id"],
        "catalog": {"lcsh": catalog.get("lcsh"), "bookshelves": catalog.get("bookshelves"),
                    "source_edition_year": catalog.get("source_edition_year")} if catalog else None,
        "catalog_error": facts["catalog_error"],
        "edition": {"descriptor": facts["edition"]["descriptor"],
                    "additions": facts["edition"]["additions"]},
        "footnotes": facts["footnotes"],
        "images": {k: facts["images"][k] for k in
                   ("new_cover", "translated_lettering", "restored", "new_illustrations")},
        "rights_flags": [f["code"] for f in facts["rights_flags"]],
        "needs_input": [n["field"] for n in facts["needs_input"]],
        "defaults_source": facts["defaults_source"],
    }


def cmd_suggest(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import suggest

    seeds = list(args.seed or [])
    if args.seeds_file:
        path = Path(args.seeds_file)
        if not path.exists():
            _die(f"--seeds-file not found: {path}")
        seeds += [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    if not seeds:
        _die("no seeds: pass --seed or --seeds-file")
    defaults = load_defaults()
    result = suggest.collect(
        seeds,
        mid=defaults.get("marketplace_id") or "ATVPDKIKX0DER",
        lop=defaults.get("locale") or "es_US",
        do_expand=args.expand,
        delay=args.delay,
    )
    path = suggest.write(result, work_dir(project_dir) / "suggestions.json")
    return {
        "status": "ok",
        "suggestions_path": str(path),
        "prefixes_queried": result["prefixes_queried"],
        "phrases": len(result["phrases"]),
        "kindle_scoped_replies": result["kindle_scoped_replies"],
        "dropped_off_seed": result["dropped_off_seed"],
        "top": [f"{p['phrase']} ({p['hits']}x, best #{p['best_position']}"
                f"{'' if p['kindle'] else ', all-store'})"
                for p in result["phrases"][:args.top]],
        "errors": result["errors"][:5],
    }


def _merge_comps(existing: dict | None, new: list[dict], queries: list[dict]) -> dict:
    by_asin = {c["asin"]: c for c in (existing or {}).get("listings") or [] if c.get("asin")}
    for listing in new:
        old = by_asin.get(listing["asin"])
        # A failed --refresh keeps the listing it already had.
        if listing.get("error") and old and not old.get("error"):
            continue
        by_asin[listing["asin"]] = listing
    return {
        "queries": ((existing or {}).get("queries") or []) + queries,
        "listings": list(by_asin.values()),
    }


def _node_counts(listings: list[dict]) -> list[dict]:
    """Kindle nodes the Spanish-language comps rank in, most-shared first."""
    counts: dict[str, dict] = {}
    for c in listings:
        if c.get("language") and c["language"] != "Spanish":
            continue
        for rung in (c.get("bsr") or {}).get("categories") or []:
            if rung.get("store") != "digital-text" or not rung.get("node"):
                continue
            entry = counts.setdefault(rung["node"], {"node": rung["node"], "name": rung["name"],
                                                     "books": 0, "best_rank": rung.get("rank")})
            entry["books"] += 1
            if rung.get("rank") and (entry["best_rank"] is None or rung["rank"] < entry["best_rank"]):
                entry["best_rank"] = rung["rank"]
    return sorted(counts.values(), key=lambda e: (-e["books"], e["best_rank"] or 10**9))


def cmd_comps(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import amazon

    if not args.query and not args.asin:
        _die("pass at least one --query or --asin")
    path = work_dir(project_dir) / "comps.json"
    existing = None if args.reset else _read_json(path)
    # An ASIN that errored last time is retried, not counted as fetched.
    seen = {c.get("asin") for c in (existing or {}).get("listings") or [] if not c.get("error")}
    fetched: list[dict] = []
    queries: list[dict] = []
    try:
        with amazon.AmazonSession(delay=args.delay) as session:
            targets: list[str] = list(args.asin or [])
            for query in args.query or []:
                try:
                    results = session.search(query)
                except amazon.PageError as exc:
                    queries.append({"query": query, "results": 0, "asins": [], "error": str(exc)})
                    continue
                organic = [r for r in results if not r["sponsored"]]
                queries.append({"query": query, "results": len(results),
                                "asins": [r["asin"] for r in organic[:args.max]]})
                targets += [r["asin"] for r in organic[:args.max]]
            for asin in dict.fromkeys(targets):
                if asin in seen and not args.refresh:
                    continue
                try:
                    fetched.append(session.listing(asin))
                except amazon.PageError as exc:
                    fetched.append({"asin": asin, "error": str(exc)})
            pages = session.pages_loaded
    except RuntimeError as exc:
        _die(str(exc))
    doc = _merge_comps(existing, fetched, queries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "comps_path": str(path),
        "pages_loaded": pages,
        "fetched": len(fetched),
        "total_listings": len(doc["listings"]),
        "queries": [{"query": q["query"], "results": q["results"], "kept": len(q["asins"]),
                     **({"error": q["error"]} if q.get("error") else {})} for q in queries],
        "books": [
            {"asin": c.get("asin"), "title": (c.get("title") or "")[:60],
             "language": c.get("language"), "reading_age": c.get("reading_age"),
             "overall": (c.get("bsr") or {}).get("overall"),
             "error": c.get("error")}
            for c in fetched
        ],
        "kindle_categories": _node_counts(doc["listings"])[:args.top],
    }


def cmd_category(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import amazon

    try:
        sample_ranks = [int(x) for x in args.sample.split(",") if x.strip()]
    except ValueError:
        _die("--sample takes comma-separated integers")
    path = work_dir(project_dir) / "categories.json"
    existing = _read_json(path) or {}
    by_node = {c["node"]: c for c in existing.get("categories") or []}
    errors: list[dict] = []
    try:
        with amazon.AmazonSession(delay=args.delay) as session:
            for node in args.node:
                try:
                    page = session.bestsellers(node, store=args.store)
                except amazon.PageError as exc:
                    # Keep the nodes that did load; an earlier run's entry stays as it was.
                    errors.append({"node": node, "error": str(exc)})
                    continue
                by_rank = {item["rank"]: item for item in page["items"]}
                sample = []
                for rank in sample_ranks:
                    item = by_rank.get(rank)
                    if not item or not item.get("asin"):
                        continue
                    try:
                        detail = session.listing(item["asin"])
                        overall = detail["bsr"]["overall"]
                    except amazon.PageError:
                        overall = None
                    sample.append({"category_rank": rank, "asin": item["asin"],
                                   "title": item["title"][:80], "overall": overall})
                by_node[node] = {
                    "node": node, "store": args.store, "name": page["name"],
                    "tree_label": page["tree_label"], "path": page["path"],
                    "siblings": page["siblings"], "children": page["children"],
                    "top": page["items"][:10], "sample": sample,
                }
            pages = session.pages_loaded
    except RuntimeError as exc:
        _die(str(exc))
    doc = {"categories": list(by_node.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "categories_path": str(path),
        "pages_loaded": pages,
        "categories": [
            {"node": by_node[n]["node"], "name": by_node[n]["name"],
             "path": " › ".join(by_node[n]["path"]),
             "children": len(by_node[n]["children"]),
             "sample": [f"#{s['category_rank']}: overall {s['overall']}" for s in by_node[n]["sample"]]}
            for n in args.node if n in by_node and not any(e["node"] == n for e in errors)
        ],
        "errors": errors,
    }


def cmd_check(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp.listing import book_state
    from src.kdp.validate import check

    from src.kdp.facts import with_live_site

    listing, path = _listing(project_dir, args.listing_file)
    facts = with_live_site(_read_json(work_dir(project_dir) / "facts.json"), project_dir)
    verdict = check(listing, load_defaults(), facts=facts, book=book_state(project_dir))
    return {"status": "ok" if verdict["ok"] else "errors", "listing": str(path), **verdict}


def cmd_report(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import report

    listing, _path = _listing(project_dir, args.listing_file)
    out = report.write(project_dir, listing, load_defaults())
    return {"status": "ok", "report_path": str(out)}


def cmd_apply(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import listing as kdp_listing
    from src.kdp.facts import with_live_site

    listing, _path = _listing(project_dir, args.listing_file)
    facts = with_live_site(_read_json(work_dir(project_dir) / "facts.json"), project_dir)
    return kdp_listing.apply(project_dir, listing, load_defaults(), facts=facts, dry_run=args.dry_run)


def cmd_verify_epub(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp.verify import verify

    result = verify(project_dir, Path(args.epub) if args.epub else None)
    return {"status": "ok" if result["ok"] else "failed", **result}


def cmd_published(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import listing as kdp_listing

    return kdp_listing.mark_published(project_dir, args.asin, when=args.date)


# ── argparse ────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KDP listing: facts, Amazon evidence, validation, apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _cmd(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--project", required=True, help="Project id or path")
        return p

    p = _cmd("facts", "Gather book and edition facts")
    p.add_argument("--no-fetch", action="store_true", help="Skip the Gutenberg catalog fetch")

    p = _cmd("suggest", "Amazon Kindle autocomplete for keyword seeds")
    p.add_argument("--seed", action="append", help="A seed phrase (repeatable)")
    p.add_argument("--seeds-file", help="One seed per line; # comments allowed")
    p.add_argument("--expand", action="store_true", help="Also query each seed + a..z")
    p.add_argument("--delay", type=float, default=0.4, help="Seconds between requests")
    p.add_argument("--top", type=int, default=25, help="Phrases to print (all go to the file)")

    p = _cmd("comps", "Comparable books' BSR ladders (needs Playwright)")
    p.add_argument("--query", action="append", help="A Kindle-store search (repeatable)")
    p.add_argument("--asin", action="append", help="A specific listing (repeatable)")
    p.add_argument("--max", type=int, default=6, help="Organic results kept per query")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds after each page load")
    p.add_argument("--refresh", action="store_true", help="Refetch ASINs already in comps.json")
    p.add_argument("--reset", action="store_true", help="Start comps.json over")
    p.add_argument("--top", type=int, default=12, help="Category nodes to print")

    p = _cmd("category", "A category's tree position and rank-to-sales signal (needs Playwright)")
    p.add_argument("--node", action="append", required=True, help="Browse-node id (repeatable)")
    p.add_argument("--store", default="digital-text", help="Store of the node (default digital-text)")
    p.add_argument("--sample", default="1,10,20", help="Category ranks whose overall BSR to read")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds after each page load")

    for name, help_ in (("check", "Validate a listing against KDP's rules"),
                        ("report", "Write the dated report for the approval gate"),
                        ("apply", "Write the approved listing to kdp_listing.json")):
        p = _cmd(name, help_)
        p.add_argument("--listing-file", help="Default: .harness/kdp/listing.json")
        if name == "apply":
            p.add_argument("--dry-run", action="store_true", help="Show the diff, write nothing")

    p = _cmd("verify-epub", "Confirm the built Kindle EPUB carries the metadata")
    p.add_argument("--epub", help="Default: the newest *.epub in the project folder")

    p = _cmd("published", "Record the live listing's ASIN and date")
    p.add_argument("--asin", required=True, help="The Kindle listing's ASIN (B0…)")
    p.add_argument("--date", help="Publication date, YYYY-MM-DD (default: today)")
    return parser


_COMMANDS = {
    "facts": cmd_facts, "suggest": cmd_suggest, "comps": cmd_comps, "category": cmd_category,
    "check": cmd_check, "report": cmd_report, "apply": cmd_apply, "verify-epub": cmd_verify_epub,
    "published": cmd_published,
}


def main(argv: list[str] | None = None) -> int:
    global _OUTPUT_DIR, _COMMAND
    args = _parser().parse_args(argv)
    project_dir = _resolve_project(args.project)
    _OUTPUT_DIR = work_dir(project_dir)
    _COMMAND = args.command
    payload = _COMMANDS[args.command](args, project_dir)
    _emit(payload)
    return 0 if payload.get("status") in ("ok", "applied", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
