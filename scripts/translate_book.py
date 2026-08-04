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
from src.epub_builder import build_epub_from_chunks
from src.harness.state import emit_harness_result
from src.api_translator import DEFAULT_MODEL
from src.models import Chunk, ChunkStatus, ChunkingConfig
from src.sentence_aligner import align_chapter_chunks
from src.utils.file_io import load_chunk, save_chunk, load_glossary, save_glossary, load_style_guide
from src.utils.source_text import load_chapter_source_text


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
    "footnotes",
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
    from ingest_gutenberg import (
        Converter,
        fetch_html,
        find_book_body,
        _normalize_whitespace,
        build_chapter_report,
        suggest_split_pattern,
    )

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

    # Footnotes: detect always; import as survivable [FOOTNOTE:N] tokens (+
    # footnotes.json) or drop cleanly, per --footnotes. Runs before the text
    # conversion that would otherwise flatten the linkage away.
    from src.footnote_import import (
        find_footnotes,
        apply_import,
        apply_drop,
        records_from_matches,
        write_footnotes_sidecar,
    )
    fn_mode = getattr(args, "footnotes", "drop") or "drop"
    fn_matches = find_footnotes(body)
    if fn_matches:
        if fn_mode == "import":
            apply_import(fn_matches)
            write_footnotes_sidecar(project_dir, records_from_matches(fn_matches))
        else:
            apply_drop(fn_matches)

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
    fn_note = ""
    if fn_matches:
        fn_note = f", {len(fn_matches)} footnotes {'imported' if fn_mode == 'import' else 'dropped'}"
    print(f"  Ingested: {word_count:,} words, {converter._images_downloaded} images{fn_note}")

    state["stage_completed"] = "ingest"
    state["source_words"] = word_count
    state["footnote_count"] = len(fn_matches)
    state["footnote_mode"] = fn_mode
    state["url"] = url
    # Heading-derived hints the agent can relay (parity with the web GUI's
    # Gutenberg report): a per-chapter report and an auto-suggested split
    # pattern, both computed from the HTML headings the Converter tracked.
    state["chapter_report"] = build_chapter_report(converter.chapters, word_count)
    state["suggested_pattern"] = suggest_split_pattern(converter.chapters)
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

    dropped: list = []
    chapters = split_book_into_chapters(
        book_text=book_text,
        pattern_type=pattern_type,
        custom_regex=custom_regex,
        min_chapter_size=min_size,
        front_matter_titles=getattr(args, "front_matter_titles", None) or None,
        back_matter_titles=getattr(args, "back_matter_titles", None) or None,
        auto_detect_front_matter=getattr(args, "auto_detect_front_matter", True),
        auto_detect_back_matter=getattr(args, "auto_detect_back_matter", True),
        auto_strip_boilerplate=getattr(args, "auto_strip_boilerplate", True),
        collect_dropped=dropped,
    )

    chapters_dir = project_dir / "chapters"
    save_chapters_to_files(chapters, str(chapters_dir))

    print(f"  Split into {len(chapters)} chapters")
    for ch in chapters:
        words = len(ch.content.split())
        print(f"    {ch.chapter_title}: {words:,} words")
    for d in dropped:
        print(f"    [stripped boilerplate] {d.get('label')}")

    state["stage_completed"] = "split"
    state["chapter_count"] = len(chapters)
    state["dropped"] = dropped
    return state


def stage_chunk(args, project_dir: Path, state: dict) -> dict:
    """Stage 3: Chunk chapters into translation-sized pieces."""
    chapters_dir = project_dir / "chapters"
    chunks_dir = project_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)

    chapter_files = sorted(chapters_dir.glob("chapter_*.txt"))
    if not chapter_files:
        raise FileNotFoundError(f"No chapter files in {chapters_dir}")

    default_target = getattr(args, "chunk_size", 2000) or 2000
    # Chunk overlap is disabled: the overlap/combine de-dup path is known-broken
    # (see docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md #20). Honor an explicit
    # 0 — note `x or 0` maps both 0 and None to 0; never coerce back to a nonzero
    # default the way the old `or 1`/`or 50` did.
    overlap_paragraphs = getattr(args, "overlap_paragraphs", 0) or 0
    min_overlap_words = getattr(args, "min_overlap_words", 0) or 0

    # Optional per-chapter target sizes (e.g. from difficulty scoring). Chapters
    # absent from the map fall back to default_target. Bounds scale with each
    # target via ChunkingConfig.from_target so the size actually bites.
    sizes_map: dict = {}
    sizes_path = getattr(args, "chunk_sizes", None)
    if isinstance(sizes_path, (str, Path)) and sizes_path:
        sizes_map = json.loads(Path(sizes_path).read_text(encoding="utf-8"))

    total_chunks = 0
    refused: list[str] = []
    for chapter_file in chapter_files:
        chapter_id = chapter_file.stem  # e.g., "chapter_01"
        # NEVER read chapters/*.txt raw here. That file is dual-purpose — the split
        # stage writes the English there and `combine` overwrites it with the
        # translation — so a re-chunk after a combine would store TRANSLATED prose as
        # source_text, and the pipeline would then translate Spanish->Spanish with
        # every guard passing (the echo guard compares against source_text, which
        # would also be Spanish). load_chapter_source_text applies the chunks-first
        # precedence that keeps this English; web_ui does the same for this reason.
        chapter_text, _mtime, kind = load_chapter_source_text(project_dir, chapter_id)
        if not chapter_text.strip():
            print(f"    WARNING: {chapter_id}: no source text found — skipped")
            continue

        # The precedence above is necessary but not sufficient. When chunk JSONs EXIST
        # but every one of them is corrupt or carries an empty source_text, the loader
        # falls through to chapters/*.txt — correct for the read-only callers (the
        # difficulty scorer, the web reader), fatal here, because post-combine that
        # file is the Spanish translation and we would write it back as source_text.
        # Existing chunks are the only trustworthy source at this stage: if they are
        # unreadable, refuse the chapter rather than re-chunk a translation.
        existing_chunks = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
        if existing_chunks and kind != "chunks":
            print(
                f"    ERROR: {chapter_id}: {len(existing_chunks)} chunk file(s) exist but "
                f"none yielded source_text (corrupt or empty), and chapters/{chapter_id}.txt "
                f"may already hold the TRANSLATION — refusing to re-chunk it as source. "
                f"Repair or delete the chunk files, then re-run."
            )
            refused.append(chapter_id)
            continue

        target = int(sizes_map.get(chapter_id, default_target))
        config = ChunkingConfig.from_target(
            target,
            overlap_paragraphs=overlap_paragraphs,
            min_overlap_words=min_overlap_words,
        )

        chunks = chunk_chapter(chapter_text, config, chapter_id)
        for chunk in chunks:
            output_file = chunks_dir / f"{chunk.id}.json"
            save_chunk(chunk, output_file)

        total_chunks += len(chunks)
        print(f"    {chapter_id}: {len(chunks)} chunks (target {target}w)")

    if refused:
        # Soft-continue would still mark stage_completed="chunk" and exit 0 with
        # total_chunks==0 (or a partial book) — callers/harness would treat a Spanish
        # guard trip as success. Raise so main() records failed_stage and exits 1.
        raise RuntimeError(
            f"{len(refused)} chapter(s) refused (corrupt/empty chunk sources; "
            f"would not re-chunk possible translation as source): {', '.join(refused)}. "
            f"Repair or delete the chunk files, then re-run."
        )

    print(f"  Total: {total_chunks} chunks across {len(chapter_files)} chapters")

    state["stage_completed"] = "chunk"
    state["total_chunks"] = total_chunks
    return state


def stage_translate(args, project_dir: Path, state: dict) -> dict:
    """Stage 4: Translate chunks via API."""
    from src.api_translator import (
        _book_has_images,
        estimate_cost,
        last_cache_usage,
        summarize_chunk_features,
        translate_chunk_realtime,
    )

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
            early = {
                "stage": "translate",
                "translated": 0,
                "note": f"no matching chapters for --chapters {args.chapters}",
            }
            state["_harness_translate_result"] = early
            emit_harness_result(early)
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

    total = sum(len(paths) for paths in chapters.values())

    if not untranslated:
        print("  All chunks already translated!")
        early = {
            "stage": "translate",
            "translated": 0,
            "total_chunks_in_scope": total,
            "note": "all chunks already translated",
        }
        state["_harness_translate_result"] = early
        emit_harness_result(early)
        state["stage_completed"] = "translate"
        return state

    print(f"  {len(untranslated)} of {total} chunks need translation")

    # Resolve cache-prefix opt-ins (tri-state CLI args; None = auto).
    project_name = getattr(args, "project_name", project_dir.name) or project_dir.name
    target_lang = getattr(args, "target_lang", "Spanish") or "Spanish"
    source_lang = getattr(args, "source_lang", "English") or "English"
    chunks_for_cost = [chunk for _, chunk in untranslated]
    book_has_images = _book_has_images(chunks_for_cost)
    features = summarize_chunk_features(chunks_for_cost, target_language=target_lang)

    always_dialogue_arg = getattr(args, "always_dialogue", None)
    always_images_arg = getattr(args, "always_images", None)
    # Absent means auto for both: on when the book needs it, so a mixed book gets
    # a stable (cacheable) prefix and a uniform one pays nothing for the block it
    # would never vary. Matches flow.translate_prepare's resolution.
    always_include_dialogue = (
        bool(always_dialogue_arg) if always_dialogue_arg is not None
        else features["dialogue"] > 0
    )
    always_include_image_instructions = (
        bool(always_images_arg) if always_images_arg is not None else book_has_images
    )

    print(
        f"  {features['total']} chunks: {features['dialogue']} with dialogue, "
        f"{features['images']} with images | dialogue-block-all: "
        f"{'on' if always_include_dialogue else 'off'}"
        f"{' (auto)' if always_dialogue_arg is None else ''}"
        f", image-instructions: "
        f"{'on' if always_include_image_instructions else 'off'}"
        f"{' (auto)' if always_images_arg is None else ''}"
    )

    # Cost estimation
    cost_info = estimate_cost(
        chunks_for_cost,
        provider,
        model,
        glossary=glossary,
        style_guide=style_guide,
        always_include_dialogue=always_include_dialogue,
        always_include_image_instructions=always_include_image_instructions,
        target_language=target_lang,
    )

    if getattr(args, "cost_only", False):
        # Estimator path (the harness `chunk` / `cost` commands). The user may
        # translate via the metered API OR the zero-API-cost subagent backend, and
        # the backend isn't chosen yet at this point in the flow — so keep the dollar
        # figure explicitly conditional and never present it as *the* cost
        # (friction-log #9).
        print(f"  Size: {len(untranslated)} chunk(s), ~{cost_info['input_tokens']:,} input tokens")
        print(f"  If translated via the metered API: ~${cost_info['cost_usd']:.2f} ({provider}/{model})")
        print("  Subagent backend uses your subscription (no API $)")
        print("  --cost-only: stopping after estimate")
        emit_harness_result({
            "stage": "cost-estimate",
            "chunks_needing_translation": len(untranslated),
            "total_chunks_in_scope": total,
            "input_tokens": cost_info["input_tokens"],
            "api_cost_usd": round(cost_info["cost_usd"], 2),
            "provider": provider,
            "model": model,
            "cost_only": True,
            "dialogue_chunk_count": features["dialogue"],
            "image_chunk_count": features["images"],
            "always_include_dialogue": always_include_dialogue,
            "always_include_image_instructions": always_include_image_instructions,
        })
        sys.exit(0)

    # Real (paid) API translate run: the dollar figure IS the spend.
    print(
        f"  Estimated cost: ${cost_info['cost_usd']:.2f} "
        f"for {len(untranslated)} chunk(s) with {provider}/{model} "
        f"({cost_info['input_tokens']:,} input tokens)"
    )

    if not getattr(args, "yes", False):
        if sys.stdin.isatty():
            response = input(f"  Spend ~${cost_info['cost_usd']:.2f} translating {len(untranslated)} chunk(s)? [y/N] ")
            if response.strip().lower() != "y":
                print("  Aborted.")
                sys.exit(0)
        else:
            print("  Cost estimate requires approval; re-run with --yes once you've approved the estimate.")
            sys.exit(1)

    # Translate
    previous_context = ""
    total_cache_read = 0
    total_cache_created = 0
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
            enable_thinking=getattr(args, "thinking", None),
            always_include_dialogue=always_include_dialogue,
            always_include_image_instructions=always_include_image_instructions,
        )

        save_chunk(translated, chunk_path)

        # Footnote-token survival guard (mirrors the image-placeholder check):
        # a marker dropped/duplicated by the model would misplace a footnote, so
        # surface it now rather than silently at build time.
        from src.utils.text_utils import footnote_tokens_preserved
        if not footnote_tokens_preserved(chunk.source_text, translated.translated_text or ""):
            print(
                f"\n    WARNING: [FOOTNOTE:N] tokens not preserved in {chunk.id}; "
                "review before running the footnotes stage.",
                end="",
            )

        elapsed = time.time() - t0
        usage = last_cache_usage() or {}
        total_cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
        total_cache_created += int(usage.get("cache_creation_input_tokens", 0) or 0)
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
    print(
        f"  cache: {total_cache_read:,} read / {total_cache_created:,} created input tokens"
    )

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

    # Stash for stage_align: the API auto-chain continues through align, and
    # _run_script keeps the *last* HARNESS_RESULT. Align re-emits this payload
    # with coverage_warnings so agents reading last_output.json see drops.
    translate_result = {
        "stage": "translate",
        "translated": len(untranslated),
        "chapters_done": chapter_ids_done,
        "estimated_cost_usd": round(actual_cost, 2),
        "remaining_untranslated": remaining,
    }
    state["_harness_translate_result"] = translate_result
    emit_harness_result(translate_result)

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
    from web_ui.evaluations import (
        _load_project_blacklist,
        _load_project_glossary,
        evaluate_and_persist_chunk,
    )

    chunks_dir = project_dir / "chunks"
    chapters = _filter_chapters(args, discover_chapters(chunks_dir))

    glossary = _load_project_glossary(project_dir)
    blacklist = _load_project_blacklist(project_dir)

    total_chunks = 0
    total_passed = 0
    total_issues = 0

    for chapter_id, chunk_paths in chapters.items():
        for chunk_path in chunk_paths:
            chunk = load_chunk(chunk_path)
            if not chunk.has_translation:
                continue

            persisted = evaluate_and_persist_chunk(
                project_dir,
                chunk,
                glossary=glossary,
                blacklist=blacklist,
            )
            summary = persisted["aggregated"]

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
    """Stage 7: Build EPUB from fully translated chunks."""
    project_name = getattr(args, "project_name", project_dir.name) or project_dir.name
    author = getattr(args, "author", "Unknown") or "Unknown"
    target_lang_code = getattr(args, "target_lang_code", "es") or "es"
    chapters = parse_chapter_range(args.chapters) if getattr(args, "chapters", None) else None

    result = build_epub_from_chunks(
        project_path=project_dir,
        title=project_name,
        author=author,
        language=target_lang_code,
        chapters=chapters,
    )

    print(f"  EPUB written to: {result.path}")
    print(f"  Included translated chapters ({len(result.included)}): {result.included}")
    if result.skipped:
        print(f"  Skipped untranslated/partial chapters ({len(result.skipped)}): {result.skipped}")
    state["stage_completed"] = "epub"
    state["epub_path"] = str(result.path)
    state["epub_included_chapters"] = result.included
    state["epub_skipped_chapters"] = result.skipped
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

    # Flattened across chapters so the final HARNESS_RESULT (and thus
    # last_output.json after harness `translate`) carries every drop.
    coverage_warnings: list[dict] = []

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
        # Source runs with no translation at all. Nothing else in the pipeline sees
        # these — a dropped paragraph leaves the length ratio, the paragraph counts
        # and the confidence score all looking normal.
        for gap in result.get("gaps") or []:
            coverage_warnings.append({"chapter_id": chapter_id, **gap})
            print(
                f"      WARNING: {gap.get('chunk_id', chapter_id)} {gap['position']} gap: "
                f"{gap['sentences']} sentence(s) / {gap['chars']} chars untranslated "
                f"(EN {gap['en_start']}-{gap['en_end']}): {gap['preview']}"
            )

    print(f"  Alignments written to: {align_dir}")

    # Re-emit as the final sentinel for the API auto-chain. _run_script keeps the
    # last HARNESS_RESULT, so agents reading last_output.json after `translate`
    # see coverage_warnings alongside the translate fields (when present).
    payload = dict(state.get("_harness_translate_result") or {"stage": "align"})
    payload["coverage_warnings"] = coverage_warnings
    if coverage_warnings:
        payload["instructions"] = (
            f"COVERAGE WARNING: {len(coverage_warnings)} source run(s) have no "
            "translation at all — the translator dropped prose. Report every entry "
            "in coverage_warnings (chapter, chunk, position, sentences, chars) to "
            "the user and re-translate the affected chunks before continuing."
        )
    emit_harness_result(payload)

    state["stage_completed"] = "align"
    return state


def stage_footnotes(args, project_dir: Path, state: dict) -> dict:
    """Stage 9: Convert imported [FOOTNOTE:N] tokens into reader footnotes.

    No-op unless ingest ran with ``--footnotes import`` (footnotes.json present).
    Runs after align so aligned sentences exist; for each chapter it locates each
    surviving token, writes a ``type:"footnote"`` annotation anchored to the
    preceding word, strips the tokens from the stored translation + alignment,
    then rebuilds the EPUB so the existing endnote machinery embeds them.
    """
    import json as _json
    from src.footnote_import import (
        load_footnotes_sidecar,
        convert_chapter_footnotes,
        write_footnote_annotations,
    )
    from src.utils.text_utils import footnote_token_numbers, strip_footnote_tokens

    notes = load_footnotes_sidecar(project_dir)
    if not notes:
        print("  No imported footnotes (footnotes.json absent) — skipping.")
        state["stage_completed"] = "footnotes"
        return state

    bodies = {
        n["number"]: (n.get("translated_body") or n.get("source_body") or "").strip()
        for n in notes
    }
    missing = [n["number"] for n in notes if not (n.get("translated_body") or "").strip()]
    if missing:
        print(f"  NOTE: {len(missing)} footnote(s) not yet translated "
              "(run scripts/translate_footnotes.py); using source text for those.")

    chunks_dir = project_dir / "chunks"
    align_dir = project_dir / "alignments"
    chapters_dir = project_dir / "chapters"
    chapters = discover_chapters(chunks_dir)

    total_written = 0
    changed: list[str] = []
    for chapter_id, chunk_paths in chapters.items():
        chunks = [load_chunk(cp) for cp in chunk_paths]
        if not all(c.has_translation for c in chunks):
            continue
        combined = combine_chunks(chunks)
        if "[FOOTNOTE:" not in combined:
            # Still report if the chapter *source* had tokens that translation lost.
            src_path = chapters_dir / f"{chapter_id}.txt"
            if src_path.exists():
                src_nums = footnote_token_numbers(src_path.read_text(encoding="utf-8"))
                if src_nums:
                    print(
                        f"    {chapter_id}: 0 written "
                        f"({len(src_nums)} expected from source, missing: "
                        f"{sorted(set(src_nums))})"
                    )
            continue

        # Clean the alignment's es sentences so both this conversion and the
        # later endnote build match the token-free chapter text.
        es_map: dict = {}
        align_path = align_dir / f"{chapter_id}.json"
        align_data = None
        if align_path.exists():
            align_data = _json.loads(align_path.read_text(encoding="utf-8"))
            for a in align_data.get("alignments", []):
                if "es" in a:
                    a["es"] = strip_footnote_tokens(a["es"])[0]
                if "es_idx" in a and "es" in a:
                    es_map[a["es_idx"]] = a["es"]
        else:
            print(f"    {chapter_id}: no alignment file — run the align stage first; skipping")
            continue

        surviving = footnote_token_numbers(combined)
        src_path = chapters_dir / f"{chapter_id}.txt"
        expected = (
            footnote_token_numbers(src_path.read_text(encoding="utf-8"))
            if src_path.exists()
            else surviving
        )

        _clean, records = convert_chapter_footnotes(
            chapter_id, project_dir.name, combined, es_map, bodies,
        )
        total_written += write_footnote_annotations(project_dir, chapter_id, records)
        changed.append(chapter_id)

        align_path.write_text(_json.dumps(align_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Strip tokens from the stored translation so the rebuilt EPUB (which
        # re-combines from chunks) is token-free and consistent with alignment.
        for c, cp in zip(chunks, chunk_paths):
            if c.translated_text and "[FOOTNOTE:" in c.translated_text:
                c.translated_text = strip_footnote_tokens(c.translated_text)[0]
                save_chunk(c, cp)

        written_nums = sorted({r.get("fn_number") for r in records if r.get("fn_number") is not None})
        missing_nums = sorted(set(expected) - set(surviving))
        if missing_nums or len(records) != len(surviving):
            print(
                f"    {chapter_id}: {len(records)} written "
                f"({len(expected)} expected from source, "
                f"{len(surviving)} surviving in translation"
                + (f", missing: {missing_nums}" if missing_nums else "")
                + (f", wrote: {written_nums}" if written_nums else "")
                + ")"
            )
        else:
            print(f"    {chapter_id}: {len(records)} footnote(s)")

    print(f"  Wrote {total_written} footnote annotation(s) across {len(changed)} chapter(s)")

    if changed:
        project_name = getattr(args, "project_name", project_dir.name) or project_dir.name
        author = getattr(args, "author", "Unknown") or "Unknown"
        target_lang_code = getattr(args, "target_lang_code", "es") or "es"
        result = build_epub_from_chunks(
            project_path=project_dir,
            title=project_name,
            author=author,
            language=target_lang_code,
        )
        print(f"  Rebuilt EPUB with footnotes: {result.path}")
        state["epub_path"] = str(result.path)

    state["stage_completed"] = "footnotes"
    state["footnotes_written"] = total_written
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
    "footnotes": stage_footnotes,
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
    parser.add_argument(
        "--footnotes",
        choices=["import", "drop"],
        default="drop",
        help="Gutenberg footnote handling at ingest: 'import' keeps them as "
             "translatable reader footnotes; 'drop' (default) removes them.",
    )

    # Languages
    parser.add_argument("--source-lang", default="English", help="Source language name (default: English)")
    parser.add_argument("--target-lang", default="Spanish", help="Target language name (default: Spanish)")
    parser.add_argument("--source-lang-code", default="en", help="Source language code (default: en)")
    parser.add_argument("--target-lang-code", default="es", help="Target language code (default: es)")

    # Translation API
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="API provider (default: anthropic)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Model identifier (default: {DEFAULT_MODEL})")
    parser.add_argument("--cost-only", action="store_true",
                        help="Estimate cost and exit without translating (never spends, never prompts)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive cost confirmation; use only after you've already reviewed the estimate")
    parser.add_argument("--thinking", dest="thinking",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Enable/disable extended thinking for the translate stage "
                             "(--thinking / --no-thinking). Absent falls back to the "
                             "TRANSLATE_THINKING env default (off). Only thinking-capable "
                             "models honor it.")
    parser.add_argument("--always-dialogue", dest="always_dialogue",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Put the DIALOGUE FORMATTING block on every chunk so it "
                             "caches in the fixed prompt prefix (--always-dialogue / "
                             "--no-always-dialogue). Absent auto-enables when any "
                             "in-scope chunk has dialogue.")
    parser.add_argument("--always-images", dest="always_images",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Put the image-placeholder instruction on every chunk "
                             "(--always-images / --no-always-images). Absent auto-enables "
                             "when any in-scope chunk has [IMAGE:...] placeholders.")

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
    parser.add_argument("--chunk-sizes", default=None,
                        help="Path to a JSON map {chapter_id: target_size} for per-chapter "
                             "chunk sizing; chapters absent from the map fall back to "
                             "--chunk-size (default: none, uniform sizing)")
    parser.add_argument("--overlap-paragraphs", type=int, default=0,
                        help="Paragraphs of overlap between chunks (default: 0; overlap is "
                             "disabled — the overlap/combine de-dup path is known-broken, see "
                             "TRANSLATE_HARNESS_FRICTION_LOG_4 #20)")
    parser.add_argument("--min-overlap-words", type=int, default=0,
                        help="Minimum overlap words (default: 0; overlap disabled)")

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
                        call_type="glossary",
                    )

                    proposals = parse_glossary_response(response)
                    terms = glossary_terms_from_proposals(proposals)
                    glossary = proposals_to_glossary(terms)
                    save_glossary(glossary, glossary_path)
                    print(f"  Glossary saved: {len(terms)} terms to {glossary_path}")

    # Run pipeline stages
    t_total = time.time()
    cost_only = bool(getattr(args, "cost_only", False))
    for stage_name in STAGES[start_idx:]:
        # --cost-only implements the estimate by entering the translate stage and
        # bailing out before a single API call, so it would otherwise announce
        # itself as "Stage: TRANSLATE" and read as if money were about to move
        # (friction-log #10). Name the stage after what it actually does.
        banner = "COST-ESTIMATE" if cost_only and stage_name == "translate" else stage_name.upper()
        print(f"\n{'='*60}")
        print(f"Stage: {banner}")
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
