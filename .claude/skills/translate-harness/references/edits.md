# Hand edits: fixing a sentence the user approved

Load this when you have found a real slip and the user has agreed on the fix. Typical
sources:
- a judge finding that `run_judges apply` withheld as `manual`, or a filtered one;
- a coded finding you checked against the English;
- a dropped heading;
- a word the user wants changed.

Two verbs land the fix exactly as the web UI would, and neither needs the web UI to be running:

| Verb | Mirrors | Use for |
|---|---|---|
| `sentence-edit` | the reader's Save, then **Apply corrections** | **Almost everything.** One edit = one sentence (alignment row). Several small sentence edits always beat one chunk edit |
| `chunk-edit` | the chunk editor | Only what no single sentence holds: a dropped heading or line, a paragraph split or join, a change across a sentence boundary |

Never POST to the web UI's routes, and never edit `translated_text` in `chunks/*.json` by
hand. Both skip the audit row, the snapshot and the realign.

## Steps

1. **Show the user each pair first**: the English, then old → new. Get approval. An edit
   is a content decision like any other.
2. **Write the edits file with the Write tool.** Don't use a heredoc or an inline shell
   string: Spanish punctuation and quotes break shell quoting. Put it in the scratchpad.
   ```json
   [
     {"chapter": "chapter_02", "find": "servicio real", "replace": "servicio efectivo",
      "finding": {"eval_name": "editorial", "chunk_id": "chapter_02_chunk_003", "issue_index": 0},
      "reason": "false friend: actual service"},
     {"chapter": "chapter_02", "es_idx": 41, "expect": "Espera acabarla",
      "new": "Espera terminarla y luego empezar a ganar."}
   ]
   ```
   - **`find` / `replace`:** replaces text inside the one sentence that contains `find`.
     Quote enough to be unique. You can add `es_idx` or `chunk_id` to scope it.
   - **`es_idx` / `new`:** rewrites that whole sentence. Read `es_idx` from
     `alignments/<chapter>.json`, and add `expect` (a few words of the sentence as it
     reads now). A realign that splits a sentence renumbers every later row, so without
     `expect` an index read earlier can rewrite the wrong sentence.
   - **`expect`** (optional, either form): text the sentence must contain, or the edit
     is refused.
   - **`finding`** (optional): names the finding this fixes. It gets the machine
     `applied` mark, so the reader stops showing it. Name it whenever there is one.
   - **`reason`** (optional): a short why, kept on the audit row.
3. **Dry run**, then read `planned` (each `before` / `after` / `en`, plus any `save_check`
   hits):
   ```bash
   python scripts/harness.py sentence-edit --project projects/<slug> --file <edits.json> --dry-run
   ```
4. **Apply**: run the same command without `--dry-run`. Then relay `applied`, any
   `save_check_warnings` (fix the slip, or say why it stands) and `reader_links`. One
   realign runs per chapter, however many edits the batch held.

### `chunk-edit`, the fallback

```bash
python scripts/harness.py chunk-edit --project projects/<slug> --chunk-id chapter_02_chunk_005 \
  --file <spans.json> --dry-run
```

The spans file is `[{"find": "...", "replace": "..."}]`, or
`{"spans": [...], "finding": {...}, "reason": "..."}`. Each `find` must occur exactly once
in the chunk and may cross sentences and paragraphs (`\n\n`). For example, to restore a
dropped heading:

```json
[{"find": "…última frase del párrafo.\n\nHacer un invento",
  "replace": "…última frase del párrafo.\n\nHACER QUE EL NEGOCIO RINDA\n\nHacer un invento"}]
```

The refusals are the chunk editor's own:
- the `[IMAGE:…]` tokens must not change;
- the overlap with the next or previous chunk can't be edited (edit it in that chunk instead);
- the result can't be empty.

## Refusals that need the user

- **`pending`**: the reader holds unapplied Saves in that chapter, and a realign would hide
  them. Ask the user either to press **Apply corrections** in the reader, or to let you
  pass `sentence-edit --include-pending`, which applies them in the same batch, as that
  button would. The flag needs at least one edit of your own; it never just empties the
  queue. `chunk-edit` also refuses Saves queued on its own chunk unless
  `--purge-pending` is passed. That flag discards them, as the chunk editor does, so ask
  the user first.
- **`refused` (nothing was written)**: one edit could not be placed. The batch is all or
  nothing. Fix that entry, using the listed `candidates` for an ambiguous `find`, and
  re-run the whole file.
- **"spans more than one sentence"**: split it into one sentence edit per sentence. Use
  `chunk-edit` only when the change truly crosses the boundary.
- **"alignment … is older than its chunks"**: a chunk was written after the last realign.
  Run `harness.py align --chapters <chapter>`, re-read the `es_idx` values, and re-run.
- **"expect not in sentence"**: the row's text changed since you read it. Re-read the
  alignment and fix the entry.

## What gets recorded

Every edit is a row in `corrections_applied.jsonl` with `source: "harness:chat"`. These are
model edits the user approved, so the KDP authorship facts count them as model fixes, not
as reader Saves. Each edited chunk also gets:
- a `.chunk_edits/` snapshot;
- a stale stamp on its evaluation;
- annotations re-anchored by the realign.

`chunk-edit` also re-runs the coded evaluators.
