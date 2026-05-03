#!/usr/bin/env python3
"""
Build a chapter_manifest for an existing project.

For projects split before front/back-matter support landed, this script
inspects the existing chapter_*.txt files, classifies each as front
matter / chapter / back matter using the same regex set as the splitter,
renumbers chapters sequentially starting at 1, prints the proposed
manifest, asks for confirmation, and writes it back into project.json.

No file renames — annotation IDs (which pin to chapter filenames) stay
valid.

Usage:
    python scripts/build_chapter_manifest.py projects/home-geography
    python scripts/build_chapter_manifest.py projects/home-geography --yes
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.book_splitter import (
    _compile_matter_patterns,
    _matches_user_title,
    _matches_builtin_pattern,
    _normalize_title,
    _TITLE_PUNCT,
)
from src.epub_builder import detect_chapter_heading


def _peek_heading(text: str, max_lines: int = 40) -> Optional[str]:
    """Return the first non-blank line of a chapter file (~heading)."""
    for line in text.splitlines()[:max_lines]:
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def classify_chapter_file(
    text: str,
    *,
    front_titles: List[str],
    back_titles: List[str],
    front_patterns,
    back_patterns,
) -> dict:
    """
    Classify a chapter file by inspecting its first heading line.

    Returns: {"kind": ..., "label": ..., "heading": ...}
    """
    heading_line = _peek_heading(text) or ""

    # 1) User-supplied front titles always win.
    user_front = _matches_user_title(heading_line, front_titles)
    if user_front:
        return {"kind": "front_matter", "label": user_front, "heading": heading_line}

    user_back = _matches_user_title(heading_line, back_titles)
    if user_back:
        return {"kind": "back_matter", "label": user_back, "heading": heading_line}

    # 2) Built-in keyword patterns.
    front_label = _matches_builtin_pattern(heading_line, front_patterns)
    if front_label:
        return {"kind": "front_matter", "label": front_label, "heading": heading_line}

    back_label = _matches_builtin_pattern(heading_line, back_patterns)
    if back_label:
        return {"kind": "back_matter", "label": back_label, "heading": heading_line}

    # 3) Default: numbered chapter.
    return {"kind": "chapter", "label": "", "heading": heading_line}


def build_manifest_for_project(
    project_dir: Path,
    *,
    front_titles: List[str],
    back_titles: List[str],
    auto_front: bool = True,
    auto_back: bool = True,
) -> List[dict]:
    """Inspect chapter_*.txt files and build a chapter_manifest list."""
    chapters_dir = project_dir / "chapters"
    if not chapters_dir.exists():
        raise FileNotFoundError(f"No chapters directory: {chapters_dir}")

    chapter_files = sorted(chapters_dir.glob("chapter_*.txt"))
    if not chapter_files:
        raise FileNotFoundError(f"No chapter_*.txt files in {chapters_dir}")

    front_patterns = _compile_matter_patterns("front_matter_patterns") if auto_front else []
    back_patterns = _compile_matter_patterns("back_matter_patterns") if auto_back else []

    raw = []
    for f in chapter_files:
        text = f.read_text(encoding="utf-8")
        info = classify_chapter_file(
            text,
            front_titles=front_titles,
            back_titles=back_titles,
            front_patterns=front_patterns,
            back_patterns=back_patterns,
        )
        raw.append({"id": f.stem, **info})

    # Front matter must come BEFORE the first chapter; once a chapter is
    # seen, any later front_matter classification is demoted to a chapter.
    # Back matter must come AFTER the last chapter; anything classified as
    # back_matter that appears before the final stretch is demoted too.
    chapter_indexes = [i for i, e in enumerate(raw) if e["kind"] == "chapter"]
    first_chapter = chapter_indexes[0] if chapter_indexes else len(raw)
    last_chapter = chapter_indexes[-1] if chapter_indexes else -1

    for i, e in enumerate(raw):
        if e["kind"] == "front_matter" and i > first_chapter:
            e["kind"] = "chapter"
            e["label"] = ""
        if e["kind"] == "back_matter" and i < last_chapter:
            e["kind"] = "chapter"
            e["label"] = ""

    # Renumber chapter entries sequentially starting at 1.
    chapter_seq = 0
    manifest = []
    for e in raw:
        entry = {"id": e["id"], "kind": e["kind"]}
        if e["kind"] == "chapter":
            chapter_seq += 1
            entry["number"] = chapter_seq
        else:
            if e.get("label"):
                entry["label"] = e["label"]
        manifest.append(entry)

    return manifest


def write_manifest(project_dir: Path, manifest: List[dict]) -> Path:
    """Merge chapter_manifest into project.json."""
    project_json = project_dir / "project.json"
    data = {}
    if project_json.exists():
        try:
            data = json.loads(project_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data["chapter_manifest"] = manifest
    project_json.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return project_json


def parse_args():
    p = argparse.ArgumentParser(
        description="Build chapter_manifest from existing project chapters/"
    )
    p.add_argument("project_dir", type=Path, help="Path to the project directory")
    p.add_argument(
        "--front-matter",
        action="append",
        default=[],
        help="Literal heading string declared as front matter (repeatable / comma-separated).",
    )
    p.add_argument(
        "--back-matter",
        action="append",
        default=[],
        help="Literal heading string declared as back matter (repeatable / comma-separated).",
    )
    p.add_argument("--no-auto-front-matter", action="store_true")
    p.add_argument("--no-auto-back-matter", action="store_true")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the interactive confirmation prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="Print proposed manifest but do not write project.json")
    return p.parse_args()


def _flatten(values):
    out = []
    for v in values or []:
        for piece in str(v).split(","):
            s = piece.strip()
            if s:
                out.append(s)
    return out


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir

    if not project_dir.exists() or not project_dir.is_dir():
        print(f"Error: not a directory: {project_dir}", file=sys.stderr)
        return 1

    try:
        manifest = build_manifest_for_project(
            project_dir,
            front_titles=_flatten(args.front_matter),
            back_titles=_flatten(args.back_matter),
            auto_front=not args.no_auto_front_matter,
            auto_back=not args.no_auto_back_matter,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("\nProposed chapter_manifest:")
    print("-" * 60)
    for entry in manifest:
        cid = entry["id"]
        kind = entry["kind"]
        if kind == "chapter":
            print(f"  {cid}  chapter  #{entry.get('number')}")
        else:
            print(f"  {cid}  {kind}  {entry.get('label', '')!r}")
    print("-" * 60)
    print(f"Total: {len(manifest)} entries")

    if args.dry_run:
        print("Dry run; not writing project.json")
        return 0

    if not args.yes:
        try:
            ans = input("\nWrite this manifest into project.json? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("Aborted.")
            return 1

    out_path = write_manifest(project_dir, manifest)
    print(f"Wrote manifest to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
