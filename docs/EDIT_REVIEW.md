# Translation Edit Review

On-demand HTML report comparing each chunk's current translation against the
raw LLM response that produced it. Surfaces the post-LLM edits you've made so
you can tag the *kind* of fix (glossary gender, missing paragraph break,
dialogue punctuation, etc.) and accumulate training data for a future
automated reviewer.

Not a live log — you run it per chapter or per project when you want to
look at what changed.

## Quick start

```bash
# Generate a report for one chapter and open it in the browser
python scripts/review_edits.py --project 50-famous --chapter chapter_27 --open

# All chapters in a project (heavier — every chunk is scanned)
python scripts/review_edits.py --project 50-famous --open
```

The report lands at `projects/<project_id>/reports/edits-<scope>-<timestamp>.html`
and is served by the Flask app at `/reports/<project_id>/<filename>`. Opening
via the Flask URL (the default for `--open`) is important — the tag buttons
embedded in the page `POST` to `/api/edit-tag`, and same-origin avoids CORS.

If the web UI is on a non-default port, pass `--port 5050` (or whatever).

## What the report shows

For every chunk whose current translation differs from its LLM baseline, the
report renders one section per chunk and one **hunk** per detected edit:

- **Side-by-side translation diff.** A ~120-character window around the edit
  in each pane (LLM baseline ↔ current), with word-level red/green
  highlighting. Tight enough to focus on the specific change.
- **Source pane.** A deliberately wider window (~400 characters) of the
  current English source for the same region. The middle, proportionally
  mapped from the translation hunk's char range, renders in normal color;
  the outer context is **dimmed** to indicate "this is here for reading
  comfort but may not actually correspond to the diff." The mapping is a
  cheap proportional heuristic — alignment isn't always perfect, which is
  why the window errs wide.
- **Banner: "Source text was also edited"** appears when the source you've
  changed since the LLM call (e.g. you deleted an image caption via the
  reader). Some of the translation delta is then *intentional* and not a
  fix to flag.
- **Banner: "No LLM baseline found"** appears when the script can't
  resolve a baseline — most often for chunks translated before the
  provenance stamp was added.

Each hunk has a tag form: pick from a small predefined vocabulary, add an
optional free-text note, click **Save tag**. The row appends to
`projects/<id>/edit_review_tags.jsonl` (one JSON object per line) with a
server-generated timestamp. Multiple tags per hunk are allowed.

## Tag vocabulary

Defined as `EDIT_TAGS` in `web_ui/app.py` and surfaced via `GET /api/edit-tags`:

- `glossary-gender-conflict` — *e.g.* the glossary forces `Ganso` but this goose is female, producing "la Ganso"
- `missing-paragraph-break` — speaker change wasn't broken into a new paragraph
- `dialogue-punctuation` — em-dashes, guillemets, nested quotes mis-handled
- `nested-quote-handling` — character quotes another character within their own speech
- `image-caption-removal` — caption was deleted to avoid duplicate text near figure
- `source-language-edit` — the English side was edited (caption, footnote, OCR fix)
- `style-tone` — register or formality adjustment
- `other` — escape hatch; pair with a descriptive note

Add categories by editing the `EDIT_TAGS` list in `web_ui/app.py`. The
predefined list is enforced server-side; the report includes whatever tags
were in the constant at generation time.

## How baselines are resolved

For each chunk, the script tries two strategies in order:

1. **Provenance stamp** (preferred). When the LLM writes a chunk's translation,
   the code captures the path of the prompt-log file written by `log_prompt()`
   via a `ContextVar` and assigns it to `chunk.last_llm_log`. Edits leave this
   field untouched, so it continues pointing at the *generating* LLM call.
   Resolution is O(1) and unambiguous.

2. **Fallback chunk-id scan**. For chunks translated *before* the stamp
   existed: the script scans `prompts/history/*translation*.json` for entries
   with a matching `metadata.chunk_id` and a non-null response, sorted
   newest-first. To guard against `chunk_id` collisions across projects (prompt
   logs don't carry `project_id`), each candidate's parsed source text is
   sanity-checked against `chunk.source_text` (first 200 normalized characters
   must overlap). Mismatches are rejected and the next candidate is tried; if
   none match, the chunk renders with the "no baseline" banner.

A chunk's translation diff is *only* meaningful relative to a specific LLM
call. If you re-translate a chunk N times, only the most recent run is the
baseline; the earlier rejected outputs still exist in `prompts/history/` but
the report doesn't expose them.

## Where the data lives

| Path | Purpose | Mutability |
| --- | --- | --- |
| `projects/<id>/chunks/*.json` | Current chunk state. Source + translation. Carries `last_llm_log`. | Mutated by edits |
| `prompts/history/<ts>_translation_<hash>.json` | Raw LLM call: prompt + response + metadata. | Append-only |
| `projects/<id>/reports/edits-<scope>-<ts>.html` | Generated reports. | Overwrite per run |
| `projects/<id>/edit_review_tags.jsonl` | Accumulated tags + notes. One JSON object per line. | Append-only |

The tag log is intentionally append-only. Re-running the report re-reads the
JSONL and pre-populates the visible chip set per hunk, but the row history is
preserved (you can audit who-tagged-what-when later, or feed the whole stream
to a training pipeline).

## Provenance stamp wiring

`chunk.last_llm_log` is set at every site where a whole-chunk LLM response
becomes `chunk.translated_text`:

- `src/api_translator.py::translate_chunk_realtime` — real-time single-chunk
  API call.
- `src/api_translator.py::_retrieve_anthropic_results` and
  `_retrieve_openai_results` — batch retrieval, when the batch result lands
  back on a chunk.

The path is read via `last_log_path()` from `src.utils.prompt_logger`, which
exposes the path of the most recent `log_prompt()` write in the current
context (a `ContextVar`-backed peek; no signature changes to `call_llm()` or
its many callers).

**Edits and span-level retranslates leave the stamp alone.** The chunk
editor, `/api/remove-text`, and `/api/sentence/replace` all preserve
`last_llm_log` so a chunk's "what came out of the LLM" pointer survives
arbitrarily many edits. The trade-off: a span-level retranslate that's
accepted into the chunk still diffs against the original whole-chunk LLM
baseline, which means the accepted-LLM-suggestion shows up as an edit in
the report. Currently this is acceptable; if it becomes noisy we can mark
those edits separately.

## Tunables

In `scripts/review_edits.py`:

- `TRANSLATION_CONTEXT_CHARS = 120` — chars on each side of a hunk in the
  translation panes.
- `SOURCE_CONTEXT_CHARS = 400` — chars on each side of the proportional
  inside region in the source pane.
- `MERGE_HUNK_GAP_CHARS = 40` — merge two adjacent hunks if they're within
  this many chars of each other (the current-translation side). Larger →
  fewer, bigger hunks; smaller → more, finer-grained hunks.

## Known limits

1. **Paste vs. hand-edit isn't distinguished.** Both show up in the report
   as "current differs from baseline." A future "Paste full translation"
   UI mode could clear `last_llm_log` to mark the paste case explicitly.
2. **Earlier regenerations become invisible** once a newer one replaces
   the baseline. The earlier outputs survive in `prompts/history/` if you
   ever need to reconstruct them by timestamp scan.
3. **Source-text edits inflate the diff.** Visible via the "Source text was
   also edited" banner, but the report doesn't separate
   "source-induced delta" from "translation fix" at the hunk level.
   Span-level tagging would solve this; out of scope for v1.
4. **Pruning `prompts/history/` loses baselines** for any chunk whose log
   file gets removed. The stamp points at a path, not a snapshot. If you
   prune, consider also copying `response` into the chunk JSON first.
5. **Hunk indices shift if the chunk is edited again** after tagging. The
   JSONL row still survives, but its `hunk_index` may no longer line up
   with the next render. The viewer collapses duplicate `(chunk_id, hunk_index, tag)`
   triples but doesn't currently flag stale indices.
6. **Proportional source mapping is approximate.** That's by design — the
   wider window with dimming is the user-facing compromise. If precision
   matters, `projects/<id>/alignments/<chapter_id>.json` has real
   sentence-level alignments from `src/sentence_aligner.py` that a future
   version could consume.

## File map

| File | Purpose |
| --- | --- |
| `scripts/review_edits.py` | CLI + report generator |
| `web_ui/templates/edit_review_report.html.j2` | Jinja template for the report |
| `web_ui/static/edit_review_report.css` | Styling for diffs and dimmed source |
| `web_ui/app.py` (`EDIT_TAGS`, `/api/edit-tag`, `/api/edit-tags`, `/reports/<project_id>/<path:filename>`) | Server endpoints |
| `src/models.py` (`Chunk.last_llm_log`) | Provenance field |
| `src/utils/prompt_logger.py` (`last_log_path`, `relative_log_path`) | Path peek helpers |
| `src/api_translator.py` (`translate_chunk_realtime`, batch retrieval) | Stamp write sites |
