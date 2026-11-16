#!/usr/bin/env python3
"""
A book's edition and its site texts: what to draft from, the checks, the writes.

Everything a book says about itself in either EPUB or on the site is written
here: the title page and copyright page (the edition), «Sobre este libro», and
the site's description, «Sobre esta edición» and parent notes. The KDP listing
(``kdp_publish.py``) comes after and reads what this wrote.

Subcommands, each printing exactly one JSON object:

    status       the content review, the built EPUB and its note count, the edition
                 as it stands, what is written, and the facts' open questions
    facts        everything known about the book and this edition (.harness/kdp/facts.json)
    notes        every endnote of the built EPUB, under the chapter the reader sees
    changes      the hand edits that changed a number or a proper noun
    source-diff  what source.txt changed against the text a review was written on
    draft        put texts.json into metadata_draft.md's fenced blocks (--pull: read them back)
    check        validate .harness/metadata/texts.json                      (no writes)
    apply        write project.json, edition_notice.json and about_book.json
    site         write the site repo's book folder: adaptaciones.md, padres.md,
                 descripcion.md and the resumen line of libro.toml
    verify       read the built EPUBs back: Kindle, libre, or both

The agent drafts the texts (``.harness/metadata/texts.json``, with a readable
copy in ``metadata_draft.md``); these commands say what the draft can rest on
and refuse what a page cannot render or what the built EPUB contradicts.

Typical flow (the book-metadata skill drives it, with a STOP gate before apply):

    python scripts/book_metadata.py status --project my-book
    python scripts/book_metadata.py notes  --project my-book
    python scripts/book_metadata.py check  --project my-book
    python scripts/book_metadata.py apply  --project my-book --dry-run
    python scripts/book_metadata.py apply  --project my-book
    python scripts/book_metadata.py site   --project my-book
    python scripts/harness.py epub --project my-book          (and --target libre)
    python scripts/book_metadata.py verify --project my-book
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every accent in the
# JSON we print. The hasattr guard keeps this safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time so it can't corrupt the JSON an agent parses.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src import collection as collection_mod  # noqa: E402
from src import edition_record, site_texts  # noqa: E402

_OUTPUT_DIR: Path | None = None
_COMMAND = ""


def _die(message: str) -> None:
    """Exit with one JSON error object, never a bare traceback."""
    raise SystemExit(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))


def _emit(payload: dict) -> None:
    """Print one JSON object, after mirroring it to ``.harness/metadata/``.

    ``last_output.<command>.json`` keeps each command's result; ``last_output.json``
    is the most recent one, whatever the command.
    """
    if _OUTPUT_DIR is not None:
        try:
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            for name in ("last_output.json", f"last_output.{_COMMAND}.json"):
                (_OUTPUT_DIR / name).write_text(text, encoding="utf-8")
            print(f"OUTPUT_JSON: {_OUTPUT_DIR / f'last_output.{_COMMAND}.json'}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - a convenience, never a hard dependency
            print(f"warning: could not write last_output.json: {exc}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _texts_path(project_dir: Path, arg: str | None) -> Path:
    return Path(arg) if arg else site_texts.work_dir(project_dir) / site_texts.TEXTS_FILE


def _texts(project_dir: Path, arg: str | None) -> tuple[dict, Path]:
    path = _texts_path(project_dir, arg)
    try:
        return site_texts.load_texts(path), path
    except ValueError as exc:
        _die(f"{exc} (the book-metadata skill drafts it at step 6)")
        raise AssertionError("unreachable")  # pragma: no cover


def _context(project_dir: Path, epub: str | None) -> dict:
    return site_texts.gather_context(project_dir, Path(epub) if epub else None)


def _facts_summary(project_dir: Path, *, fetch: bool) -> dict:
    """The part of the facts a draft starts from; the whole of it is at ``path``."""
    from src.kdp import facts as kdp_facts

    facts = kdp_facts.gather(project_dir, fetch=fetch)
    return {
        "path": facts.get("_path"),
        "needs_input": facts["needs_input"],
        "rights_flags": facts["rights_flags"],
        "footnotes": facts["footnotes"],
        "images": {k: facts["images"].get(k) for k in
                   ("new_cover", "translated_lettering", "restored", "new_illustrations", "models")},
        "additions": facts["edition"]["additions"],
        "notice_draft": facts["edition"]["notice_draft"],
        "house_subtitle": facts["edition"]["house_subtitle"],
        "translator": facts["translator"],
        "site": facts.get("site"),
        "existing_listing": facts.get("existing_listing"),
    }


def cmd_status(args: argparse.Namespace, project_dir: Path) -> dict:
    payload = site_texts.status(project_dir)
    work = site_texts.work_dir(project_dir)
    work.mkdir(parents=True, exist_ok=True)
    current_path = work / site_texts.CURRENT_FILE
    current_path.write_text(
        json.dumps(site_texts.current_texts(project_dir), ensure_ascii=False, indent=2),
        encoding="utf-8")
    payload["current"]["path"] = str(current_path)
    if not args.no_facts:
        payload["facts"] = _facts_summary(project_dir, fetch=not args.no_fetch)
        for item in payload["facts"]["needs_input"]:
            payload["needs"].append(f"facts: {item['field']}: {item['why']}")
    return payload


def cmd_facts(args: argparse.Namespace, project_dir: Path) -> dict:
    from src.kdp import facts as kdp_facts

    return kdp_facts.gather(project_dir, fetch=not args.no_fetch)


def cmd_notes(args: argparse.Namespace, project_dir: Path) -> dict:
    return edition_record.notes(project_dir, Path(args.epub) if args.epub else None)


def cmd_changes(args: argparse.Namespace, project_dir: Path) -> dict:
    """A book's hand edits; for a collection, each work's, since each keeps its own log."""
    book_works = site_texts.works(project_dir)
    if len(book_works) == 1 and book_works[0][0] == "":
        return edition_record.changes(project_dir)
    by_work = {slug: edition_record.changes(work_dir) for slug, work_dir in book_works}
    return {"ok": True, "total": sum(r["total"] for r in by_work.values()),
            "listed": sum(r.get("listed", 0) for r in by_work.values()), "works": by_work}


def cmd_source_diff(args: argparse.Namespace, project_dir: Path) -> dict:
    return edition_record.source_diff(project_dir, Path(args.against), max_hunks=args.max,
                                      min_words=args.min_words)


def cmd_draft(args: argparse.Namespace, project_dir: Path) -> dict:
    path = _texts_path(project_dir, args.texts_file)
    if args.pull:
        try:
            texts = site_texts.load_texts(path) if path.exists() else {}
        except ValueError as exc:
            _die(str(exc))
        pulled = site_texts.pull_draft(project_dir, texts)
        if pulled["errors"]:
            return {"ok": False, "pulled": False, "texts_file": str(path), **pulled, "texts": None}
        if pulled["changed"] and not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(pulled["texts"], ensure_ascii=False, indent=2),
                            encoding="utf-8")
        report = site_texts.check(pulled["texts"], context=_context(project_dir, None))
        return {"ok": not report["errors"], "pulled": not args.dry_run, "dry_run": args.dry_run,
                "texts_file": str(path), "changed": pulled["changed"], **report}
    texts, _path = _texts(project_dir, args.texts_file)
    title = site_texts.status(project_dir)["title"]
    return {"ok": True, **site_texts.render_draft(project_dir, texts, title=title,
                                                  dry_run=args.dry_run)}


def cmd_check(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    context = _context(project_dir, args.epub)
    report = site_texts.check(texts, context=context)
    return {"ok": not report["errors"], "texts_file": str(path), "epub": context["epub"],
            "endnotes": context["endnotes"], **report}


def cmd_apply(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    context = _context(project_dir, args.epub)
    result = site_texts.apply(project_dir, texts, dry_run=args.dry_run, context=context)
    return {"project": project_dir.name, "texts_file": str(path), "epub": context["epub"], **result}


def cmd_site(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    result = site_texts.site(project_dir, texts, Path(args.site_dir) if args.site_dir else None,
                             dry_run=args.dry_run)
    return {"project": project_dir.name, "texts_file": str(path), **result}


def cmd_verify(args: argparse.Namespace, project_dir: Path) -> dict:
    """Read the built EPUBs back. With no ``--target``, every build that exists is checked."""
    from src.kdp.verify import KINDLE, LIBRE, verify

    if args.epub and not args.target:
        _die("--epub needs --target, so the right rules are applied to it")
    results = {}
    for target in ([args.target] if args.target else [KINDLE, LIBRE]):
        result = verify(project_dir, Path(args.epub) if args.epub else None, target=target)
        if result.get("error") and not args.target:
            results[target] = {"skipped": result["error"]}
            continue
        results[target] = result
    checked = [r for r in results.values() if "skipped" not in r]
    return {"ok": bool(checked) and all(r["ok"] for r in checked), "targets": results}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--project", required=True, help="Project slug or path (a collection too)")
        return p

    p = add("status", "What a draft can be built from, and what is already written")
    p.add_argument("--no-facts", action="store_true", help="Skip gathering the facts")
    p.add_argument("--no-fetch", action="store_true", help="Skip the Gutenberg catalog fetch")

    p = add("facts", "Gather book and edition facts")
    p.add_argument("--no-fetch", action="store_true", help="Skip the Gutenberg catalog fetch")

    p = add("notes", "Every endnote of the built EPUB, by chapter")
    p.add_argument("--epub", help="Default: the newest *.epub in the project folder")

    add("changes", "The hand edits that changed a number or a proper noun")

    p = add("source-diff", "What source.txt changed against the text a review read")
    p.add_argument("--against", required=True, help="The text the content review was written on")
    p.add_argument("--max", type=int, default=80, help="Hunks to list (the count is always whole)")
    p.add_argument("--min-words", type=int, default=1,
                   help="List only hunks of at least this many words (the rewrites, not the small fixes)")

    p = add("draft", "Keep metadata_draft.md's fenced blocks in step with texts.json")
    p.add_argument("--texts-file", help="Default: .harness/metadata/texts.json")
    p.add_argument("--pull", action="store_true",
                   help="Read the draft's blocks into texts.json (the user edited the draft)")
    p.add_argument("--dry-run", action="store_true", help="Report without writing")

    for name, help_ in (("check", "Validate the drafted texts"),
                        ("apply", "Write project.json, edition_notice.json and about_book.json")):
        p = add(name, help_)
        p.add_argument("--texts-file", help="Default: .harness/metadata/texts.json")
        p.add_argument("--epub", help="Default: the newest *.epub in the project folder")
        if name == "apply":
            p.add_argument("--dry-run", action="store_true", help="Report without writing")

    p = add("site", "Write the site repo's book folder")
    p.add_argument("--texts-file", help="Default: .harness/metadata/texts.json")
    p.add_argument("--site-dir", help="The site repo checkout (default: site.repo_dir in "
                                      "prompts/publish_defaults.json)")
    p.add_argument("--dry-run", action="store_true", help="Report without writing")

    p = add("verify", "Read the built EPUBs back")
    p.add_argument("--target", choices=("kindle", "libre"),
                   help="Default: every build that exists")
    p.add_argument("--epub", help="A specific file (needs --target)")
    return parser


_COMMANDS = {
    "status": cmd_status, "facts": cmd_facts, "notes": cmd_notes, "changes": cmd_changes,
    "source-diff": cmd_source_diff, "draft": cmd_draft, "check": cmd_check, "apply": cmd_apply,
    "site": cmd_site, "verify": cmd_verify,
}


def main(argv: list[str] | None = None) -> int:
    global _OUTPUT_DIR, _COMMAND
    args = _parser().parse_args(argv)
    try:
        project_dir = collection_mod.resolve_book_or_collection(args.project)
    except FileNotFoundError as exc:
        _die(str(exc))
    _OUTPUT_DIR = site_texts.work_dir(project_dir)
    _COMMAND = args.command
    payload = _COMMANDS[args.command](args, project_dir)
    _emit(payload)
    failed = payload.get("ok") is False or bool(payload.get("error")) or (
        args.command == "apply" and bool(payload.get("errors")))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
