#!/usr/bin/env python3
"""
End-to-end pipeline orchestrator: Gutenberg URL to translated EPUB.

Wires together all existing pipeline stages into a single command.
Supports checkpoint/resume so interrupted runs can continue.

Usage:
    # Full pipeline from Gutenberg URL
    python scripts/translate_book.py --url https://www.gutenberg.org/files/123/123-h/123-h.htm \\
        --project-name my-book --target-lang es

    # Resume after interruption (auto-detects checkpoint)
    python scripts/translate_book.py --project-dir projects/my-book --resume

    # Skip translation (already translated, just evaluate+combine+epub+align)
    python scripts/translate_book.py --project-dir projects/my-book --start-stage evaluate

    # Cost estimation only
    python scripts/translate_book.py --project-dir projects/my-book --cost-only
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.book_splitter import split_book_into_chapters, save_chapters_to_files
from src.chunker import chunk_chapter
from src.combiner import combine_chunks
from src.epub_builder import build_epub
from src.models import Chunk, ChunkStatus, ChunkingConfig, EvaluationConfig
from src.sentence_aligner import align_chapter_chunks
from src.utils.file_io import load_chunk, save_chunk, load_glossary, save_glossary, load_style_guide


# Pipeline stages in order
STAGES = [
    "ingest",
    "split",
    "chunk",
    "translate",
    "evaluate",
    "combine",
    "epub",
    "align",
]


def load_pipeline_state(project_dir: Path) -> dict:
    """Load checkpoint state from pipeline_state.json."""
    state_path = project_dir / "pipeline_state.json"
    if state_path.exists():
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_pipeline_state(project_dir: Path, state: dict):
    """Save checkpoint state to pipeline_state.json."""
    state["updated_at"] = datetime.now().isoformat()
    state_path = project_dir / "pipeline_state.json"
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def parse_chapter_range(spec: str) -> set[str]:
    """Parse a chapter range spec like '1-5' or '3,7,12' into chapter IDs.

    Returns a set of chapter IDs like {'chapter_01', 'chapter_05'}.
    Supports: '1-5', '3,7,12', '1-3,7,10-12'.
    """
    numbers = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            for n in range(int(start), int(end) + 1):
                numbers.add(n)
        else:
            numbers.add(int(part))
    return {f"chapter_{n:02d}" for n in numbers}


def discover_chapters(chunks_dir: Path) -> dict[str, list[Path]]:
    """Discover chapters by scanning chunk JSON files."""
    import re

    chapters: dict[str, list[Path]] = {}
    for chunk_path in sorted(chunks_dir.glob("*_chunk_*.json")):
        match = re.match(r"^(.+)_chunk_\d+\.json$", chunk_path.name)
        if not match:
            continue
        chapter_id = match.group(1)
        chapters.setdefault(chapter_id, []).append(chunk_path)

    for chapter_id in chapters:
        chapters[chapter_id] = sorted(chapters[chapter_id])

    return dict(sorted(chapters.items()))


def stage_ingest(args, project_dir: Path, state: dict) -> dict:
    """Stage 1: Ingest from Gutenberg URL."""
    from bs4 import BeautifulSoup

    # Import ingest functions from script
    scripts_dir = Path(__file__).parent
    sys.path.insert(0, str(scripts_dir))
    from ingest_gutenberg import Converter, fetch_html, find_book_body, _normalize_whitespace

    url = args.url
    if not url:
        # Check if source.txt already exists
        source_path = project_dir / "source.txt"
        if source_path.exists():
            print(f"  source.txt already exists ({source_path.stat().st_size:,} bytes)")
            state["stage_completed"] = "ingest"
            return state
        raise ValueError("--url is required for ingest stage (no existing source.txt)")

    print(f"  Fetching {url} ...")
    html, base_url = fetch_html(url)

    print("  Parsing HTML ...")
    soup = BeautifulSoup(html, "html.parser")
    body = find_book_body(soup)

    images_dir = project_dir / "images"
    images_dir.mkdir(exist_ok=True)

    converter = Converter(
        base_url=base_url,
        images_dir=images_dir,
        download_images=True,
    )
    text = converter.convert(body)
    text = _normalize_whitespace(text)

    out_path = project_dir / "source.txt"
    out_path.write_text(text, encoding="utf-8")

    word_count = len(text.split())
    print(f"  Ingested: {word_count:,} words, {converter._images_downloaded} images")

    state["stage_completed"] = "ingest"
    state["source_words"] = word_count
    state["url"] = url
    return state


def stage_split(args, project_dir: Path, state: dict) -> dict:
    """Stage 2: Split source.txt into chapters."""
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        raise FileNotFoundError(f"source.txt not found in {project_dir}")

    book_text = source_path.read_text(encoding="utf-8")

    pattern_type = getattr(args, "chapter_pattern", "roman") or "roman"
    custom_regex = getattr(args, "custom_regex", None)
    min_size = getattr(args, "min_chapter_size", 100) or 100

    chapters = split_book_into_chapters(
        book_text=book_text,
        pattern_type=pattern_type,
        custom_regex=custom_regex,
        min_chapter_size=min_size,
    )

    chapters_dir = project_dir / "chapters"
    save_chapters_to_files(chapters, str(chapters_dir))

    print(f"  Split into {len(chapters)} chapters")
    for ch in chapters:
        words = len(ch.content.split())
        print(f"    {ch.chapter_title}: {words:,} words")

    state["stage_completed"] = "split"
    state["chapter_count"] = len(chapters)
    return state


def stage_chunk(args, project_dir: Path, state: dict) -> dict:
    """Stage 3: Chunk chapters into translation-sized pieces."""
    chapters_dir = project_dir / "chapters"
    chunks_dir = project_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    chapter_files = sorted(chapters_dir.glob("chapter_*.txt"))
    if not chapter_files:
        raise FileNotFoundError(f"No chapter files in {chapters_dir}")

    config = ChunkingConfig(
        target_size=getattr(args, "chunk_size", 2000) or 2000,
        overlap_paragraphs=getattr(args, "overlap_paragraphs", 1) or 1,
        min_overlap_words=getattr(args, "min_overlap_words", 50) or 50,
    )

    total_chunks = 0
    for chapter_file in chapter_files:
        chapter_text = chapter_file.read_text(encoding="utf-8")
        chapter_id = chapter_file.stem  # e.g., "chapter_01"

        chunks = chunk_chapter(chapter_text, config, chapter_id)
        for chunk in chunks:
            output_file = chunks_dir / f"{chunk.id}.json"
            save_chunk(chunk, output_file)

        total_chunks += len(chunks)
        print(f"    {chapter_id}: {len(chunks)} chunks")

    print(f"  Total: {total_chunks} chunks across {len(chapter_files)} chapters")

    state["stage_completed"] = "chunk"
    state["total_chunks"] = total_chunks
    return state


def stage_translate(args, project_dir: Path, state: dict) -> dict:
    """Stage 4: Translate chunks via API."""
    from src.api_translator import translate_chunk_realtime, estimate_cost

    chunks_dir = project_dir / "chunks"
    chapters = discover_chapters(chunks_dir)

    # Filter to requested chapters
    chapter_filter = None
    if getattr(args, "chapters", None):
        chapter_filter = parse_chapter_range(args.chapters)
        chapters = {k: v for k, v in chapters.items() if k in chapter_filter}
        if not chapters:
            print(f"  No matching chapters found for --chapters {args.chapters}")
            print(f"  Available: {', '.join(sorted(discover_chapters(chunks_dir).keys()))}")
            state["stage_completed"] = "translate"
            return state

    provider = args.provider
    model = args.model

    # Load optional glossary and style guide
    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        glossary = load_glossary(glossary_path)

    style_guide = None
    style_path = project_dir / "style.json"
    if style_path.exists():
        style_guide = load_style_guide(style_path)

    # Collect untranslated chunks
    untranslated = []
    for chapter_id, chunk_paths in chapters.items():
        for cp in chunk_paths:
            chunk = load_chunk(cp)
            if not chunk.has_translation:
                untranslated.append((cp, chunk))

    if not untranslated:
        print("  All chunks already translated!")
        state["stage_completed"] = "translate"
        return state

    total = sum(len(paths) for paths in chapters.values())
    print(f"  {len(untranslated)} of {total} chunks need translation")

    # Cost estimation
    chunks_for_cost = [chunk for _, chunk in untranslated]
    cost_info = estimate_cost(chunks_for_cost, provider, model, glossary=glossary, style_guide=style_guide)
    print(f"  Estimated cost: ${cost_info['cost_usd']:.2f} ({cost_info['input_tokens']:,} input tokens)")

    cost_limit = getattr(args, "cost_limit", 5.0) or 5.0
    if cost_info["cost_usd"] > cost_limit:
        print(f"  WARNING: Estimated cost ${cost_info['cost_usd']:.2f} exceeds --cost-limit ${cost_limit:.2f}")
        response = input("  Continue? [y/N] ").strip().lower()
        if response != "y":
            print("  Aborted.")
            sys.exit(0)

    if getattr(args, "cost_only", False):
        print("  --cost-only: stopping after estimate")
        sys.exit(0)

    # Translate
    project_name = getattr(args, "project_name", project_dir.name) or project_dir.name
    target_lang = getattr(args, "target_lang", "Spanish") or "Spanish"
    source_lang = getattr(args, "source_lang", "English") or "English"

    previous_context = ""
    for i, (chunk_path, chunk) in enumerate(untranslated, 1):
        print(f"  [{i}/{len(untranslated)}] Translating {chunk.id} ...", end=" ", flush=True)
        t0 = time.time()

        translated = translate_chunk_realtime(
            chunk=chunk,
            provider=provider,
            model=model,
            glossary=glossary,
            style_guide=style_guide,
            project_name=project_name,
            source_language=source_lang,
            target_language=target_lang,
            previous_chapter_context=previous_context,
        )

        save_chunk(translated, chunk_path)
        elapsed = time.time() - t0
        print(f"done ({elapsed:.1f}s, {translated.translation_word_count} words)")

        # Use tail of this chunk's source as context for the next chunk
        paragraphs = chunk.source_text.strip().split("\n\n")
        previous_context = "\n\n".join(paragraphs[-2:]) if len(paragraphs) >= 2 else chunk.source_text.strip()

        # Checkpoint after each chunk
        state["last_translated_chunk"] = chunk.id
        state["translated_count"] = i
        save_pipeline_state(project_dir, state)

    # Batch summary
    cost_per_chunk = cost_info["cost_per_chunk_usd"]
    actual_cost = cost_per_chunk * len(untranslated)
    chapter_ids_done = sorted({cid for _, c in untranslated for cid in [c.chapter_id]})
    print(f"\n  Batch complete: {len(untranslated)} chunks across {len(chapter_ids_done)} chapter(s)")
    print(f"  Estimated cost: ${actual_cost:.2f}")

    # If there are remaining untranslated chunks beyond this batch, show remaining estimate
    all_chapters = discover_chapters(project_dir / "chunks")
    remaining = 0
    for ch_id, paths in all_chapters.items():
        for cp in paths:
            c = load_chunk(cp)
            if not c.has_translation:
                remaining += 1
    if remaining > 0:
        print(f"  Remaining: {remaining} untranslated chunks (~${remaining * cost_per_chunk:.2f})")

    state["stage_completed"] = "translate"
    return state


def _filter_chapters(args, chapters: dict) -> dict:
    """Apply --chapters filter if set."""
    if getattr(args, "chapters", None):
        requested = parse_chapter_range(args.chapters)
        return {k: v for k, v in chapters.items() if k in requested}
    return chapters


def stage_evaluate(args, project_dir: Path, state: dict) -> dict:
    """Stage 5: Evaluate all translated chunks."""
    from src.evaluators import run_all_evaluators, aggregate_results

    chunks_dir = project_dir / "chunks"
    chapters = _filter_chapters(args, discover_chapters(chunks_dir))

    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        glossary = load_glossary(glossary_path)

    config = EvaluationConfig(
        enabled_evals=["length", "paragraph", "completeness"],
        fail_on_errors=False,
    )

    total_chunks = 0
    total_passed = 0
    total_issues = 0

    for chapter_id, chunk_paths in chapters.items():
        for chunk_path in chunk_paths:
            chunk = load_chunk(chunk_path)
            if not chunk.has_translation:
                continue

            results = run_all_evaluators(chunk, config, glossary)
            summary = aggregate_results(results)

            total_chunks += 1
            if summary["overall_passed"]:
                total_passed += 1
            total_issues += summary["total_issues"]

    print(f"  Evaluated {total_chunks} chunks: {total_passed} passed, {total_chunks - total_passed} failed")
    print(f"  Total issues: {total_issues}")

    state["stage_completed"] = "evaluate"
    state["eval_passed"] = total_passed
    state["eval_total"] = total_chunks
    return state


def stage_combine(args, project_dir: Path, state: dict) -> dict:
    """Stage 6: Combine translated chunks into chapter files."""
    chunks_dir = project_dir / "chunks"
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)

    chapters = _filter_chapters(args, discover_chapters(chunks_dir))

    for chapter_id, chunk_paths in chapters.items():
        chunks = [load_chunk(cp) for cp in chunk_paths]

        # Skip chapters with untranslated chunks
        if not all(c.has_translation for c in chunks):
            print(f"    {chapter_id}: skipped (not fully translated)")
            continue

        combined = combine_chunks(chunks)

        out_path = chapters_dir / f"{chapter_id}.txt"
        out_path.write_text(combined, encoding="utf-8")
        print(f"    {chapter_id}: {len(combined.split()):,} words")

    print(f"  Combined {len(chapters)} chapters")

    state["stage_completed"] = "combine"
    return state


def stage_epub(args, project_dir: Path, state: dict) -> dict:
    """Stage 7: Build EPUB from combined chapters."""
    project_name = getattr(args, "project_name", project_dir.name) or project_dir.name
    author = getattr(args, "author", "Unknown") or "Unknown"
    target_lang_code = getattr(args, "target_lang_code", "es") or "es"

    epub_path = build_epub(
        project_path=project_dir,
        title=project_name,
        author=author,
        language=target_lang_code,
    )

    print(f"  EPUB written to: {epub_path}")
    state["stage_completed"] = "epub"
    state["epub_path"] = str(epub_path)
    return state


def stage_align(args, project_dir: Path, state: dict) -> dict:
    """Stage 8: Compute sentence alignments for reader mode."""
    chunks_dir = project_dir / "chunks"
    align_dir = project_dir / "alignments"
    align_dir.mkdir(exist_ok=True)

    chapters = discover_chapters(chunks_dir)
    project_name = project_dir.name

    source_lang = getattr(args, "source_lang_code", "en") or "en"
    target_lang = getattr(args, "target_lang_code", "es") or "es"

    for chapter_id, chunk_paths in chapters.items():
        # Check all chunks are translated
        chunks = [load_chunk(cp) for cp in chunk_paths]
        if not all(c.has_translation for c in chunks):
            print(f"    {chapter_id}: skipped (not fully translated)")
            continue

        t0 = time.time()
        result = align_chapter_chunks(
            chunk_paths=[str(p) for p in chunk_paths],
            project_id=project_name,
            chapter_id=chapter_id,
            source_lang=source_lang,
            target_lang=target_lang,
            output_path=str(align_dir / f"{chapter_id}.json"),
        )
        elapsed = time.time() - t0

        print(
            f"    {chapter_id}: {result['es_count']} sentences, "
            f"{result['high_confidence_pct']}% high-confidence ({elapsed:.1f}s)"
        )

    print(f"  Alignments written to: {align_dir}")

    state["stage_completed"] = "align"
    return state


STAGE_FUNCTIONS = {
    "ingest": stage_ingest,
    "split": stage_split,
    "chunk": stage_chunk,
    "translate": stage_translate,
    "evaluate": stage_evaluate,
    "combine": stage_combine,
    "epub": stage_epub,
    "align": stage_align,
}


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end book translation pipeline: Gutenberg URL to EPUB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Source
    parser.add_argument("--url", help="Gutenberg HTML URL to ingest")
    parser.add_argument(
        "--project-dir",
        help="Existing project directory (skip ingest if source.txt exists)",
    )
    parser.add_argument("--project-name", help="Project/book title")
    parser.add_argument("--author", default="Unknown", help="Author name for EPUB metadata")

    # Languages
    parser.add_argument("--source-lang", default="English", help="Source language name (default: English)")
    parser.add_argument("--target-lang", default="Spanish", help="Target language name (default: Spanish)")
    parser.add_argument("--source-lang-code", default="en", help="Source language code (default: en)")
    parser.add_argument("--target-lang-code", default="es", help="Target language code (default: es)")

    # Translation API
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (default: anthropic)")
    parser.add_argument("--model", default="claude-sonnet-4-20250514",
                        help="Model identifier (default: claude-sonnet-4-20250514)")
    parser.add_argument("--cost-limit", type=float, default=5.0,
                        help="Cost limit in USD, prompts if exceeded (default: $5.00)")
    parser.add_argument("--cost-only", action="store_true",
                        help="Only estimate cost, don't translate")

    # Chapter detection
    parser.add_argument("--chapter-pattern", default="roman",
                        choices=["roman", "numeric", "custom"],
                        help="Chapter detection pattern (default: roman)")
    parser.add_argument("--custom-regex", help="Custom regex for chapter detection")
    parser.add_argument("--min-chapter-size", type=int, default=100,
                        help="Minimum chapter size in characters (default: 100)")

    # Chunking
    parser.add_argument("--chunk-size", type=int, default=2000,
                        help="Target words per chunk (default: 2000)")
    parser.add_argument("--overlap-paragraphs", type=int, default=1,
                        help="Paragraphs of overlap between chunks (default: 1)")
    parser.add_argument("--min-overlap-words", type=int, default=50,
                        help="Minimum overlap words (default: 50)")

    # Setup (style guide + glossary)
    parser.add_argument("--generate-style-guide", action="store_true",
                        help="Generate style guide from fixed questions before translating (no LLM needed)")
    parser.add_argument("--bootstrap-glossary", action="store_true",
                        help="Extract glossary candidates and bootstrap via LLM before translating")

    # Pipeline control
    parser.add_argument("--chapters",
                        help="Translate only these chapters (e.g., '1-5' or '3,7,12')")
    parser.add_argument("--start-stage", choices=STAGES,
                        help="Start from this stage (skip earlier stages)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint")

    args = parser.parse_args()

    # Determine project directory
    if args.project_dir:
        project_dir = Path(args.project_dir)
    elif args.url:
        # Derive project name from URL or --project-name
        name = args.project_name
        if not name:
            # Use last path segment of URL, cleaned up
            from urllib.parse import urlparse
            parsed = urlparse(args.url)
            name = Path(parsed.path).stem or "book"
            name = name.replace("-h", "").replace("_h", "")
        project_dir = Path("projects") / name
    else:
        parser.error("Either --url or --project-dir is required")

    project_dir.mkdir(parents=True, exist_ok=True)
    print(f"Project directory: {project_dir}")

    # Load or initialize state
    state = load_pipeline_state(project_dir)

    # Determine starting stage
    if args.resume and state.get("stage_completed"):
        completed = state["stage_completed"]
        if completed in STAGES:
            start_idx = STAGES.index(completed) + 1
            if start_idx >= len(STAGES):
                print(f"Pipeline already complete (last stage: {completed})")
                return
            start_stage = STAGES[start_idx]
            print(f"Resuming from: {start_stage} (last completed: {completed})")
        else:
            start_stage = STAGES[0]
    elif args.start_stage:
        start_stage = args.start_stage
        print(f"Starting from: {start_stage}")
    else:
        start_stage = STAGES[0]

    start_idx = STAGES.index(start_stage)

    # Clear stale error state from previous runs
    state.pop("last_error", None)
    state.pop("failed_stage", None)

    # --generate-style-guide: create style.json from fixed questions if it doesn't exist
    if getattr(args, "generate_style_guide", False):
        style_path = project_dir / "style.json"
        if style_path.exists():
            print("\nStyle guide already exists, skipping generation.")
        else:
            print("\n" + "=" * 60)
            print("SETUP: Generate Style Guide (fixed questions, no LLM)")
            print("=" * 60)
            from src.style_guide_wizard import (
                load_fixed_questions, answers_to_style_guide_fallback,
                save_style_guide_json, load_source_sample,
            )
            questions = load_fixed_questions()
            # Use default answers for all questions
            answers = {}
            for q in questions:
                answers[q["id"]] = q.get("default", 0)
            content = answers_to_style_guide_fallback(questions, answers)
            save_style_guide_json(content, style_path)
            print(f"  Style guide saved to {style_path}")
            print(f"  ({len(content)} chars from {len(questions)} questions with default answers)")

    # --bootstrap-glossary: extract candidates and bootstrap via LLM
    if getattr(args, "bootstrap_glossary", False):
        glossary_path = project_dir / "glossary.json"
        if glossary_path.exists():
            print("\nGlossary already exists, skipping bootstrap.")
        else:
            print("\n" + "=" * 60)
            print("SETUP: Bootstrap Glossary")
            print("=" * 60)
            from src.style_guide_wizard import load_source_sample
            from src.glossary_bootstrap import (
                build_glossary_prompt, parse_glossary_response,
                glossary_terms_from_proposals, proposals_to_glossary,
            )

            # Need chunks to exist for extraction — check if we've chunked yet
            chunks_dir = project_dir / "chunks"
            source_path = project_dir / "source.txt"
            if not source_path.exists() and not chunks_dir.exists():
                print("  No source text found. Run ingest/split/chunk stages first.")
            else:
                # Extract candidates using the extraction script
                if source_path.exists():
                    source_text = source_path.read_text(encoding="utf-8")
                else:
                    source_text = load_source_sample(project_dir, max_words=50000)

                sys.path.insert(0, str(Path(__file__).parent))
                from extract_glossary_candidates import extract_candidates
                report = extract_candidates(source_text, verbose=True)
                candidates = [c.model_dump() for c in report.candidates[:200]]
                print(f"  {len(candidates)} candidates extracted")

                if candidates:
                    # Load style guide for context if available
                    style_content = ""
                    style_path = project_dir / "style.json"
                    if style_path.exists():
                        sg = load_style_guide(style_path)
                        style_content = sg.content

                    sample = load_source_sample(project_dir)
                    target_lang = getattr(args, "target_lang", "Spanish") or "Spanish"

                    prompt = build_glossary_prompt(candidates, sample, style_content, target_lang)
                    print(f"  Calling LLM for glossary proposals ({len(candidates)} candidates)...")

                    from src.api_translator import call_llm
                    response = call_llm(
                        prompt,
                        provider=args.provider,
                        model=args.model,
                        max_tokens=8192,
                    )

                    proposals = parse_glossary_response(response)
                    terms = glossary_terms_from_proposals(proposals)
                    glossary = proposals_to_glossary(terms)
                    save_glossary(glossary, glossary_path)
                    print(f"  Glossary saved: {len(terms)} terms to {glossary_path}")

    # Run pipeline stages
    t_total = time.time()
    for stage_name in STAGES[start_idx:]:
        print(f"\n{'='*60}")
        print(f"Stage: {stage_name.upper()}")
        print(f"{'='*60}")

        stage_fn = STAGE_FUNCTIONS[stage_name]
        t0 = time.time()

        try:
            state = stage_fn(args, project_dir, state)
        except Exception as e:
            print(f"\n  ERROR in {stage_name}: {e}")
            state["last_error"] = str(e)
            state["failed_stage"] = stage_name
            save_pipeline_state(project_dir, state)
            print(f"\n  Pipeline stopped. Resume with:")
            print(f"    python scripts/translate_book.py --project-dir {project_dir} --resume")
            sys.exit(1)

        elapsed = time.time() - t0
        print(f"  [{stage_name}] completed in {elapsed:.1f}s")
        save_pipeline_state(project_dir, state)

    total_elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"Pipeline complete! Total time: {total_elapsed:.1f}s")
    print(f"{'='*60}")

    if state.get("epub_path"):
        print(f"\n  EPUB: {state['epub_path']}")
    print(f"  Project: {project_dir}")
    print(f"  Alignments: {project_dir / 'alignments'}")


if __name__ == "__main__":
    main()
