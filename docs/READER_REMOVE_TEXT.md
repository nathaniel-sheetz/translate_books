# Reader: Remove Text Feature

In-reader gesture for removing stray sentences (publisher artifacts,
plate captions left by OCR, etc.) from a published book without leaving
reader mode. The feature edits **both** `chunk.source_text` and
`chunk.translated_text`, then runs the existing recombine + realign
pipeline so the chapter updates in place. Because chunks stay paired, a
later re-translate cannot resurrect the removed text.

## User Flow

1. In the bilingual reader, tap a Spanish sentence to open the bottom
   sheet.
2. Tap **Remove text…** (sits next to *Edit chunk*).
3. The Remove modal opens with two stacked panes:
   - **Spanish pane** — full `chunk.translated_text` of the seed chunk.
   - **English pane** — full `chunk.source_text` of the same chunk.
   - Each pane auto-scrolls so the seed-aligned sentence is visible
     and pre-highlighted in yellow.
4. Adjust the highlight in either pane:
   - Use **native browser text selection** — click-and-drag on
     desktop, long-press + selection handles on mobile.
   - Tap **Highlight** (yellow) to commit the current selection as the
     pane's highlight (replaces any existing highlight).
   - Tap **Clear highlight** (white) to empty the pane's highlight.
   - When nothing is selected, the toolbar shows **Reset** (back to
     the seeded suggestion) and **Clear** (empty highlight) instead.
5. Tap **Eliminar / Remove** to apply. The reader re-fetches alignment
   and annotations, re-renders, and scrolls to the sentence that was
   just above the removed region.

## Buttons (per pane)

| Button | Visible when | Action |
|---|---|---|
| Highlight (yellow) | Text is selected in this pane | Set this pane's highlight to the current selection |
| Clear highlight | Text is selected in this pane | Empty this pane's highlight |
| Reset | No selection | Restore highlight to the seeded suggestion |
| Clear | No selection | Empty this pane's highlight |

The toolbar swaps based on whether `window.getSelection()` has a
non-collapsed selection inside the pane. Buttons that operate on a
selection use `mousedown preventDefault` so tapping them does not
collapse the active selection before the click handler reads it.

## API

### `GET /api/removal-context/<project_id>/<chapter_id>/<es_idx>`

Returns the data the modal needs to render:

```json
{
  "chunk_id": "chapter_01_chunk_002",
  "chunk_mtime": 1730000000.123,
  "alignment_mtime": 1730000001.456,
  "es_full": "<chunk.translated_text>",
  "en_full": "<chunk.source_text>",
  "es_suggested": {"start": 421, "end": 482},
  "en_suggested": {"start": 387, "end": 444},
  "image_token_ranges_es": [[100, 142]],
  "image_token_ranges_en": [[88, 130]]
}
```

- `*_suggested` is computed by locating the alignment record's `es` /
  `en` text inside the chunk text (whitespace-tolerant). If the
  match fails, the field is omitted and the user paints from scratch.
- `image_token_ranges_*` lists the character ranges of every
  `[IMAGE:…]` token, computed via `_IMAGE_TOKEN_RE`. Used for the
  client-side image-overlap guard.

### `POST /api/remove-text`

Payload:

```json
{
  "project_id": "50-famous",
  "chapter_id": "chapter_01",
  "chunk_id": "chapter_01_chunk_002",
  "es_remove": "<exact substring of es_full>",
  "en_remove": "<exact substring of en_full>",
  "es_remove_start": 421,
  "en_remove_start": 387,
  "expected_chunk_mtime": 1730000000.123
}
```

Response on success:

```json
{
  "ok": true,
  "chunk_mtime": 1730000010.987,
  "alignment_mtime": 1730000011.234,
  "orphaned_annotations": 0,
  "corrections_purged": 0
}
```

Returns `409` if `expected_chunk_mtime` no longer matches the chunk
file (concurrent edit). Returns `400` for empty post-removal text,
image-token overlap, or substring miss.

## Server-Side Substring Matching

The server uses `_remove_substring(text, substring, hint_start)` with
this priority order:

1. **Hint match (preferred).** If `hint_start` is provided and
   `text[hint_start:hint_start+len(substring)] == substring`, that
   occurrence is used directly.
2. **Exact `text.find(substring)`** — first occurrence.
3. **Whitespace-normalized search.** Whitespace in both texts is
   collapsed; the hit is remapped back to original-string offsets via
   an index map.

### Why the hint matters

The same string can legitimately appear more than once in a chunk —
most notably, an `[IMAGE:images/foo.jpg:The Sword of Damocles.]`
caption can contain the same phrase that also appears as a standalone
sentence below the image. Without the hint, `text.find()` returns the
in-image occurrence and the image-token guard correctly rejects it,
even though the user highlighted the standalone sentence.

The hint solves this by letting the client tell the server *which*
occurrence the user actually highlighted. The server only trusts the
hint when the substring at that offset matches exactly — if the chunk
text has changed, falls back to search.

`expected_chunk_mtime` provides the safety belt: if the chunk file was
modified after the modal opened, the server returns `409` before any
removal happens, so the hint can never apply to a stale offset.

## Pipeline Side Effects

`_apply_chunk_edits(project_dir, project_id, chapter_id, edits)` runs
the same post-edit pipeline used by the chunk editor:

1. **Backup** — pre-edit chunk JSON snapshot under
   `projects/<id>/.chunk_edits/<chapter>/<chunk_id>/<timestamp>.json`.
2. **Save** the new `source_text` / `translated_text`.
3. **Purge corrections** — drop any unapplied rows in
   `corrections.jsonl` for the edited chunk (their original anchors no
   longer apply post-removal).
4. **Recombine** the chapter via `combine_chunks`.
5. **Realign** via `align_chapter_chunks` (sentence boundaries and
   `es_idx` numbering are recomputed from scratch, deterministically).
6. **Re-anchor annotations** by text match
   (`_reanchor_annotations_after_realign`). Annotations on removed
   text fall off and are reported as `orphaned_annotations`.
7. **Re-evaluate** as the chunk editor does.

Recombine + realign run **once per chapter**, even when the removal
touches multiple chunks (see overlap-region handling below).

## Overlap-Region Duplication

The combiner uses a `use_previous` overlap strategy: each chunk's
`metadata.overlap_end` text is duplicated in the next chunk's
`metadata.overlap_start`. If the user removes text inside the seed
chunk's overlap region, the same text lives in the neighbor chunk too.

`/api/remove-text` propagates the removal: a first-occurrence string
replace runs on the neighbor's `source_text` and `translated_text` so
the pair stays coherent. `metadata.overlap_*` numbers (length-from-edge,
not absolute offsets) are preserved — the combiner re-derives the
overlap window post-edit.

This is intentional and differs from the chunk editor, which forbids
overlap edits. The reader doesn't know which chunk visually "owns" the
sentence the user tapped, so we handle the duplication explicitly.

## Audit Trail

Every successful removal appends a JSON line to
`projects/<id>/removals.jsonl`:

```json
{
  "project_id": "50-famous",
  "chapter_id": "chapter_01",
  "chunk_id": "chapter_01_chunk_002",
  "es_remove": "…",
  "en_remove": "…",
  "es_remove_start": 421,
  "en_remove_start": 387,
  "timestamp": "2026-04-25T13:29:45Z"
}
```

Mirrors `corrections.jsonl` for auditability. Not consumed by the
reader.

## Edge Cases

- **Image tokens.** The client refuses any selection overlapping an
  `[IMAGE:…]` range (yellow Highlight button shows an inline error and
  the selection is dropped). The server double-checks via
  `_check_no_image_token_overlap(text, start, end)` and returns `400`
  if either side overlaps a token.
- **Empty post-removal.** If the resulting `source_text` or
  `translated_text` would be empty/whitespace-only, the server returns
  `400`. This usually indicates a mis-selection.
- **Whitespace tidying.** A single adjoining ASCII space is collapsed
  after removal to avoid `"foo  bar"`. Newline-based paragraph breaks
  (`\n\n`) are preserved.
- **Concurrent edits.** `expected_chunk_mtime` is compared against
  `chunk_path.stat().st_mtime` on the server; mismatch returns `409`
  with a "reload and try again" message.
- **Substring-not-found.** If neither the hint, exact, nor
  whitespace-normalized search locates the substring, the server
  returns `400` ("Could not locate the selected text in the chunk.").
- **Annotations on removed text.** Re-anchor surfaces them in
  `orphaned_annotations`; the client shows a count via `alert()` after
  the re-render completes.
- **Multi-chunk removals.** Not supported in v1. If the user wants to
  remove more than the seed chunk holds, they perform a second
  removal afterward.

## Files

| Path | Purpose |
|---|---|
| `web_ui/app.py` — `_remove_substring` | Hint-aware substring removal |
| `web_ui/app.py` — `_apply_chunk_edits` | Shared post-edit pipeline |
| `web_ui/app.py` — `/api/removal-context/...` | Modal seed data |
| `web_ui/app.py` — `/api/remove-text` | Apply removal endpoint |
| `web_ui/templates/reader.html` — `#remove-modal` | Modal markup |
| `web_ui/static/reader.css` — `.remove-pane`, `.hi`, `.remove-apply-btn` | Modal styling |
| `web_ui/static/reader.js` — `openRemoveModal`, `applySelectionAsHighlight`, `updateActionButtons` | Client logic |
| `web_ui/i18n.py` — `remove_*` keys | EN + ES strings |
| `projects/<id>/removals.jsonl` | Audit log |
