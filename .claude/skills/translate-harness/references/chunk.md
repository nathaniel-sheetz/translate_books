# Difficulty + chunk (backend-agnostic prep)

Load this at `stage: pre-chunk` (after glossary approval). Shared by every translation backend.

## Step 3 — Difficulty-aware chunk (deterministic) — estimator only

**3a — Score difficulty → default chunk target size.** The glossary now exists, so the scorer
reflects it:
```bash
python scripts/harness.py difficulty --project projects/<slug>
```
This prints `book_difficulty` and a book-level `suggested_target_size` (N), plus a **per-chapter
table** (each chapter's `difficulty` and its own `suggested_target_size`). Present the book
difficulty and `N`, and surface the per-chapter spread when chapters differ markedly (e.g. a
dialect-heavy chapter scored harder/smaller). Treat the suggestions as **defaults**: honor a user
override; otherwise chunk at them. (If `wordfreq` isn't installed, `wordfreq_available` is `false`
and suggestions lean toward 2000 — still usable.)

**3b — Chunk (estimator only).** Default to per-chapter sizing so each chapter uses its own
`suggested_target_size`:
```bash
python scripts/harness.py chunk --project projects/<slug> --size <N> --per-chapter
```
With `--per-chapter`, the chunker reads the per-chapter suggestions from `difficulty.json` (so run
`difficulty` first) and `--size <N>` is the **fallback** for any chapter not in the manifest. Each
chapter's target also rescales its min/max bounds, so a harder/smaller target actually splits more.
Drop `--per-chapter` to chunk the whole book uniformly at `--size <N>` instead (e.g. if the user
prefers one size). Either way this chunks once, prints the estimate, then halts — it runs
`--cost-only` and physically cannot spend. The estimate is **backend-neutral**: it shows the job
size, the metered-API price framed as *conditional* ("If translated via the metered API: ~$X"), and
a reminder that the **no-API workers backends** (subagent / headless) use your subscription (no
API $). Do **not** ask a binary "API vs subagent" question here.

**After chunking — hand off to the three-way backend gate.** If `config.backend` is unset, Read
`references/translate-workers.md` and run **4B-backend** (AskUserQuestion with exactly three
options: Metered API / Subagent / Headless). Carry the dollar figure into the
`references/translate-api.md` cost gate **only if** they pick API; on subagent/headless ignore it —
those paths gate on the `usage_summary` from `translate-prepare`, not this dollar estimate. If
`backend` is already set, load the matching translate reference and continue.
