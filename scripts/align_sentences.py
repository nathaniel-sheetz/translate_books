"""
Generate sentence-level alignments for translated chunks.

Produces a JSON alignment file mapping each target-language sentence
to its source-language counterpart, for use by the reader UI.

Usage:
    # Align a single chunk
    python scripts/align_sentences.py projects/fabre2/chunks/chapter_06_chunk_000.json

    # Align all chunks for a chapter, output to alignment dir
    python scripts/align_sentences.py projects/fabre2/chunks/chapter_06_chunk_*.json \\
        --output projects/fabre2/alignments/chapter_06.json

    # Test alignment quality (verbose mode)
    python scripts/align_sentences.py projects/fabre2/chunks/chapter_06_chunk_000.json --verbose
"""

import argparse
import glob
import json
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sentence_aligner import align_chunk, align_chapter_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Generate sentence-level alignments for translated chunks"
    )
    parser.add_argument(
        "chunks",
        nargs="+",
        help="Chunk JSON files to align (supports glob patterns)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output path for chapter-level alignment JSON",
    )
    parser.add_argument(
        "--project-id",
        help="Project ID (default: inferred from path)",
    )
    parser.add_argument(
        "--chapter-id",
        help="Chapter ID (default: inferred from path)",
    )
    parser.add_argument(
        "--source-lang",
        default="en",
        help="Source language code (default: en)",
    )
    parser.add_argument(
        "--target-lang",
        default="es",
        help="Target language code (default: es)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed alignment output",
    )
    args = parser.parse_args()

    # Expand glob patterns
    chunk_paths = []
    for pattern in args.chunks:
        expanded = sorted(glob.glob(pattern))
        if expanded:
            chunk_paths.extend(expanded)
        else:
            chunk_paths.append(pattern)

    # Validate paths exist
    for p in chunk_paths:
        if not Path(p).exists():
            print(f"Error: {p} not found", file=sys.stderr)
            sys.exit(1)

    # Infer project_id and chapter_id from first chunk path
    first = Path(chunk_paths[0])
    project_id = args.project_id or first.parts[-3] if len(first.parts) >= 3 else "unknown"
    chapter_id = args.chapter_id
    if not chapter_id:
        # Extract from filename like "chapter_06_chunk_000.json"
        name = first.stem
        parts = name.split("_chunk_")
        chapter_id = parts[0] if parts else name

    print(f"Aligning {len(chunk_paths)} chunk(s)")
    print(f"  Project: {project_id}")
    print(f"  Chapter: {chapter_id}")
    print(f"  Languages: {args.source_lang} -> {args.target_lang}")

    t0 = time.time()

    if len(chunk_paths) == 1 and not args.output:
        # Single chunk, print results
        result = align_chunk(
            chunk_paths[0],
            source_lang=args.source_lang,
            target_lang=args.target_lang,
        )
        elapsed = time.time() - t0

        print(f"\n  EN sentences: {result['en_count']}")
        print(f"  ES sentences: {result['es_count']}")
        print(f"  High confidence: {result['high_confidence_pct']}%")
        print(f"  Avg similarity: {result['avg_similarity']}")
        print(f"  Time: {elapsed:.1f}s")

        if args.verbose:
            print(f"\n  Alignments:")
            for a in result["alignments"]:
                marker = "  " if a["confidence"] == "high" else "??"
                print(
                    f"  {marker} ES[{a['es_idx']:3d}] -> EN[{a['en_idx']:3d}] "
                    f"({a['similarity']:.2f}) {a['es'][:60]}"
                )
                if a["confidence"] == "low":
                    print(f"           EN: {a['en'][:60]}")

        # Print as JSON to stdout if not verbose
        if not args.verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        # Multiple chunks or explicit output path
        output = args.output
        if not output:
            # Default output path
            parent = first.parent.parent
            align_dir = parent / "alignments"
            output = str(align_dir / f"{chapter_id}.json")

        result = align_chapter_chunks(
            chunk_paths,
            project_id=project_id,
            chapter_id=chapter_id,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            output_path=output,
        )
        elapsed = time.time() - t0

        print(f"\n  EN sentences: {result['en_count']}")
        print(f"  ES sentences: {result['es_count']}")
        print(f"  High confidence: {result['high_confidence_pct']}%")
        print(f"  Avg similarity: {result['avg_similarity']}")
        print(f"  Time: {elapsed:.1f}s")
        print(f"  Written to: {output}")

        if args.verbose:
            for a in result["alignments"]:
                marker = "  " if a["confidence"] == "high" else "??"
                print(
                    f"  {marker} ES[{a['es_idx']:3d}] -> EN[{a['en_idx']:3d}] "
                    f"({a['similarity']:.2f}) {a['es'][:60]}"
                )


if __name__ == "__main__":
    main()
