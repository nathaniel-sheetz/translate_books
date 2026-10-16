#!/usr/bin/env python3
"""
Report where a book stands against the house layer and its sibling books.

Deterministic and read-only (it only writes the report). Run it at each gate
and after translation:

    python scripts/house_check.py game-legs
    python scripts/house_check.py game-legs --json

Sections: style answers against house rules and siblings, address pairs against
series siblings, the glossary against the house lexicon and siblings, committed
text against the glossary, promotion candidates for the lexicon, and drift since
the book's precedent snapshot. See src/house/check.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.house.check import render_markdown, run_check  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="house_check.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("project", help="project id (under projects/) or path")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    project_dir = Path(args.project)
    if not project_dir.is_dir():
        project_dir = _REPO_ROOT / "projects" / args.project
    if not project_dir.is_dir():
        print(f"project not found: {args.project}", file=sys.stderr)
        return 2

    report = run_check(project_dir)
    markdown = render_markdown(report)
    out = project_dir / "reports" / "house_check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    print(f"REPORT: {out}", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
