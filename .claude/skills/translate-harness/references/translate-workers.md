# Translate — workers backends (subagent / headless)

Load this when `config.backend` is `subagent` or `headless`, **or when `backend` is unset**
(the three-way choice lives here). Shared wave / commit / align / reader-link flow.

## Step 4B — Workers path (subagent / headless) — ALTERNATIVE to `references/translate-api.md`

**"'no API $' is TWO backends.** Never persist `subagent` without having also offered `headless`
— for a Sonnet book with many chunks, headless is usually the better pick (preamble cache, no
Task turns in your transcript). Never invent a binary "Metered API | Subagent workers" question.

Both no-API-key backends run **spawned workers on the running subscription**, so a stranger can
translate token-free; the dollar figure from `chunk`/`cost` is the **metered-API** price and does
**not** apply — their gate is the `usage_summary` from `translate-prepare` (4B-b), not a cost estimate.
Headless *proves* this rather than assuming it: the launcher scrubs metered credentials from the
child env and the wave refuses to start unless the CLI confirms a subscription login.

**4B-backend. STOP — three-way backend gate (ask once, then save).** *Before* any review batch,
spawn-mode, or translation. **Skip** only when `config.backend` is already set (resume) and the
user is not asking to change it.

Show the conditional API dollar estimate from chunking for context, then **AskUserQuestion with
exactly three predefined options** (include the ~$X figure in the API label):

1. **Metered API (~$X)** — needs `ANTHROPIC_API_KEY`; spends metered dollars; auto-chains
   combine → epub → align.
2. **Subagent (Task workers)** — subscription; you spawn one `translator` Task per chunk
   (Read→Write→`done`); workers appear in your transcript; no preamble cache across Task calls.
3. **Headless (CLI fan-out)** — subscription, enforced (refuses to run on an API-key
   login); harness runs `translate-fanout` via
   `claude -p` (default) or `cursor-agent -p`; preamble cache on Claude/Sonnet after
   chunk 1; stays out of orchestrator context. **Bias toward this** when the worker
   tier is Sonnet and the book has many chunks. After choosing headless, optionally
   `config-set headless_cli cursor` (default remains `claude`).

In the question text, remind: do **not** treat "no API $" as one choice — options 2 and 3 are
different backends with different caching and failure modes. Bias toward a no-API-key backend
when there is no `ANTHROPIC_API_KEY`. END your turn and wait. Do **not** persist a backend, start
4B-0, or spawn workers in the same turn.

Once answered, **persist + log** — this single beat is what every later step reads back
(including footnote translation in `references/footnotes.md`):
```bash
python scripts/harness.py config-set --project projects/<slug> --key backend --value <api|subagent|headless>
python scripts/harness.py log-event --project projects/<slug> \
  --event backend --data '{"backend":"api"|"subagent"|"headless","model":"<model>"}'
```
- **api** → Read `references/translate-api.md` and **stop** loading this file (cost gate +
  `translate --yes` live there).
- **subagent** / **headless** → continue below (4B-0 → …).

The translate phase is a **review-first, set-by-set** flow: translate a small batch, auto-align it so
it is instantly readable, then translate the rest with the same spawn settings. Do the beats in order
— do **not** improvise parallelism; the spawn mode is the user's call (4B-0b).

**4B-0. STOP — propose a review batch first.** Only after `config.backend` is `subagent` or
`headless`. Before translating anything, suggest translating
**10–20% of the book's chapters** as a concrete range (e.g. "chapters 1–6 of 40") so the user can read
a sample in the reader and catch glossary/voice problems before the whole book is spent. They may
accept it, give a different range, or choose **all chapters in one go**. END your turn and wait.
Record the choice as `<set>` (a `--chapters` spec like `1-6`, or "all" = omit `--chapters`).

**4B-0b. STOP — spawn-mode gate (ask once, then save).** *Immediately after* the batch is chosen and
**before any translation**, ask via AskUserQuestion how workers should be spawned.

> **Skip this gate when the spawn mode is moot.** If `status` (or `translate-prepare`) reports
> `spawn_mode_moot: true` — i.e. every chapter is a single chunk — the three modes below are
> **equivalent** (there is no later chunk to inherit a previous chunk's Spanish, so "continuity"
> buys nothing). Don't make the user choose: just use all-parallel in bounded batches of
> `batch_size` and move on. The gate matters only for books with multi-chunk chapters.

Otherwise, three options; **bias toward #2 (the default)**:

  1. **Sequential** — one chunk at a time, in order. Slowest, but every chunk after the first sees the
     previous chunk's **English + Spanish** (max continuity). Pick if continuity beats speed.
  2. **Chapter-parallel (recommended, default)** — run a **window of X chapters (default 3)** at once
     and **finish that window before moving on**. Within a window, spawn **wave by wave on chunk
     position**: first the opening chunk of every chapter in parallel, then each chapter's second
     chunk, etc. First chunks across chapters run concurrently; later chunks within a chapter wait for
     that chapter's previous chunk, so within-chapter EN+Spanish continuity is preserved. Keep
     **`window ≤ batch_size`** so a first-position wave (one worker per chapter in the window) never
     exceeds the fan-out throttle; the defaults (window 3, batch_size 3) match.
  3. **All-parallel** — every chunk at once (in bounded batches). Fastest; **no** cross-chunk Spanish
     context (nothing is committed when prompts render). Pick only when speed clearly wins.

END your turn and wait. When answered, **save it** by passing it on the next `translate-prepare`
(`--parallelism sequential|chapter|all`, plus `--window <X>` for #2) — it is persisted to the project
config so the "translate the rest" batch reuses it without re-asking. Confirm X for mode 2 (default 3).
**Log the choice:** `log-event --event spawn_mode --data '{"mode":"sequential"|"chapter"|"all",
"window":<X>}'`.

**4B-0c. STOP — worker-thinking gate (ask once, then save).** *Only when the chosen `worker_model` is
thinking-capable* (`sonnet`/`opus`/`haiku` — a full-id worker resolves via `model_supports_thinking`),
ask via AskUserQuestion whether workers should engage **extended "think hard" thinking** — **default
No**. Extended thinking gives the worker room to reason through tricky passages, but is slower and burns
more subscription usage per chunk; leave it off unless the book's difficulty warrants it. **Skip this
question entirely for a non-thinking worker** (e.g. `fable`, always-on) — mirroring the GUI hiding the
checkbox — and just proceed with thinking off. Persist the answer by passing `--worker-thinking` (yes)
or `--no-worker-thinking` (no) on the next `translate-prepare`; it is saved to project config and reused
by the "translate the rest" batch (4B-f) without re-asking. **Log the choice:** `log-event --event
thinking --data '{"worker_thinking":true|false}'`.

**4B-a. Prepare (no spend).** Render one prompt per untranslated chunk in the set + a manifest, saving
the spawn mode:
```bash
python scripts/harness.py translate-prepare --project projects/<slug> --chapters <set> \
  --parallelism <mode> [--window <X>] [--worker-model sonnet] [--worker-thinking]
```
This prints a `manifest` (each entry: `chunk_id`, `chapter_id`, `prompt_path`, `draft_path`, and when
the cacheable prefix is stable across chunks also `preamble_path` + `body_path`), a `usage_summary`,
the `worker_model`, and the saved `spawn_plan` (`parallelism` + `window`). It does **not** call an
API. (Omit `--chapters` for the whole book.) Re-running only fills chunks that still need a
translation, so resume is free. The shared preamble lives at `.harness/translate/preamble.txt`;
per-chunk bodies at `.harness/translate/<id>.body.txt` — `preamble + body` is byte-identical to
`<id>.prompt.txt` when those paths are present.

**4B-b. STOP — usage gate. END THE TURN.** The subagent analog of the cost gate: no dollars, but
spawning N workers consumes real subscription/rate usage. Show the `usage_summary` ("N workers on
`<model>`, mode `<parallelism>`, **thinking: on/off**" — read the thinking state from
`usage_summary.worker_thinking`), confirm the worker model **and the backend already chosen** in the translate-workers intro, and ask via AskUserQuestion: **proceed / abort**. When approved, spawn per that
backend — **Subagent (Task workers)** → Option [1] below; **Headless** → `translate-fanout`
(Option [2]).

**Worker-model / cache note (fold into this confirmation, not a new gate):** the ~2k-token shared
preamble clears **Sonnet's** 1024-token cache minimum (caches for free on the headless path after
chunk 1) but **not** Opus/Haiku's 4096. Prefer a **Sonnet** worker for headless caching; Haiku/Opus
cache only if the preamble is later enlarged (full glossary in the prefix — follow-on). Task workers
do not get cross-invocation prompt caching either way. **Stable prefix:** headless caching needs
`preamble_path`/`body_path` on the manifest — prefer `always_include_dialogue` (and
`always_include_image_instructions` when the book has images) so dialogue/image opt-ins don't make
the prefix diverge across chunks. Without those, mixed chapters silently fall back to the full
`prompt.txt` (correct, just uncached). Headless puts the preamble in `--system-prompt-file`
(system role) for Claude Code caching; Task/API keep prefix+suffix as one user prompt — intentional
divergence, not a bug.

**End your turn and wait.** Do not spawn workers or run `translate-fanout` in the same turn that
produced the manifest. (The backend was already logged in the translate-workers intro; no separate
`fanout_mode` beat is needed.)

**4B-c. Spawn workers per the chosen mode, then commit.** Only after the user approves in a later turn.

**Never construct chunk paths by hand.** Take `chunk_path` / `chunk_id` straight from the manifest —
the on-disk name is `chunks/<chapter_id>_chunk_<NNN>.json` and the `chunk_id` is that filename's
stem (e.g. `chapter_01_chunk_000`), not `chunk_chapter_01_001`.

### Option [1] Task workers (default)

Each worker uses the **Task** tool with `subagent_type: translator` (`.claude/agents/translator.md`),
`model:` the approved `worker_model` (how the worker is pinned cheaper than you), and the prompt:
*"Translate one chunk. Read `<prompt_path>`. Write ONLY the translated prose to `<draft_path>`. Then
reply with exactly `done <chunk_id>` and nothing else — no summary, no list of choices."* **When the
manifest's `worker_thinking` is `true`, add the "think hard" trigger** so the worker engages extended
thinking: *"Translate one chunk. Read `<prompt_path>`. **Think hard** about the tricky passages, then
write ONLY the translated prose to `<draft_path>`. Then reply with exactly `done <chunk_id>` and nothing
else — no summary, no list of choices."* When `worker_thinking` is `false` (the default), use the plain
prompt above (no keyword → no extended thinking).

The worker writes its file and reports back only that token — **do not** have it return the prose *or a
recap of its choices* to you (either one floods your context). You learn each worker's success from
`translate-commit`'s `committed`/`failed`/`missing` lists, not from its chat-back.

### Option [2] Headless fan-out

Run one wave via the harness (bounded parallelism, neutral cwd, no Task turns). Pass `--chunk-ids`
when the spawn mode only wants the current wave's entries (chapter-parallel / sequential); omit it to
fan out every still-undrafted manifest entry (all-parallel batches):
```bash
python scripts/harness.py translate-fanout --project projects/<slug> \
  [--chunk-ids <id1,id2,...>] [--concurrency <batch_size>] \
  [--cli {claude,cursor}] [--cli-bin <bin>] \
  [--effort low|medium|high|xhigh|default] [--prompt-cache auto|5m|1h|off]
```
Persist the CLI family once per book (optional; default `claude`):
```bash
python scripts/harness.py config-set --project projects/<slug> \
  --key headless_cli --value cursor
```

**Effort (Claude only).** Translate waves run at `--effort high` by default —
the band `claude -p` already used, now named so it shows up in argv, the usage
rows and `status`. Persist a different level for this book's translate waves
with `config-set --key headless_effort_translate --value <level>`, or pass
`--effort` for one run. It is a **separate key from the judge/annotation
waves** (`headless_effort_judges` / `_annotations`, which default to `medium`):
a cheaper judge pass is a cheaper review of prose that already exists, while a
cheaper translate pass changes the prose itself. Reduced effort on literary
prose is **unmeasured** — the measured `low`/`medium` numbers in
`docs/LLM_PROVIDERS.md` are judge-wave data and do not transfer. Never lower
this silently; if the user wants a cheaper run, say what is untested about it.
Pin a Cursor model id at prepare time (manifest `worker_model`), e.g.
`translate-prepare --worker-model grok-4.5` or `--worker-model auto`. Or pass
`--cli cursor` on the fan-out itself (worker model still comes from the manifest).

**Claude profile (default):** each process is effectively
`claude -p` with the body (or full prompt) on stdin, optional `--system-prompt-file <preamble_path>`,
`--model <worker_model>`, `--tools ""`, `--output-format json` (the envelope is unwrapped, and
carries the usage the wave reports) → `draft_path`. The system-prompt
split is used only when `preamble + body` still equals `prompt.txt`; otherwise fan-out falls back
to the full prompt (no cache).

**Cursor profile (`--cli cursor` / `headless_cli=cursor`):** each process is
`cursor-agent -p --trust --mode ask --model <worker_model> --output-format text` with the
**full prompt on stdin** (no `--system-prompt-file`, no `--tools`). Auth is the interactive
`cursor-agent login` session — no `CURSOR_API_KEY`, no metered per-call spend. Pin
`worker_model` to a Cursor id (`grok-4.5`, `auto`, …); a Claude alias with cursor is almost
certainly a misconfiguration (warning only).

**Subscription enforcement (both profiles).** The child env is scrubbed of every metered
credential — the whole `ANTHROPIC_*` namespace, the `CLAUDE_CODE_USE_*` third-party switches,
`CURSOR_API_KEY` — while `CLAUDE_CODE_OAUTH_TOKEN` survives, since that *is* subscription auth.
On the Claude profile a `claude auth status --json` preflight then runs once per wave and blocks
it outright unless a subscription is confirmed; there is no override flag. **Caveat:** no verified
`cursor-agent` auth-status command exists, so the Cursor profile gets the scrub but not the
preflight. Full rationale in `docs/LLM_PROVIDERS.md`.

Headless does **not** use extended "think hard" thinking. After the wave, commit as below — the
prepare→commit seam is unchanged (`committed`/`failed`/`missing`).

After a wave's drafts are written (Task or headless), commit:
```bash
python scripts/harness.py translate-commit --project projects/<slug>
```
which guards each draft (length / completeness / image-token parity / echo), writes provenance, stamps
the chunks, and prints `committed` / `failed` / `missing` / `skipped` (idempotent — done chunks are
skipped).

`translate-commit` also **auto-runs and persists the coded evaluators** (`length, paragraph, dictionary, glossary, completeness, blacklist, grammar`) for each newly-committed chunk, so Review-tab badges update without any separate evaluate step.

It also **refreshes `chapters/<chapter_id>.txt`** for every chapter that becomes *fully* translated
in that run (reported as `recombined`; per-chapter failures under `combine_failed`, which never fail
the commit). That file is what the web reader reads for paragraph breaks and `[IMAGE:...]`
placement. Before this seam existed the workers path never wrote it at all, so it still held the
English split output. Backfill an older project — or repair a `combine_failed` chapter — with
`python scripts/harness.py combine --project projects/<slug> [--chapters <spec>]`. `status` flags the
drift as `combine_stale`; see `references/epub.md`.

> **Waiving a confirmed guard false-positive (`--allow-problem`).** Rarely a guard flags a chunk that is
> actually fine — e.g. the placeholder check trips on a legitimate Roman numeral heading. When you have
> *confirmed* the `failed` problem is spurious (read the named problem and the draft), re-commit with
> `--allow-problem <substring>` (repeatable) to drop only that problem:
> ```bash
> python scripts/harness.py translate-commit --project projects/<slug> --allow-problem XXX
> ```
> Every other guard stays enforced (a real defect still lands the chunk in `failed`), and the waive is
> reported under `waived` and recorded in the chunk's provenance log. Use this instead of hand-writing a
> stamping script. Do **not** blanket-waive — match the smallest substring of the specific false-positive.

> **Spawning into a flaky API — probe, throttle, commit-then-check.** Worker spawns can fail when the
> API is degraded. Handle it deterministically instead of hammering:
> - **Probe on any non-virgin project, not only after an error.** If `status` shows any translated
>   chunk (`stage: partial` / `fully-translated`), spawn **ONE** worker / `translate-fanout
>   --chunk-ids <one_id>` and confirm it lands in `wrote`, **not** `skipped_existing_draft`. A
>   skipped probe means stale drafts are on disk — stop and run `retranslate`
>   (`references/retranslate.md`); fanning out from here re-commits the OLD prose reporting success.
> - **Probe before a big wave.** After *any* spawn failure (or a known incident), spawn **ONE** worker
>   (Task) or `translate-fanout --chunk-ids <one_id>` first and confirm it writes a draft before
>   fanning out. A 1-worker probe discovers an outage at a fraction of the context/usage cost of a
>   failed full wave.
> - **`500` vs `529` are opposite signals.** A **500** is a server outage — concurrency is irrelevant;
>   **wait / back off**, don't change batch size, don't spam retries (pause and tell the user if it
>   persists). A **529** is *overloaded* — **reduce concurrency**: step the wave down the ladder
>   `batch_size → 3 → 1` until drafts land, then ramp back up toward `batch_size`.
> - **Commit-then-check, regardless of the Agent / claude -p error.** A Task worker often `529`s on
>   its final *wrap-up* turn **after** it already wrote a valid draft (you'll see `tool_uses: 2`). A
>   killed or partial `claude -p` leaves `missing` instead. So an error is **not** a reliable "no draft"
>   signal: after every wave (success or error) run `translate-commit` and trust its
>   `missing`/`failed` lists — not the spawn error text — to decide what to re-spawn. This avoids
>   re-translating chunks that already landed.

Spawn according to the saved mode (each wave is `batch_size` workers wide unless throttling down).
For **headless**, replace each "spawn Task workers" step with `translate-fanout` (pass `--chunk-ids`
for the wave's entries; pass `--concurrency` when throttling):

- **Sequential:** take the single lowest-position still-untranslated chunk, spawn **one** worker,
  `translate-commit`, then **re-run `translate-prepare`** (so the just-committed Spanish is baked into
  the next chunk's prompt) and repeat until the set is done.
- **Chapter-parallel (default):** work in windows of **X** chapters. For the current window:
  1. From the manifest, group entries by `chapter_id`; the **next wave** is the lowest-position
     still-untranslated chunk of each chapter in the window.
  2. Spawn those workers **in parallel** (multiple `Task` calls in one message, or one
     `translate-fanout --chunk-ids ...`), then `translate-commit`.
  3. **Re-run `translate-prepare --chapters <window>`** so each committed chunk's translation flows
     into its chapter's next chunk, and repeat from step 1 until every chunk in the window is committed.
  4. Only then advance to the next window of X chapters. Complete chapters, **not** "all first chunks
     first" — each window is fully finished before the next starts.

  Re-preparing a **narrower** scope no longer wipes a just-finished wave: `translate-prepare`
  keeps any non-empty `.draft.txt` on disk and **rescues** mappable uncommitted drafts into
  the new manifest (reported as `rescued_prior_drafts`), so `translate-commit` can still land
  them. Prefer committing a wave before re-preparing; unmappable or unreadable drafts stay on
  disk untouched. `window` is clamped to `batch_size` when it would exceed the fan-out throttle.
- **All-parallel:** spawn workers for **all** manifest entries in bounded batches of `batch_size`
  (the saved fan-out width; rate limits), `translate-commit` after each batch. No re-prepare (this mode
  has no cross-chunk Spanish context). This is also the mode to use whenever `spawn_mode_moot` is true.
  Headless: one `translate-fanout` call already waves at `batch_size`; then commit.

**4B-d. Re-spawn the misses.** For any `failed` (the report names the problem per chunk) or `missing`
(no draft written), re-spawn a worker for just those `chunk_id`s — Task spawn, or
`translate-fanout --chunk-ids <ids>` — write fresh prose to the same `draft_path` — and re-run
`translate-commit`. Cap re-spawns at ~3 per chunk, then surface the chunk for a manual edit-or-skip
decision rather than looping. **Log each re-spawn:** `log-event --event respawn --data
'{"chunk_id":"<id>","attempt":<n>,"reason":"failed"|"missing"}'`.

**4B-e. Align the set + give a reader link.** Once the set's chunks are all committed, make it readable
with no manual steps:
```bash
python scripts/harness.py align --project projects/<slug> --chapters <set>
```
This writes `alignments/<chapter>.json` for each fully-translated chapter and prints `reader_first` (a
link to the first chapter of the set). Ensure the reader is up — if nothing answers on port 5000,
start it in the background (`python web_ui/app.py`, serving `http://localhost:5000`). Then give the
user the `reader_first` link (e.g. `http://localhost:5000/read/<slug>/chapter_01`) so they can read the
new chapters immediately.

**Check `coverage_warnings` before you move on.** Each entry is a run of source sentences with *no
translation at all* — the worker dropped prose. This is the only place such a drop is visible: the
translation still reads perfectly, so the length ratio, paragraph counts, `high_confidence_pct` and the
`translate-commit` guards all stay clean (a real case shipped with a character ratio of 1.002). Report
every entry to the user — chapter, `chunk_id`, `position`, `sentences`, `chars`, `preview` — and
re-translate the affected chunk with the redo verb — **never by hand**:
```bash
python scripts/harness.py retranslate --project projects/<slug> --chunk-ids <chunk_id>        # preview
python scripts/harness.py retranslate --project projects/<slug> --chunk-ids <chunk_id> --yes  # execute
```
then re-run 4B-a → 4B-c for that chunk and re-align. **Clearing `translated_text` alone is a silent
no-op:** the chunk's old `.draft.txt` is still on disk, so `translate-fanout` skips it
(`skipped_existing_draft`) and `translate-commit` re-lands the *old* prose reporting
`committed: 1, failed: 0`. See `references/retranslate.md`.
`position` is relative to the chunk: `tail` on a non-final chunk means the drop sits on a
chunk seam, the most common shape; `full` means the entire chunk was unclaimed.

To also show a sample **in chat** (a quick EN→ES gut-check before spending the rest), use the read-back
command — never read `.harness/translate/*.draft.txt` (consumed/empty after commit) or hand-parse the
chunk files:
```bash
python scripts/harness.py show-translation --project projects/<slug> --chapters <set> --max-chunks 4
```
Read the result from `OUTPUT_JSON` (`.harness/last_output.json`) — do **not** write a `python` probe that
guesses the shape (it is **not** a top-level `chunks`/`items`/`samples` list). The structure nests
`chapters[] → chunks[] → translated_text`: the first chunk's prose is
`result["chapters"][0]["chunks"][0]["translated_text"]`, and its English is the sibling `["source_text"]`.
Committed translations also live in `projects/<slug>/chunks/*.json`. `--max-chunks` caps the sample; add
`--no-source` for translation-only.

**4B-f. Translate the rest (if a subset was done).** If you only did a review batch, prompt the user to
translate the **remaining** chapters now, noting the **same spawn mode/window as before** will be used
(it is saved — you can omit `--parallelism`/`--window`). On yes, repeat 4B-a → 4B-e for the remaining
`--chapters` range. When the whole book is translated, continue (`references/footnotes.md` if footnotes were imported,
then `references/epub.md` — combine + EPUB).

Then continue exactly as the API path does — **`references/footnotes.md` if footnotes were imported**, then `references/epub.md`
(combine + EPUB).
