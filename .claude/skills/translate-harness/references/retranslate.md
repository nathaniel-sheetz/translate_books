# Re-translate (redo) — clearing work you intend to replace

Load this when the user asks to **redo / re-translate / start over** on chapters or chunks that
**already have translations** — a whole-book redo, a chapter the user disliked, or a single chunk
flagged by `coverage_warnings`. Everything else in this skill is resume-shaped (idempotent
*forward*); this is the one destructive beat.

## Why there is a dedicated verb (read before improvising)

Clearing `translated_text` by hand and re-running the wave **silently produces the wrong result.**
Every link is individually correct; the composition is not:

| Step | Behavior |
|---|---|
| Clear `translated_text` | drafts untouched |
| `translate-prepare` | keeps non-empty drafts on disk **by design**; the rescue only covers drafts *not* in the new manifest, so it reports `rescued_prior_drafts: 0` and the stale drafts stay invisible |
| `translate-fanout` | **skips** every entry with a non-empty existing draft (`skipped_existing_draft`) |
| `translate-commit` | skips only chunks that already have a translation — you just cleared them all, so **nothing** is skipped and it commits every stale draft |
| Report | `committed: N, failed: 0, missing: 0`, evaluators green, `align` clean |

The user receives "N/N re-translated, zero failures" over **byte-identical old prose**. Every guard
passes, because the old drafts are genuinely good translations — just not new ones. Draft-skip is
right for *resume* and wrong for *redo*; `retranslate` is what tells the two apart.

- **NEVER hand-roll a clear** against `src.utils.file_io`. `projects/` is gitignored — a wrong glob
  is unrecoverable, there is no dry run, and nothing gets logged.
- **NEVER delete chunk JSON files.** `source_text` and the chunking live there.
- Don't guess the field set. `status` as a bare `"pending"` string trips a pydantic serializer
  warning; the verb uses the `ChunkStatus.PENDING` enum for you.

## R-0. Probe before fan-out on ANY non-virgin project

If `status` reports `stage: partial` or `fully-translated`, spawn **one** worker (Task) or
`translate-fanout --chunk-ids <one_id>` and confirm the result lands in **`wrote`**, not
`skipped_existing_draft`, before the full wave. This rule is not only for recovering from spawn
errors (see the flaky-API blockquote in `translate-workers.md`) — a *skipped* probe is how you
discover stale drafts before they masquerade as success.

## R-1. Preview (never skip)

```bash
python scripts/harness.py retranslate --project projects/<slug> [--chapters <spec>]
# or a single chunk:
python scripts/harness.py retranslate --project projects/<slug> --chunk-ids <chunk_id>
```

Without `--yes` this changes **nothing** (`dry_run: true`). Read `.harness/last_output.json` and
report to the user in this order:

1. `counts.chunks` / `counts.chapters` — what is about to be destroyed.
2. `stale_drafts` (with mtimes) — name the landmine out loud.
3. `downstream` + `warnings` — annotations, review marks, alignments, EPUBs.

## R-2. STOP — the redo gate. END THE TURN.

Ask via **AskUserQuestion, two questions in one call**:

1. **Proceed / Abort** — label it concretely: *"Clear N translations across M chapters"*.
2. **"Create a separate archive of the original first?"** — *Snapshot (recommended)* | *No snapshot*.

State plainly in the question text: `projects/` is gitignored, **there is no restore command**, and
restoring is a manual file copy. Do **not** run the clear in the same turn that produced the preview.

## R-3. Execute

```bash
python scripts/harness.py retranslate --project projects/<slug> [--chapters <spec>] \
  --archive --yes
```

Confirm `cleared`, `drafts_deleted` and `archive.dir` from `last_output.json`. The archive is a
**precondition** — if the copy fails, nothing is cleared and the command returns an `error`. A
`retranslate` beat is written to the run log automatically; no manual `log-event` needed.

## R-4. Re-translate

Return to `references/translate-workers.md` **4B-a → 4B-f** (or `references/translate-api.md` when
`config.backend == api`). Do **not** re-ask the backend, spawn mode or worker-thinking gates — they
are saved in `.harness/config.json`. Run R-0's one-chunk probe **first**.

Note: `cost` on an already-translated book short-circuits to `note: "all chunks already translated"`
with no dollar figure, so a redo's backend gate may have to label the metered-API option
qualitatively. That is expected, not a failure.

## R-5. Verify the redo actually redid something

`0 failed` is **not** evidence — that is exactly what the silent no-op reports. Three cheap checks:

1. `show-translation --chapters <one> --max-chunks 1` — eyeball one paragraph against what the user
   saw before. Look for **different prose**, not a different count.
2. `status` → `combine_stale` should be empty (`translate-commit` recombines each chapter as it
   completes).
3. `align --chapters <set>` → report `coverage_warnings`.

## R-6. Downstream cleanup — the user's call (retranslate never touches these)

| Artifact | State after a redo | Fix |
|---|---|---|
| `annotations.jsonl` | `es_idx` anchors into replaced prose — **mis-anchored**, not merely stale | user decides; no command |
| `corrections_applied.jsonl` | historical record of edits to prose that is gone | leave it |
| `reviewed.json` | chapters marked reviewed now describe text nobody has read | user re-reviews |
| `alignments/*.json` | reader shows mismatched Spanish | `align --chapters <set>` |
| `*.epub` | built from the replaced text | `epub` |
| `chapters/*.txt` | holds the old translation until the chapter re-completes | automatic (`translate-commit`) |
| `evaluations/` | **self-healing** — `translate-commit` re-runs the coded evaluators | nothing |

## Restoring from an archive (manual — there is no restore command)

Read `projects/<slug>/archive/<stamp>/manifest.json` first: it lists exactly what was captured and
where each file came from, plus `contains` / `excludes`. Then copy back what you want and re-run
`combine` + `align`.

The snapshot is **narrower than the `downstream` census** the preview shows you — the census reports
everything a redo affects, the archive stores only what it can meaningfully restore:

| In the archive | NOT in the archive |
|---|---|
| `chunks/*.json` (in scope), `chapters/*.txt`, `alignments/*.json` | `.chunk_edits/` — manual per-chunk edit history |
| `*.epub` | `retranslations.jsonl` |
| `annotations.jsonl`, `corrections_applied.jsonl`, `reviewed.json` | `evaluations/` (self-healing), `.harness/` (rebuilt by prepare) |

`retranslate` never deletes anything in the right-hand column — it is excluded from the *snapshot*,
not destroyed. But if the user plans to hand-edit or delete those after the redo, copy them
somewhere first: the archive will not bring them back.

## What this file deliberately does NOT do

- No restore command, and no automatic deletion of annotations, corrections or review marks.
- Never deletes chunk JSON.
- No backend / spawn-mode / worker-thinking re-asking — those gates live in
  `references/translate-workers.md` and their answers live in `.harness/config.json`.
