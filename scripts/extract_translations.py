#!/usr/bin/env python3
"""
Extract translation text from a comparison run into readable text files.

Usage:
    python scripts/extract_translations.py <run_dir>

Example:
    python scripts/extract_translations.py comparisons/50-famous/c34083d3-e415-4335-9167-d5a41f1fd447

Produces one .txt file per model in the run directory, e.g.:
    translations_claude-sonnet-4-6.txt
    translations_claude-haiku-4-5-20251001.txt
    translations_google_gemma-4-31B-it.txt
"""

import json
import sys
from pathlib import Path


def extract(run_dir: Path) -> None:
    jsonl_files = sorted(run_dir.glob("translations_*.jsonl"))
    if not jsonl_files:
        print(f"No translations_*.jsonl files found in {run_dir}")
        sys.exit(1)

    for jsonl_path in jsonl_files:
        # Read all records, sorted by chapter
        records = []
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                records.append(json.loads(line))
        records.sort(key=lambda r: r["chunk_id"])

        # Write plain text
        txt_path = jsonl_path.with_suffix(".txt")
        model = records[0]["model"] if records else "unknown"
        with txt_path.open("w", encoding="utf-8") as out:
            out.write(f"Model: {model}\n")
            out.write(f"Chunks: {len(records)}\n")
            out.write("=" * 60 + "\n\n")

            for rec in records:
                out.write(f"--- {rec['chapter_id']} ({rec['chunk_id']}) ---\n\n")
                out.write(rec["response"])
                out.write("\n\n")

        print(f"  {txt_path.name} ({len(records)} chunks)")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/extract_translations.py <run_dir>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"Not a directory: {run_dir}")
        sys.exit(1)

    print(f"Extracting from {run_dir}:")
    extract(run_dir)
    print("Done.")


if __name__ == "__main__":
    main()
