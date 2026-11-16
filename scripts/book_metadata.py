#!/usr/bin/env python3
"""
A book's site and «Sobre este libro» texts: what to draft from, the checks, the writes.

Subcommands, each printing exactly one JSON object:

    status   the content review, the built EPUB and its note count, what is written
    check    validate .harness/metadata/texts.json                  (no writes)
    apply    write project.json (forms, adaptations, parent_notes) and about_book.json
    site     write the site repo's book folder: adaptaciones.md, padres.md,
             descripcion.md and the resumen line of libro.toml

The agent drafts the texts (``.harness/metadata/texts.json``, with a readable
copy in ``metadata_draft.md``); these commands say what the draft can rest on
and refuse what the EPUB page cannot render or what the built EPUB contradicts.

Typical flow (the book-metadata skill drives it, with a STOP gate before apply):

    python scripts/book_metadata.py status --project my-book
    python scripts/book_metadata.py check  --project my-book
    python scripts/book_metadata.py apply  --project my-book --dry-run
    python scripts/book_metadata.py apply  --project my-book
    python scripts/book_metadata.py site   --project my-book --dry-run
    python scripts/book_metadata.py site   --project my-book
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every accent in the
# JSON we print. The hasattr guard keeps this safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from src import site_texts  # noqa: E402
from src.harness import state as hstate  # noqa: E402

_OUTPUT_DIR: Path | None = None


def _die(message: str) -> None:
    """Exit with one JSON error object, never a bare traceback."""
    raise SystemExit(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))


def _emit(payload: dict) -> None:
    """Print one JSON object, after mirroring it to ``.harness/metadata/last_output.json``."""
    if _OUTPUT_DIR is not None:
        try:
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out = _OUTPUT_DIR / "last_output.json"
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"OUTPUT_JSON: {out}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - a convenience, never a hard dependency
            print(f"warning: could not write last_output.json: {exc}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _texts(project_dir: Path, arg: str | None) -> tuple[dict, Path]:
    path = Path(arg) if arg else site_texts.work_dir(project_dir) / site_texts.TEXTS_FILE
    try:
        return site_texts.load_texts(path), path
    except ValueError as exc:
        _die(f"{exc} (the book-metadata skill drafts it at step 6)")
        raise AssertionError("unreachable")  # pragma: no cover


def _endnotes(project_dir: Path, arg: str | None) -> tuple[int | None, str | None]:
    """The built EPUB's note count, and the EPUB it was read from."""
    epub = Path(arg) if arg else site_texts.newest_epub(project_dir)
    if not epub or not epub.exists():
        return None, None
    return site_texts.read_epub(epub)["endnotes"], str(epub)


def cmd_status(args: argparse.Namespace, project_dir: Path) -> dict:
    return site_texts.status(project_dir)


def cmd_check(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    endnotes, epub = _endnotes(project_dir, args.epub)
    report = site_texts.check(texts, endnotes=endnotes)
    return {"ok": not report["errors"], "texts_file": str(path), "epub": epub,
            "endnotes": endnotes, **report}


def cmd_apply(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    endnotes, epub = _endnotes(project_dir, args.epub)
    result = site_texts.apply(project_dir, texts, endnotes=endnotes, dry_run=args.dry_run)
    return {"project": project_dir.name, "texts_file": str(path), "epub": epub, **result}


def cmd_site(args: argparse.Namespace, project_dir: Path) -> dict:
    texts, path = _texts(project_dir, args.texts_file)
    result = site_texts.site(project_dir, texts, Path(args.site_dir) if args.site_dir else None,
                             dry_run=args.dry_run)
    return {"project": project_dir.name, "texts_file": str(path), **result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--project", required=True, help="Project slug or path")
        return p

    add("status", "What a draft can be built from, and what is already written")

    for name, help_ in (("check", "Validate the drafted texts"),
                        ("apply", "Write project.json and about_book.json")):
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
    return parser


_COMMANDS = {"status": cmd_status, "check": cmd_check, "apply": cmd_apply, "site": cmd_site}


def main(argv: list[str] | None = None) -> int:
    global _OUTPUT_DIR
    args = _parser().parse_args(argv)
    try:
        project_dir = hstate.resolve_project_dir(args.project).resolve()
    except FileNotFoundError:
        _die(f"project not found: {args.project!r}")
    _OUTPUT_DIR = site_texts.work_dir(project_dir)
    payload = _COMMANDS[args.command](args, project_dir)
    _emit(payload)
    failed = payload.get("ok") is False or bool(payload.get("error")) or (
        args.command == "apply" and bool(payload.get("errors")))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
