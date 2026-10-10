# Reader: Sentence Retranslate Feature

In-reader gesture for getting a fresh LLM translation of a single sentence
(or N:1 alignment group) and replacing the existing translation. The user
confirms the source span before paying for the call (alignment is not
always perfect), can hand-edit the LLM output before accepting it, and
picks a model per request: a metered API, a subscription CLI, or the
local inference server (see *Model Flexibility*). The replace step mutates `chunk.translated_text`
and re-aligns the chapter — same durability path as `/api/remove-text`.

Sentence scoring with `judge_absolute` is **not** wired into v1; the same
modal can host `[Score]` buttons in a future phase.

## User Flow

1. In the bilingual reader, tap a Spanish sentence to open the bottom
   sheet.
2. Tap **Retranslate…** (sits next to *Edit chunk* and *Remove text…*).
3. The Retranslate modal opens with:
   - An **alignment confidence badge** in the header (e.g.
     `alignment: 0.93 ✓ high` or `alignment: 0.42 ⚠ low`).
   - A **Source (English)** textarea, pre-populated from the alignment
     row's English text.
   - A **Current translation** textarea, pre-populated from the literal
     chunk substring (`text_in_chunk`) — see *Source-span confirmation*.
   - A **Model** dropdown populated from `llm_config.json`. An entry that
     cannot be used right now is greyed out with the reason (`not loaded`,
     `no API key`, `CLI not installed`, `server not reachable`).
   - A **Retranslate** button.
4. Adjust either textarea if alignment looked off — add or trim
   sentences, fix typos, etc. Whatever's in **Source (English)** at click
   time is what the LLM sees.
5. Pick a model. The dropdown defaults to the system default on first
   load and the last-used model afterwards (persisted in `localStorage`
   under `retranslate.preferred_model` as `provider::model`). If the
   last-used model is not available now, the default is selected.
6. Tap **Retranslate**. While the LLM call runs, the status line shows
   `Calling LLM…`. On return, a **New translation** textarea appears,
   pre-filled with the LLM's output, plus a `Reset to LLM output`
   button and a cost label
   (`claude-sonnet-5 · 234→48 tokens · $0.0014`; a CLI shows
   `subscription` and the local server `local` in place of a price).
7. Hand-edit the new translation if desired. Click **Reset to LLM
   output** to restore the unedited model response (the front-end keeps
   it in JS state).
8. Tap **Replace**. A confirmation overlay (mirrors the remove-text
   pattern) warns that the chunk will be rewritten and the chapter
   re-aligned (~5–15s) and notes that backups are saved to
   `.chunk_edits/`.
9. Confirm. The reader closes the modal, runs the replace + re-align,
   and re-renders the chapter scrolled to the sentence above the edit.

## Source-span Confirmation

Sentence alignment is not always 1:1 or correct. The reader uses the
alignment row's `similarity` and `confidence` (`'high'` | `'low'`)
fields to bias the user's attention:

- **High-confidence rows** (`confidence='high'`, `similarity > 0.5`) —
  the user is unlikely to need to widen the source. The badge is green.
- **Low-confidence rows** — the badge is amber and the help text reminds
  the user to verify the selection. They can edit the source textarea
  freely (paste a different sentence, add a neighbor, etc.) before
  retranslating.

The two textareas are **independent**:

- The **Source (English)** value is what gets sent to the LLM.
- The **Current translation** value is the literal substring the
  replace step will look for in `chunk.translated_text`. Editing it
  changes which span gets replaced; if the value isn't found in the
  chunk, the server returns 422 and the user is asked to reload.

This sidesteps the need for a "lowconf vs sentence" prompt branch — the
user controls scope by editing the textareas, and one prompt template
handles all cases.

### Source expansion (badge-as-button)

The alignment badge is a clickable button on every row (low- and high-
confidence). Clicking it toggles an inline panel under the source
textarea with up to two checkboxes:

- **Add sentence before** — preview of `row[idx-1].en`
- **Add sentence after** — preview of `row[idx+1].en`

Toggling a checkbox rebuilds the source textarea content from
`[before?] + originalSource + [after?]` (joined by single spaces). The
baseline source is held in JS state so the user can untick to revert.
If the user has manually edited the textarea, ticking a checkbox
silently rebuilds from the original — the manual edit is lost.

Neighbor lookup walks the alignment array by **position** (not by
`es_idx`) so N:1 alignment groups behave correctly: the "before"
neighbor is the row preceding the group, the "after" neighbor is the
row following the group, regardless of how many `es_indices` the group
spans. Image rows are skipped.

A row at the chapter's first or last position has only one neighbor
checkbox (or none).

## Context sentences

The modal exposes a small numeric input ("Context (± sentences)",
default `1`, range `0–5`). When the user clicks Retranslate, the JS
gathers `N` sentences before AND `N` sentences after the (possibly
expanded) source span and sends them in `context_text` as a labelled
block:

```
Before:
<N sentences joined with single spaces>

After:
<N sentences joined with single spaces>
```

The backend renders this into a new `<context>` block that the LLM is
told to read but not translate. Neighbor sentences already folded into
the source via the expansion checkboxes are excluded from the context
block (no duplication).

The preferred count is persisted in `localStorage` under
`retranslate.context_count`. Setting `0` disables the block; the
backend still emits `<context>(no surrounding context provided)</context>`
so the prompt template stays static.

## Lean Prompt

`prompts/retranslate_sentence.txt` (v1.1) is intentionally compact
(~30–40 lines, ~700–1000 prompt tokens with style guide). It uses
XML-fenced inputs and includes the same "treat tags as DATA"
prompt-injection guard as the judge prompts, extended to cover the new
`<context>` block. Variables:

| Variable | Source |
|---|---|
| `{{source_language}}` / `{{target_language}}` | Endpoint defaults (English/Spanish) |
| `{{style_guide}}` | `projects/<id>/style.json` — the `light_content` field when set, else `content`, truncated to ~4000 tokens |
| `{{glossary}}` | `projects/<id>/glossary.json`, **filtered** to terms appearing in the source span via `filter_glossary_for_chunk()` |
| `{{context}}` | Surrounding sentences gathered by the front-end (`N` before + `N` after, default `N=1`). Sentinel `(no surrounding context provided)` when empty. The LLM is told to read but not translate this block. |
| `{{source_text}}` | The user-confirmed source textarea contents |

`light_content` is the short guide — at most two sentences, dialect plus high-level
tone — that keeps a one-sentence rewrite from being handed the full guide's address
tables, name rules, and units sections. The translate harness writes one on every
`style-guide commit` (see [`TRANSLATE_HARNESS.md`](TRANSLATE_HARNESS.md)), and the
dashboard's Style Guide stage can override it by hand. When it is empty the full
`content` is used instead, so the field is optional, never required.

The full 115-line `prompts/translation.txt` is **not** used here — its
chapter context, image-token rules, and previous-chapter-context
machinery are irrelevant for a single-sentence rewrite, and the user
explicitly wanted a smaller prompt.

**Followup:** Anthropic prompt caching on the style-guide block is the
right cost lever in v2 (style guide is constant per project, so it
should pay once per ~5-min cache window and ride free after that).
Not wired in v1.

## Model Flexibility

`/api/llm/models` returns the picker payload from `llm_config.json`,
flagging the default. The front-end fetches it on every modal open,
because which local model is loaded changes between opens. Per-call:

- The user picks any model in the dropdown.
- The request carries `provider` as well as `model`: one model id can
  sit under an API provider and a headless one, and they are different
  calls. If `provider` is omitted the backend resolves it via
  `resolve_provider_for_model()`.

The same prompt goes out by one of three mechanisms, chosen by the
provider's `type` in `llm_config.json`
([`LLM_PROVIDERS.md`](LLM_PROVIDERS.md#provider-types)). The dispatch is
`src/llm_mechanisms.py`, which is not specific to this screen:

| Mechanism | Provider `type` | How it is reached | Cost label |
|---|---|---|---|
| API | `anthropic`, `openai-compatible` | `call_llm()` | `$`, from the model's pricing and the `len // 4` estimator |
| Headless | `headless` | one `claude -p` or `cursor-agent -p` process, through `run_headless_wave` | `subscription`; tokens are the CLI's own count |
| Local | `local` | `llama-server` on the LAN | `local`; tokens are the server's own count |

**Headless.** The call goes through the same launcher as every wave, so
it keeps the credential scrub and the fail-closed login preflight; a
logged-out CLI comes back as a 502 carrying the CLI's own message. A
Claude model runs at the `effort` its catalog entry names; a Cursor
model carries its effort in the id (`grok-4.7-medium`). The job is
killed after 180 seconds. Each call appends a row to
`projects/<id>/.harness/retranslate/usage.jsonl`. Two things to expect:
the preflight probes run before every call (a few seconds), and the
token count includes the CLI's fixed prefix. On a prompt of about 700
tokens, Sonnet 5.5 billed about 7,500 and returned in 7 seconds; Grok
4.7 billed about 16,000 and took 34 (2026-10-09, one call each).

**Local.** Only a model the server already has loaded can be used.
The server is asked what is in memory before every prompt; a model that
is not loaded is refused with a 409 and nothing is sent, so the server
is never made to load or swap a model. Thinking is switched off in the
request. A catalog model matches the loaded one on its `id` or one of
its `aliases`. Gemma 4 31B on the Mac mini answered a one-sentence
prompt in about 9 seconds.

## API

### `GET /api/llm/models`

```json
{
  "default_model": "claude-sonnet-5",
  "models": [
    {
      "id": "claude-sonnet-5",
      "name": "Claude Sonnet 5",
      "provider": "anthropic",
      "mechanism": "api",
      "pricing": {"input": 2.00, "output": 10.00},
      "is_default": true,
      "available": true,
      "unavailable_reason": null
    },
    {
      "id": "claude-sonnet-5-5",
      "name": "Sonnet 5.5 (headless)",
      "provider": "claude-headless",
      "mechanism": "headless",
      "pricing": {},
      "is_default": false,
      "available": true,
      "unavailable_reason": null
    },
    {
      "id": "qwen3.8-27b",
      "name": "Qwen 3.8 27B (local)",
      "provider": "local",
      "mechanism": "local",
      "pricing": {},
      "is_default": false,
      "available": false,
      "unavailable_reason": "not_loaded"
    }
  ]
}
```

`unavailable_reason` is one of `no_key` (the provider's key env var is
not set), `cli_missing` (the CLI is not on PATH; its login is checked
when it is called), `server_down` (the local server did not answer) or
`not_loaded` (the local server has a different model in memory).

### `POST /api/sentence/retranslate`

```json
{
  "project_id": "fabre2",
  "chapter_id": "chapter_01",
  "chunk_id": "chapter_01_chunk_002",
  "es_idx": 7,
  "source_text": "The cake was burnt and the king was scolded.",
  "model": "claude-sonnet-5",
  "provider": "anthropic",
  "context_text": "Before:\nIt was a quiet morning in the castle.\n\nAfter:\nThe queen would later deny everything.",
  "expected_chunk_mtime": 1730000000.123
}
```

`context_text` is optional. When omitted/empty the prompt renders a
sentinel inside `<context>` so the template stays static.

Response on success:

```json
{
  "ok": true,
  "new_translation": "La torta se quemó y la campesina regañó al rey.",
  "model": "claude-sonnet-5",
  "provider": "anthropic",
  "mechanism": "api",
  "prompt_tokens": 234,
  "completion_tokens": 48,
  "cost_usd": 0.001422
}
```

- `provider` is optional in the request; resolved from `model` when
  omitted.
- `expected_chunk_mtime` guards against paying for a rewrite based on a
  stale source if another tab edited the chunk in between. `409` on
  mismatch.
- `400` if `source_text` is empty.
- `502` if the LLM produces unusable output after one retry
  (`RetranslationError`), or a headless or local call fails
  (`MechanismError`: the CLI is not logged in, the job timed out, the
  local server returned an error, dropped the connection after the
  prompt went out, or gave an answer that was cut off at `max_tokens` or
  was all reasoning).
- `409` also when a local model cannot be asked (`MechanismUnavailable`):
  it is not the model loaded, or the server did not answer. The body
  says which model is loaded.
- `400` when `provider` is a headless or local one and `model` is not
  listed under it in `llm_config.json`. An API provider still runs an
  unlisted id.
- `mechanism` is `api`, `headless` or `local`. `cost_usd` is 0 for the
  last two.

### `POST /api/sentence/replace`

```json
{
  "project_id": "fabre2",
  "chapter_id": "chapter_01",
  "chunk_id": "chapter_01_chunk_002",
  "es_idx": 7,
  "current_translation": "La torta se quemó.",
  "new_translation": "La torta se quemó y la campesina regañó al rey.",
  "expected_chunk_mtime": 1730000000.123,
  "chunk_offset_start": 412,
  "chunk_offset_end": 430
}
```

Response on success mirrors `/api/remove-text`:

```json
{
  "ok": true,
  "chunk_mtime": 1730000010.987,
  "alignment_mtime": 1730000011.234,
  "orphaned_annotations": 0,
  "corrections_purged": 0
}
```

- `current_translation` is the **literal substring** to be replaced in
  `chunk.translated_text`. Span resolution is three-tier:
  1. If `chunk_offset_start`/`chunk_offset_end` are present and
     `chunk.translated_text[start:end] == current_translation`, that exact
     span is replaced. This is the always-correct branch when the reader
     submits an unedited alignment row, and it prevents silent corruption
     when the same string appears more than once in the chunk (e.g. a body
     sentence whose text also lives inside an `[IMAGE:...]` caption).
  2. If offsets are present but don't slice cleanly (user edited the
     "current" textbox, or the row is stale), the server falls back to
     `chunk.translated_text.find(current_translation, chunk_offset_start)`
     — anchored near the user's intended position.
  3. If no offsets are sent (old clients), the server falls back to a plain
     `find()` from position 0.
- If the string isn't found anywhere, returns `422` ("Cannot locate the
  original sentence in the chunk. Reload and try again.").
- `expected_chunk_mtime` mismatch returns `409`.
- Empty `new_translation` (after strip) returns `400`.
- Replacement that would empty the chunk returns `400`.

## How `current_translation` is Pre-populated

The aligner emits ES sentence text via `pysbd.split` + `.strip()` +
`" ".join(...)` for N:1 groups. That text is **not** byte-identical to
chunk text — whitespace and paragraph breaks differ.

To give the front-end a literal substring it can round-trip cleanly,
`/api/alignment/<project_id>/<chapter>` now attaches three fields to
each non-image alignment row, before paragraph enrichment:

| Field | Meaning |
|---|---|
| `text_in_chunk` | Literal slice of `chunk.translated_text` covering this row's `es_indices` |
| `chunk_offset_start` / `chunk_offset_end` | Char offsets within `chunk.translated_text` |
| `chunk_mtime` | `chunks/<chunk_id>.json` mtime (used as the `expected_chunk_mtime` guard) |

Computation: re-run `_split_sentences_with_para_indices(chunk_text, 'es')`
on each chunk's `translated_text`, walk char positions sentence by
sentence, then map each row's `es_indices` (offset by the chunk's local
ES base) to char ranges. N:1 groups use the start of the first sentence
through the end of the last. Falls through quietly on unreadable
chunks; rows without a valid mapping just don't get the fields, and the
front-end falls back to `row.es`.

## Pipeline Side Effects

`/api/sentence/replace` calls the same `_apply_chunk_edits()` helper as
`/api/remove-text`:

1. Backup the pre-edit chunk JSON to
   `projects/<id>/.chunk_edits/<chapter>/<chunk_id>/<timestamp>.json`
   (last 10 retained).
2. Save the new `translated_text` (source text is unchanged for
   retranslate).
3. Purge any unapplied corrections for the edited chunk.
4. Recombine the chapter via `combine_chunks`.
5. Realign via `align_chapter_chunks` (full chapter, ~5–15s on a 30-chunk
   chapter — see *Out of scope*).
6. Re-anchor existing annotations by text match.
7. Re-evaluate the edited chunk.

Unlike `/api/remove-text`, **neighbor-overlap propagation is skipped**
— a retranslation is a rewrite, not a deletion, and rewriting an
overlap span in only one chunk keeps the seam visible to the human
reviewer rather than silently drifting.

## Audit Trail

Every successful replace appends a JSON line to
`projects/<id>/retranslations.jsonl`:

```json
{
  "project_id": "fabre2",
  "chapter_id": "chapter_01",
  "chunk_id": "chapter_01_chunk_002",
  "es_idx": 7,
  "chunk_offset_start": 412,
  "chunk_offset_end": 430,
  "current_translation": "...",
  "new_translation": "...",
  "timestamp": "2026-04-26T10:14:22.187"
}
```

`chunk_offset_start`/`chunk_offset_end` reflect the actual span the server
overwrote (whether they came from the client or were resolved via the
`find()` fallbacks), so a bad replacement can be diagnosed without
reconstructing the chunk state. Mirrors `removals.jsonl` and
`corrections.jsonl` for auditability.

## Edge Cases

- **Source not found in chunk** — front-end edited `current_translation`
  to a string that doesn't exist in `chunk.translated_text`. Server
  returns 422 with "Cannot locate the original sentence in the chunk.
  Reload and try again." Front-end surfaces this in the modal.
- **Concurrent edit** — another tab modified the chunk between modal
  open and request. Server returns 409 ("Chunk was modified by another
  process. Reload and try again.") on both retranslate and replace
  calls. The retranslate guard saves the user from paying for a stale
  rewrite.
- **Markdown fences in LLM output** — `_strip_markdown_fences()` peels
  ` ```text\n…\n``` ` wrappers and surrounding quotes before returning.
- **Empty LLM output** — `retranslate_sentence()` retries once with a
  stricter suffix; if still empty, raises `RetranslationError` and the
  endpoint returns 502.
- **Style guide missing or empty** — `_load_style_guide_content()`
  returns `""` and the prompt renders `(no style guide configured)` in
  place of the style block.
- **Style guide oversized** — truncated to ~4000 tokens with a
  `[...truncated]` marker; warning logged.
- **Glossary missing** — endpoint logs a warning and proceeds with no
  glossary block. The prompt renders `No glossary terms specified.`
- **N:1 alignment group** — `current_translation` pre-populates with
  the entire group span (joined as it appears in the chunk). Replace
  swaps the whole span. The model may return a single sentence or
  several; either is accepted.
- **Re-alignment latency** — full chapter re-align runs every replace
  (5–15s on a 30-chunk chapter). Future work (see *Out of scope*) is to
  re-align only the touched chunk and patch the alignment file
  in-place.

## Out of Scope (v1)

- **Sentence scoring** with `judge_absolute`. The judge primitive
  (`src/judge.py`) is unchanged; `[Score current]` / `[Score new]`
  buttons in the same modal are a planned v2 layer.
- **Pairwise verdicts** comparing current vs new.
- **Single-chunk re-align + alignment-file patch** to replace the
  full-chapter re-align.
- **Anthropic prompt caching** on the style-guide block.
- **UI undo**. `.chunk_edits/<chapter>/<chunk>/<ts>.json` backups are
  the manual undo path; surfaced in the confirmation modal copy.
- **Multiple rewrites per click**. The user can hit Retranslate again
  with a different model.
- **Auth / rate limiting**. Matches existing reader endpoints
  (local-only, unauthenticated).

## Files

| Path | Purpose |
|---|---|
| `src/retranslator.py` — `retranslate_sentence()` | Core primitive: prompt build, call via `llm_mechanisms.complete()`, fence strip, retry |
| `src/llm_mechanisms.py` — `complete()`, `list_models()` | One call for API, headless and local; the picker rows and their availability |
| `src/local_llm.py` | The local server client: what is loaded, and a completion on it |
| `projects/<id>/.harness/retranslate/usage.jsonl` | One row per headless retranslate call |
| `prompts/retranslate_sentence.txt` | Compact prompt template |
| `src/models.py` — `RetranslationResult` | Response Pydantic model |
| `web_ui/app.py` — `_attach_text_in_chunk` | Alignment-row enrichment with `text_in_chunk`, offsets, `chunk_mtime` |
| `web_ui/app.py` — `/api/llm/models` | Model picker payload |
| `web_ui/app.py` — `/api/sentence/retranslate` | LLM call endpoint with mtime guard |
| `web_ui/app.py` — `/api/sentence/replace` | Mutation + re-align endpoint |
| `web_ui/templates/reader.html` — `#retranslate-modal` | Modal markup |
| `web_ui/static/reader.js` — `openRetransModal`, `loadModelsOnce`, `showRetransConfirm` | Client logic |
| `web_ui/static/reader.css` — `.retranslate-modal-box`, `.retranslate-alignment-badge`, etc. | Modal styling |
| `web_ui/i18n.py` — `retranslate_*` keys | EN + ES strings |
| `scripts/_smoke_retranslate.py` | CLI smoke for iterating on the prompt without booting the web app |
| `projects/<id>/retranslations.jsonl` | Audit log |
