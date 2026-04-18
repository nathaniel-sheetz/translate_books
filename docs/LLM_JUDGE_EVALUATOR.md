# LLM Judge Evaluator

Use an LLM to evaluate translation quality — either scoring a single translation (absolute mode) or comparing two translations side-by-side (pairwise mode). The main use case is the **model comparison harness**: "Is Sonnet meaningfully better than Haiku for this book?"

## Quick Start

Compare two models on a chapter:

```bash
python scripts/compare_models.py \
    --source projects/fabre2/chapters/chapter_01.txt \
    --models sonnet,haiku \
    --judge claude-sonnet-4-6 \
    --project projects/fabre2
```

This translates the chapter with each model, runs coded evaluators (grammar, glossary, etc.) on both, then asks the judge to pick a winner per chunk. Results go to `comparisons/<project_id>/<run_id>/`.

## How It Works

```
source chapter (txt)
      │
      ▼
  [chunker] ─────────────────────────── existing pipeline
      │
      ▼
  for each model in --models:
     [translate_chapter_with_model]       Anthropic batch API
      │
      ▼
  for each chunk × model:
     [run_all_evaluators] → coded signals (grammar, glossary, etc.)
      │
      ▼
  pairwise loop (round-robin model pairs):
     for each chunk pair:
        randomize A/B order
        [judge_pairwise] → PairwiseVerdict
      │
      ▼
  [write CSV + raw JSONL]
      │
      ▼
  [bootstrap CI on win rates] → summary
```

### Phases

1. **Translate** — Each model translates the full chapter via the batch API. Models that fail are dropped; the harness continues as long as ≥2 models remain.
2. **Coded evaluators** — Existing evaluators (length, paragraph, glossary) run on every translation. Their output is formatted as context signals for the judge.
3. **Pairwise judging** — For every model pair and every chunk, the judge compares the two translations. A/B order is randomized per chunk to guard against position bias. Results are un-swapped before writing to CSV.
4. **Summary** — Win rates with 95% bootstrap confidence intervals, per-dimension breakdowns, position bias warnings, and length-bucket analysis.

## Rubric

The judge scores on four dimensions:

| Dimension | What it measures |
|---|---|
| **Fluency** | Reads naturally as Spanish. Not a translation that sounds translated. |
| **Fidelity** | Conveys the source meaning. No additions, no omissions, no drift. |
| **Regional** | Consistent with the chosen Spanish variant. No mixed register. |
| **Voice** | Preserves the author's voice (rhythm, formality, idiom density) as described in `style.json`. |

In **pairwise** mode: A wins, B wins, or tie per dimension, plus an overall winner.

In **absolute** mode: 1–5 per dimension, normalized to `[0.0, 1.0]` via `(avg(dims) - 1) / 4`.

### Voice dimension

Voice context comes from the project's `style.json` (`content` field — the prose style guide already written for the translator LLM).

- **`style.json` exists with non-empty `content`** — Voice dimension is scored. Content is passed to the judge prompt under a `<voice_context>` tag.
- **`style.json` missing or `content` empty** — Voice dimension is omitted entirely. The judge uses a no-voice prompt variant and `voice_winner` is `N/A`.
- **`content` > 4000 tokens (~16K chars)** — Truncated with a `[...truncated]` marker, warning logged.

## CLI Reference

```
python scripts/compare_models.py [OPTIONS]
```

| Flag | Required | Default | Description |
|---|---|---|---|
| `--source` | ✓ | — | Path to the source chapter text file |
| `--models` | ✓ | — | Comma-separated model IDs or aliases (e.g. `sonnet,haiku,opus`) |
| `--project` | ✓ | — | Path to the project directory |
| `--judge` | | `claude-sonnet-4-6` | Model used as the judge |
| `--provider` | | from `llm_config.json` | LLM provider |
| `--style` | | auto-detected from project | Path to `style.json` for voice context |
| `--output` | | `comparisons/<project>/<run_id>/` | Output directory |
| `--cost-limit` | | `5.0` | Max estimated cost (USD) before requiring `--confirm` |
| `--confirm` | | false | Proceed even if cost exceeds `--cost-limit` |
| `--verbose` | | false | Debug logging |

### Model aliases

Short aliases are resolved to full model IDs:

| Alias | Model ID |
|---|---|
| `sonnet` | `claude-sonnet-4-6` |
| `haiku` | `claude-haiku-4-5-20251001` |
| `opus` | `claude-opus-4-0-20250514` |
| `gpt4o` | `gpt-4o` |
| `gpt4o-mini` | `gpt-4o-mini` |

You can also pass full model IDs directly.

## Output

Each run writes to `comparisons/<project_id>/<run_id>/`:

```
comparisons/fabre2/a1b2c3d4-.../
  pairwise.csv          # one row per chunk per model-pair
  raw.jsonl             # run header + raw judge JSON per verdict
  style_guide.txt       # copy of style.json used (if any)
  prompts/              # session-scoped judge prompt logs
```

### CSV columns (pairwise)

| Column | Description |
|---|---|
| `run_id` | UUID for this harness invocation |
| `project_id` | Project directory name |
| `chapter_id` | Source file stem |
| `chunk_id` | Chunk identifier |
| `source_hash` | SHA-256 of the source chunk text |
| `position_a_model` | Model presented as Translation A to the judge |
| `position_b_model` | Model presented as Translation B to the judge |
| `translation_a_hash` | SHA-256 of translation A |
| `translation_b_hash` | SHA-256 of translation B |
| `judge_model` | Full model ID of the judge |
| `judge_prompt_version` | SHA-256 of the prompt template |
| `fluency_winner` | `A`, `B`, or `tie` |
| `fidelity_winner` | `A`, `B`, or `tie` |
| `regional_winner` | `A`, `B`, or `tie` |
| `voice_winner` | `A`, `B`, `tie`, or `N/A` |
| `overall_winner` | `A`, `B`, or `tie` |
| `word_count_a` / `word_count_b` | Word counts for length-bias detection |
| `status` | `success`, `translation_failed`, `judge_failed`, or `batch_omitted` |
| `judge_rationale_short` | First 200 chars of the judge's rationale |

**Important:** `A`/`B` in the CSV are mapped back to the *original* model order (not the randomized presentation order). So `overall_winner = A` always means the first model in the pair won.

### JSONL header

The first line of `raw.jsonl` is a run header capturing reproducibility metadata:

```json
{
  "type": "run_header",
  "run_id": "...",
  "judge_model": "claude-sonnet-4-6",
  "judge_temperature": 0.0,
  "judge_prompt_version": "sha256...",
  "git_commit": "abc123...",
  "judge_max_tokens": 4096,
  "started_at": "2026-04-18T..."
}
```

## Cost Estimation

Before running, the harness prints a full cost breakdown:

```
Estimated cost:
  Translation: $0.1200 (2 models × 20 chunks, batch 50% discount)
  Judge:       $0.0800 (1 pairs × 20 chunks × ~1200 tokens avg)
  Total:       $0.2000
  Estimated prompt log size: ~0.1 MB (20 judge calls × ~4 KB each)
```

If the estimate exceeds `--cost-limit` (default $5), the harness stops and asks you to pass `--confirm`.

Pairwise judge calls grow `O(models² × chunks)`: 2 models = 1 pair, 3 models = 3 pairs, 4 models = 6 pairs per chunk.

## Statistical Safeguards

### Effective N

Chunks have overlap from the chunker. The harness estimates the effective independent sample size and warns when it's too small:

```
Effective independent N: ~14 (overlap-adjusted)
WARNING: N_effective < 50; "winner" claims are noise-prone.
```

**Minimum recommended N for any "model A is better" claim: 50 effective chunks.** Single-chapter runs are exploratory only.

### Bootstrap confidence intervals

Win rates include 95% bootstrap CIs (10,000 resamples):

```
Sonnet wins 12/20 = 60% (95% CI [40%, 80%])
Haiku  wins  6/20 = 30% (95% CI [15%, 50%])
```

If the CI overlaps 50%, the result is not statistically significant.

### Position bias detection

A/B order is randomized per chunk, and the raw presentation order is tracked. If either position wins >70% of comparisons, a warning is emitted:

```
⚠️  WARNING: Position bias detected (>70% for one position).
Judge may be favoring A/B position rather than content.
```

### Length-bucket breakdown

Verdicts are broken down by chunk length (short <100w, medium 100–300w, long >300w) so you can spot length-dependent bias.

## Reproducibility

Every run locks:

- **`temperature=0`** for all judge calls
- **Pinned model version** — full model ID stored, never an alias
- **Prompt template hash** — SHA-256 of the prompt file, recorded in CSV and JSONL
- **Git commit** — `HEAD` at run start

Re-running the same harness on the same source with the same config should produce identical CSV output (modulo `run_id` and timestamps).

## Using the Judge in the Existing Pipeline

The `LLMJudgeEvaluator` in `src/evaluators/llm_judge_eval.py` wraps the absolute scoring mode into the standard `BaseEvaluator` interface. It can be used anywhere the existing coded evaluators run:

```python
from src.evaluators.llm_judge_eval import LLMJudgeEvaluator

evaluator = LLMJudgeEvaluator()
result = evaluator.evaluate(chunk, context={
    "style_json_path": Path("projects/fabre2/style.json"),
    "coded_eval_results": prior_results,  # optional: coded evaluator output
    "judge_model": "claude-sonnet-4-6",   # optional: override default
})

print(result.score)            # 0.0–1.0 normalized
print(result.metadata)         # {"fluency": 4, "fidelity": 5, ...}
```

## Prompt Templates

Four prompt templates live in `prompts/`:

| File | Mode | Voice |
|---|---|---|
| `judge_pairwise.txt` | Pairwise | ✓ |
| `judge_pairwise_no_voice.txt` | Pairwise | — |
| `judge_absolute.txt` | Absolute | ✓ |
| `judge_absolute_no_voice.txt` | Absolute | — |

All prompts use XML-fenced inputs (`<source>`, `<translation_a>`, etc.) with an explicit instruction to treat tagged content as data, not instructions — a guard against prompt injection from book content.

## Error Handling

| Scenario | Behavior |
|---|---|
| Judge returns malformed JSON | Retry once with a stricter "respond with only JSON" suffix; then mark `status=judge_failed` and continue |
| Judge returns out-of-range score (e.g. 7) | Clamped to [1, 5], warning logged |
| Translation fails for one chunk | Row written with `status=translation_failed`, comparison continues |
| Translation fails for entire model | Model dropped from comparison; continues if ≥2 models remain |
| Batch retrieval omits items | `status=batch_omitted` row emitted for each missing chunk |
| Cost exceeds limit | Prints breakdown, exits unless `--confirm` is passed |

## Examples

Compare three models with a custom style guide and higher cost limit:

```bash
python scripts/compare_models.py \
    --source projects/fabre2/chapters/chapter_01.txt \
    --models sonnet,haiku,opus \
    --judge claude-sonnet-4-6 \
    --project projects/fabre2 \
    --style projects/fabre2/style_v2.json \
    --cost-limit 10 --confirm
```

Answer "does a richer style guide produce better prose?" by running the same comparison twice with different style guides and comparing the CSV output:

```bash
# Run 1: minimal style guide
python scripts/compare_models.py \
    --source projects/fabre2/chapters/chapter_01.txt \
    --models sonnet,haiku \
    --project projects/fabre2 \
    --style projects/fabre2/style_v1.json

# Run 2: richer style guide
python scripts/compare_models.py \
    --source projects/fabre2/chapters/chapter_01.txt \
    --models sonnet,haiku \
    --project projects/fabre2 \
    --style projects/fabre2/style_v2.json
```

Compare the voice dimension scores across runs to see if the richer guide helps.
