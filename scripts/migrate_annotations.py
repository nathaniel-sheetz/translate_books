#!/usr/bin/env python3
"""
Migrate word-level annotations from chunk review_data to sentence-level
annotations.jsonl for the reader.

Maps existing annotation types:
    usage_doubt, translation_doubt -> word_choice
    problem                        -> inconsistency
    footnote                       -> footnote
    note, issue, terminology, question, other -> flag

For each word-level annotation, finds the alignment sentence that contains
the annotated word (using word_text + context) and writes a sentence-level
record to annotations.jsonl.

Usage:
    python scripts/migrate_annotations.py projects/fabre2
    python scripts/migrate_annotations.py projects/fabre2 --dry-run
"""

import argparse
import json
import sys
from pathlib import Path


TYPE_MAP = {
    "usage_doubt": "word_choice",
    "translation_doubt": "word_choice",
    "problem": "inconsistency",
    "footnote": "footnote",
    "note": "flag",
    "issue": "flag",
    "terminology": "flag",
    "question": "flag",
    "other": "flag",
}


def find_sentence_for_word(alignment_data: dict, word_text: str, context_before: list[str], context_after: list[str]) -> int | None:
    """Find the es_idx of the sentence containing the annotated word."""
    # Clean the word (strip punctuation that might be attached)
    clean_word = word_text.strip(".,;:!?\"'()[]{}—–-")

    for a in alignment_data.get("alignments", []):
        es_text = a.get("es", "")
        if clean_word in es_text:
            # Check context for disambiguation if multiple matches
            if context_before:
                ctx = " ".join(context_before[-2:])
                if ctx and ctx in es_text:
                    return a["es_idx"]
            if context_after:
                ctx = " ".join(context_after[:2])
                if ctx and ctx in es_text:
                    return a["es_idx"]
            # Word found but no context match, still use it
            return a["es_idx"]

    # Fallback: try with the raw word_text including punctuation
    for a in alignment_data.get("alignments", []):
        if word_text in a.get("es", ""):
            return a["es_idx"]

    return None


def migrate_project(project_dir: Path, dry_run: bool = False) -> int:
    """Migrate all word-level annotations in a project to annotations.jsonl."""
    chunks_dir = project_dir / "chunks"
    alignments_dir = project_dir / "alignments"

    if not chunks_dir.exists():
        print(f"No chunks/ directory in {project_dir}")
        return 0

    # Load all alignment files
    alignment_cache: dict[str, dict] = {}
    if alignments_dir.exists():
        for ap in sorted(alignments_dir.glob("*.json")):
            with open(ap, encoding="utf-8") as f:
                alignment_cache[ap.stem] = json.load(f)

    # Collect all word-level annotations mapped to (chapter_id, es_idx)
    TYPE_PRIORITY = {"footnote": 3, "word_choice": 2, "inconsistency": 1, "flag": 0}
    pending: dict[tuple[str, int], list[dict]] = {}
    skipped = 0

    for chunk_path in sorted(chunks_dir.glob("*.json")):
        with open(chunk_path, encoding="utf-8") as f:
            chunk = json.load(f)

        review_data = chunk.get("review_data")
        if not review_data:
            continue

        annotations = review_data.get("annotations", [])
        if not annotations:
            continue

        chunk_id = chunk_path.stem
        chapter_id = chunk_id.rsplit("_chunk_", 1)[0]

        alignment_data = alignment_cache.get(chapter_id)
        if not alignment_data:
            print(f"  WARNING: No alignment for {chapter_id}, skipping {len(annotations)} annotations")
            skipped += len(annotations)
            continue

        for ann in annotations:
            word_text = ann.get("word_text", "")
            context_before = ann.get("context_before", [])
            context_after = ann.get("context_after", [])

            es_idx = find_sentence_for_word(alignment_data, word_text, context_before, context_after)

            if es_idx is None:
                print(f"  WARNING: Could not map '{word_text}' in {chunk_id}")
                skipped += 1
                continue

            old_type = ann.get("annotation_type", "other")
            new_type = TYPE_MAP.get(old_type, "flag")

            key = (chapter_id, es_idx)
            if key not in pending:
                pending[key] = []
            pending[key].append({
                "old_type": old_type,
                "new_type": new_type,
                "word_text": word_text,
                "content": ann.get("content"),
                "timestamp": ann.get("created_at", ""),
                "ann_id": ann.get("id", ""),
            })

            print(f"  {chunk_id} [{old_type}->{new_type}] word='{word_text}' -> es_idx={es_idx}")

    # Merge multiple annotations on the same sentence
    records = []
    for (chapter_id, es_idx), items in sorted(pending.items()):
        # Pick highest-priority type
        best_type = max(items, key=lambda x: TYPE_PRIORITY.get(x["new_type"], 0))["new_type"]

        # Merge content: deduplicate, combine notes with word references
        content_parts = []
        seen_words = set()
        for item in items:
            word = item["word_text"]
            if word in seen_words:
                continue
            seen_words.add(word)
            part = f"[{word}]"
            if item["content"]:
                part = f"{item['content']} [{word}]"
            content_parts.append(part)

        content = "; ".join(content_parts)

        record = {
            "project_id": project_dir.name,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "type": best_type,
            "content": content,
            "timestamp": items[0]["timestamp"],
            "migrated_from": ",".join(i["ann_id"] for i in items),
        }
        records.append(record)

    if dry_run:
        multi = sum(1 for v in pending.values() if len(v) > 1)
        print(f"\nDry run: {sum(len(v) for v in pending.values())} word-level annotations -> {len(records)} sentence-level ({multi} merged)")
        if skipped:
            print(f"  {skipped} skipped (no alignment match)")
        return len(records)

    if records:
        annotations_path = project_dir / "annotations.jsonl"
        with open(annotations_path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nMigrated {len(records)} annotations to {annotations_path.name}")

    if skipped:
        print(f"Skipped {skipped} annotations (no alignment match)")

    return len(records)


def main():
    parser = argparse.ArgumentParser(description="Migrate word-level annotations to sentence-level")
    parser.add_argument("project_dir", type=Path, help="Path to project directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated")
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.exists():
        print(f"Error: Project directory not found: {project_dir}")
        sys.exit(1)

    migrate_project(project_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
