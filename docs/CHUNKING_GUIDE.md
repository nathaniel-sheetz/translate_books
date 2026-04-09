# Chunking Guide

## Overview

After splitting a book into chapters, each chapter is divided into translation-sized **chunks**. Chunks are the unit of work sent to the LLM for translation. The chunker uses a three-phase algorithm to produce evenly-sized chunks that split at natural boundaries in the text.

Paragraphs are the atomic unit -- the chunker never splits a paragraph across two chunks.

## How It Works

### Phase 1: Plan

The chunker calculates how many chunks a chapter needs based on word count and configuration:

- If the chapter fits within `max_chunk_size`, it stays as **one chunk** (no unnecessary splitting)
- Otherwise, the ideal count is `round(total_words / target_size)`, clamped between `ceil(total / max)` and `floor(total / min)`

Examples with default settings (target=2000, min=500, max=3000):

| Chapter words | Chunks | Words per chunk |
|---------------|--------|-----------------|
| 1800 | 1 | 1800 |
| 2500 | 1 | 2500 |
| 3500 | 2 | ~1750 |
| 5000 | 2 | ~2500 |
| 7000 | 4 | ~1750 |

### Phase 2: Score

Every paragraph boundary gets a quality score from 0.0 (bad split point) to 1.0 (good split point). The scoring considers:

**Penalties (avoid splitting here):**

- **Continuation words** (-0.3): Next paragraph starts with "And", "But", "However", "Then", "So", "Yet", "Still", "Meanwhile", "Furthermore", "Nevertheless", etc. Also checks two-word connectors like "after that", "and then", "but then" in the first few words.
- **Mid-dialogue** (-0.25): Both the current and next paragraph are dialogue. Detected by opening quote characters (`"`, curly quotes, guillemets) or the presence of attribution verbs ("said", "replied", "asked", "cried", "whispered", etc.) alongside quotation marks.

**Bonuses (prefer splitting here):**

- **Scene break** (+0.4): Next or current paragraph is a scene-break marker like `***`, `---`, `* * *`, or similar.
- **End of dialogue** (+0.15): Current paragraph is dialogue but the next is not -- the conversation just ended.
- **Narrative finality** (+0.15): Current paragraph's last sentence is short (under 15 words) and the paragraph is not dialogue. Short closing sentences often signal a topic shift.
- **Long paragraph** (+0.1): Current paragraph is over 150 words. Long paragraphs tend to be self-contained narrative blocks.

### Phase 3: Optimize

Given the target chunk count and the scored boundaries, a dynamic programming solver finds the split points that minimize:

```
cost = size_deviation + split_quality_weight * split_penalty
```

Where `size_deviation` penalizes uneven chunks (squared relative deviation from ideal size) and `split_penalty` penalizes bad boundaries. This means the algorithm will tolerate slightly uneven chunk sizes to avoid splitting in the middle of a dialogue or right before "However...".

## Configuration

Chunking is configured via `ChunkingConfig` in the project config or the web UI:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target_size` | 2000 | Target words per chunk |
| `min_chunk_size` | 500 | Minimum words per chunk |
| `max_chunk_size` | 3000 | Maximum words per chunk |
| `overlap_paragraphs` | 0 | Minimum paragraphs of overlap between chunks |
| `min_overlap_words` | 0 | Minimum words in overlap region |
| `split_quality_weight` | 0.5 | Balance between even sizing (0.0) and good split points (higher). Range: 0.0 - 2.0 |

### In project config JSON

```json
{
  "chunking": {
    "target_size": 2000,
    "max_chunk_size": 3000,
    "overlap_paragraphs": 2,
    "min_overlap_words": 100,
    "split_quality_weight": 0.5
  }
}
```

### Tuning `split_quality_weight`

- **0.0**: Pure even splitting. Ignores content entirely, just balances word counts.
- **0.5** (default): Moderate content awareness. Will shift a split point by a few paragraphs to avoid a bad boundary.
- **1.0+**: Strong content preference. Will accept noticeably uneven chunks to land on good split points.

For most books, the default of 0.5 works well. Increase it for dialogue-heavy novels where mid-conversation splits are disruptive. Decrease it for reference or non-fiction text where even sizing matters more.

## Overlap

Overlap repeats the last few paragraphs of chunk N at the start of chunk N+1. This gives the LLM context about what was just translated, improving consistency at chunk boundaries.

Overlap uses a **dual-constraint** strategy -- it takes paragraphs from the end of the previous chunk until **both** conditions are met:

1. At least `overlap_paragraphs` paragraphs
2. At least `min_overlap_words` words

This adapts to text density:
- Long narrative paragraphs (200+ words): 2 paragraphs usually satisfies both constraints
- Short dialogue lines (5 words each): Takes 10+ lines to reach the word minimum, preserving the full dialogue exchange

When chunks are combined after translation, the overlap is resolved using a "use previous" strategy: the translation from the end of chunk N is kept, and the overlapping start of chunk N+1 is discarded. This works because the translator has more context at the end of a chunk than at the beginning of the next one.

## CLI Usage

```bash
# Chunk a single chapter
python scripts/chunk_chapter.py projects/my-book/chapters/chapter_01.txt \
    --chapter-id chapter_01 \
    --target-size 2000 \
    --output projects/my-book/chunks/

# Chunk all chapters via the web UI (Step 4 in the dashboard)
```

## Chunk JSON Format

Each chunk is saved as an individual JSON file:

```json
{
  "id": "chapter_01_chunk_000",
  "chapter_id": "chapter_01",
  "position": 0,
  "source_text": "Chapter I  When this story begins...",
  "translated_text": null,
  "metadata": {
    "char_start": 0,
    "char_end": 5380,
    "overlap_start": 0,
    "overlap_end": 970,
    "paragraph_count": 18,
    "word_count": 2138
  },
  "status": "pending"
}
```

## Edge Cases

| Situation | Behavior |
|-----------|----------|
| Chapter shorter than `max_chunk_size` | Single chunk, no splitting |
| Chapter barely over `max_chunk_size` (e.g., 3100 words) | Two roughly equal chunks (~1550 each) |
| Single paragraph longer than `max_chunk_size` | Single chunk with a "too large" warning (paragraph is atomic) |
| Very short chapter (under `min_chunk_size`) | Single chunk with a "too small" warning |
| All boundaries are bad (entire chapter is dialogue) | Splits at the least-bad boundaries; degrades to approximately-even sizing |
| Chapter has scene breaks (`***`) | Scene breaks are strongly preferred as split points |

## Related Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md) - Full workflow including chunking as Step 4
- [CHAPTER_DETECTION_GUIDE.md](CHAPTER_DETECTION_GUIDE.md) - Splitting books into chapters (the step before chunking)
- [BATCH_PIPELINE.md](BATCH_PIPELINE.md) - Batch processing of chunks for evaluation and combining
- [PROMPT_GUIDE.md](PROMPT_GUIDE.md) - How chunks are turned into translation prompts
