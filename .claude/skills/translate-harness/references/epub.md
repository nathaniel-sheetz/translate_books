# Combine + EPUB

Load this on the workers path when chapters are translated and you need the downloadable EPUB. (API `translate` auto-builds.)

## Step 5 — Combine + EPUB (translated chapters only)

The API `translate` run chains through combine, epub, and align, building the EPUB from translated
chunks only and reporting exactly which chapters shipped. On the **subagent** path you already aligned
each set in translate-workers 4B-e (the reader reads `alignments/`, not the EPUB), so here you only (re)build the
EPUB — the downloadable deliverable — from whatever chapters are translated so far:

> **Stitching contract.** Chunks are created with **zero overlap**, so `combine` is a plain
> concatenation of each chunk's translation (one blank line at every boundary) — there is no
> overlap de-dup, on either backend. The prompt's "previous section" block is **continuity context
> only and is never re-combined.** Overlap/combine de-dup is disabled (known-broken): `combine`
> hard-fails if a chunk ever carries overlap. So a worker must translate its **whole** chunk and
> never drop content that also appears in the previous-section block. See
> `docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md` #20.

```bash
python scripts/harness.py epub --project projects/<slug>
# --title / --author / --language default to what you set at setup; pass them to override.
```
Report the included/skipped chapter lists so a partial translation is never mistaken for a complete
book. Confirm the EPUB landed:
```bash
ls projects/<slug>/*.epub
```
