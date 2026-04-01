# Batch Pipeline

The batch pipeline script runs evaluate and combine stages across all chapters in a project with a single command. It replaces the need to manually run `evaluate_chunk.py` and `combine_chunks.py` for each chapter individually.

## Quick Start

```bash
# Combine all translated chapters in a project
python scripts/batch_pipeline.py projects/my-book --stages combine

# Evaluate all chapters, then combine
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine
```

## How It Works

The script discovers chapters automatically by scanning `{project}/chunks/` for files matching the pattern `*_chunk_*.json`. It groups them by chapter ID (everything before `_chunk_NNN.json`) and processes each chapter in natural sort order.

No configuration file is required. The script uses directory conventions:

```
projects/my-book/
  chunks/
    chapter_01_chunk_000.json
    chapter_01_chunk_001.json
    chapter_02_chunk_000.json
    ...
  translated/          <-- combine output goes here
  glossary.json        <-- auto-discovered for evaluation
```

## Stages

### evaluate

Runs evaluators on every translated chunk. Chunks without a translation are skipped. After evaluation, each chunk's status is updated to `validated` (all evaluators passed) or `failed` (any errors).

Default evaluators: `length`, `paragraph`, `completeness`. The glossary evaluator is included automatically when a glossary is found.

### combine

Validates that each chapter has a complete set of translated chunks, then combines them using the "use_previous" overlap strategy. Output files are written to `{project}/translated/{chapter_id}.txt`.

Chapters with missing translations are skipped with an error message rather than halting the entire run.

## Options

### Select stages

```bash
# Only evaluate (no combining)
python scripts/batch_pipeline.py projects/my-book --stages evaluate

# Only combine
python scripts/batch_pipeline.py projects/my-book --stages combine

# Both, in order
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine
```

### Glossary

The script auto-discovers `{project}/glossary.json` if it exists. To use a glossary at a different path:

```bash
python scripts/batch_pipeline.py projects/my-book --stages evaluate --glossary path/to/glossary.json
```

### Choose evaluators

```bash
python scripts/batch_pipeline.py projects/my-book --stages evaluate --evaluators length,paragraph,glossary
```

### Process specific chapters

```bash
python scripts/batch_pipeline.py projects/my-book --stages combine --chapters chapter_01,chapter_02
```

### Override output directory

```bash
python scripts/batch_pipeline.py projects/my-book --stages combine --output-dir output/spanish/
```

### Dry run

See what would be processed without making any changes:

```bash
python scripts/batch_pipeline.py projects/my-book --dry-run
```

### Verbose output

Show per-chunk evaluation results and word counts:

```bash
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine --verbose
```

## Example Output

```
Glossary: 49 terms from projects/my-book/glossary.json

Project: projects/my-book
Stages: evaluate, combine
Chapters: 10
Total chunks: 10
Output: projects/my-book/translated

  chapter_01 (1 chunks)
  chapter_02 (1 chunks)
  ...
  chapter_10 (1 chunks)

======================================================================
BATCH PIPELINE SUMMARY
======================================================================

Chapter               Chunks  Eval'd  Passed  Combined
------------------------------------------------------
chapter_01            1       1       1/1     Yes
chapter_02            1       1       1/1     Yes
...
chapter_10            1       1       0/1     Yes
------------------------------------------------------
TOTAL                 10      10      8/10    10/10
```

## All Arguments

| Argument | Default | Description |
|---|---|---|
| `project_dir` | (required) | Path to project directory |
| `--stages` | `combine` | Comma-separated: `evaluate`, `combine` |
| `--glossary` | auto-discover | Path to glossary JSON |
| `--evaluators` | `length,paragraph,completeness` | Comma-separated evaluator names |
| `--output-dir` | `{project}/translated/` | Directory for combined chapter files |
| `--chapters` | all | Comma-separated chapter IDs to process |
| `--dry-run` | off | Show plan without making changes |
| `--verbose` | off | Show per-chunk details |
