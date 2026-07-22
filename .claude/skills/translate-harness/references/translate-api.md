# Translate — metered API backend

Load this **only** when `config.backend == api` (already chosen in the 4B-backend three-way gate
in `references/translate-workers.md`). Auto-chains combine → epub → align after translate.

If you landed here with `backend` unset, do **not** persist `api` yourself — Read
`references/translate-workers.md` and run **4B-backend** first.

## Step 4 — COST beat, then translate

```
┌──────────────────────────────────────────────────────────────────────────┐
│ THIS IS THE ONLY PAID STEP AND THE ONLY HARD STOP THAT COSTS MONEY.        │
│ Estimating cost and starting translation are TWO SEPARATE TURNS.           │
│ Print the estimate, ask, and END YOUR TURN. Never run `chunk`/`cost` and   │
│ `translate` in the same response. No earlier approval (style guide,        │
│ glossary, chunk) authorizes this — only the answer to the question below.  │
└──────────────────────────────────────────────────────────────────────────┘
```

1. You already have the estimate from `references/chunk.md`. To recompute it (still WITHOUT spending):
   ```bash
   python scripts/harness.py cost --project projects/<slug>
   ```
2. **STOP — approval beat. END THE TURN HERE.** Show the estimate, then ask via AskUserQuestion:
   proceed / abort — and stop. Do not call any further tool in this response. Resume ONLY after the
   user has, in a *later* turn, explicitly chosen to proceed. If unsure whether they approved, treat
   it as NOT approved and ask again. **Confirm the model with them here** — this is the only
   place on the API path where the model is chosen; it determines the price shown in the estimate.
   > Cost note (eng review 2026-06-05): the API path does not use prompt caching today, so input
   > tokens are not discounted across chunks. The estimate is the honest figure.
3. **Only once the user has affirmatively approved in a separate turn**, translate:
   ```bash
   python scripts/harness.py translate --project projects/<slug> --yes
   ```
   `translate` refuses to run without `--yes`. The model defaults to Sonnet 5 (or whatever was
   persisted in config); pass `--model` to override, and surface the choice rather than assuming.

**If footnotes were imported, do `references/footnotes.md` before `references/epub.md`** to translate + embed them (the API
`translate` run auto-chains through combine/epub/align, but not the footnote *body* translation).
