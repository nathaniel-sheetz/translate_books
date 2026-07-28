# Combine + EPUB

Load this on the workers path when chapters are translated and you need the downloadable EPUB, or
when `status` reports `combine_stale`. (API `translate` auto-builds.)

## chapters/*.txt — what it actually holds (read this)

`projects/<slug>/chapters/<chapter_id>.txt` is **dual-purpose**:

- the split step writes the **English** section text there (`setup` / `split`);
- combine **overwrites it with the translation**.

So on a translated project that file is Spanish; on a workers-path project that predates the
auto-recombine it may still be **English**; and after a redo it may hold the **previous**
translation. The EPUB does not care — `epub` builds from `chunks/*.json` via
`build_epub_from_chunks` — but the **web reader does**: `/api/alignment/...` reads this file to
re-derive paragraph breaks and `[IMAGE:...]` placement, so stale content means mis-tagged paragraphs
and dropped or misplaced images.

`translate-commit` now recombines each chapter as it completes (`recombined` in its output). Repair
or backfill explicitly:

```bash
python scripts/harness.py combine --project projects/<slug> [--chapters <spec>]
```

Only **fully-translated** chapters are written; partial ones come back under `skipped`. `status`
flags drift as `combine_stale` — if that list is non-empty, run `combine` before handing out a
reader link. (Chapters edited in the web UI's chunk editor can also show up there; that is real
drift, not a false alarm.)

> **Stitching contract.** Chunks are created with **zero overlap**, so `combine` is a plain
> concatenation of each chunk's translation (one blank line at every boundary) — there is no
> overlap de-dup, on either backend. The prompt's "previous section" block is **continuity context
> only and is never re-combined.** Overlap/combine de-dup is disabled (known-broken): `combine`
> hard-fails if a chunk ever carries overlap. So a worker must translate its **whole** chunk and
> never drop content that also appears in the previous-section block. See
> `docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md` #20.

## Step 5 — EPUB (translated chapters only)

The API `translate` run chains through combine, epub, and align, building the EPUB from translated
chunks only and reporting exactly which chapters shipped. On the **workers** path you already
aligned each set in translate-workers 4B-e (the reader reads `alignments/`, not the EPUB), so here
you only (re)build the EPUB — the downloadable deliverable — from whatever chapters are translated
so far:

```bash
python scripts/harness.py epub --project projects/<slug>
# --title / --author / --language default to what you set at setup; pass them to override.
```
Report the included/skipped chapter lists so a partial translation is never mistaken for a complete
book. Confirm the EPUB landed:
```bash
ls projects/<slug>/*.epub
```
