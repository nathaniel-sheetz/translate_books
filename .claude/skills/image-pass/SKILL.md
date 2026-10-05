---
name: image-pass
description: |
  Redo a book's images headlessly: translate the lettering on maps and diagrams,
  clean up scans, make cover art, or replace an illustration. Inventories the
  images a book references, runs Codex on a ChatGPT subscription to produce
  candidates, shows them beside the original, and swaps the approved one in under
  the original filename with a backup. Subscription-only and fail-closed, no dollars.
  Use when asked to "redo the images", "translate the text in this map", "clean up
  the scans", "make a cover", "replace this illustration", or "image-pass".
allowed-tools:
  - Bash
  - Read
  - Write
  - AskUserQuestion
---

# image-pass

The work that used to be the ChatGPT web UI plus copying files into
`projects/<slug>/images/` by hand. The deterministic surface is one non-interactive
CLI — **`scripts/image_pass.py`** — with eight subcommands that each print JSON.

**Division of labour: you look, Codex draws, the user picks.** You `Read` each image
and write its job. Codex only makes pixels. Nothing reaches `images/` until a human
has seen the candidate beside the original. Three STOP gates sit on that path and
none of them is optional.

## What this writes

Replacement files in `projects/<slug>/images/`, **under the original name and
format**. That is the whole design:

- No `[IMAGE:images/<file>:<alt>]` token in `source.txt`, the chapters, the chunks or
  the alignments is ever touched. `git status` shows nothing, because `projects/` is
  gitignored and no tracked text changes.
- The reader (`serve_project_image`) and the EPUB builder read the same path and
  pick the new pixels up as-is. A new cover lands at `images/cover.jpg`, which
  `_resolve_cover` already auto-detects.
- The first replacement of any file copies the original to
  `projects/<slug>/images_original/<file>`. **Nothing ever overwrites that copy**, so
  replacing an image twice cannot turn a generated picture into "the original".
  `revert` restores from there, byte for byte.

Alt text is **not** this skill's business. The `:<alt>` half of a token is translated
text and belongs to translate-harness.

## The CLI (read first)

`python scripts/image_pass.py <inventory|backfill|prepare|generate|review|apply|revert|verify>`
prints one JSON object and mirrors it to
`projects/<slug>/.harness/images/last_output.json` (`OUTPUT_JSON: <path>` on stderr) —
**Read that file** rather than piping stdout through a second interpreter.

```bash
# 1. Every image the book references, plus the cover. Rows go to inventory.json.
python scripts/image_pass.py inventory --project home-geography

# 1b. Gutenberg books: bring in the larger scans the source page links to.
python scripts/image_pass.py backfill --project home-geography --dry-run
python scripts/image_pass.py backfill --project home-geography [--accept 036.jpg,042.jpg] \
    [--source <url-or-saved-page>] [--images 042.jpg,057.jpg]

# 2. Validate jobs and render one prompt each. Spends nothing.
python scripts/image_pass.py prepare --project home-geography \
    --json-file projects/home-geography/.harness/images/jobs.json [--replace]

# 3. Estimate, then run. One Codex process per candidate.
python scripts/image_pass.py generate --project home-geography --estimate
python scripts/image_pass.py generate --project home-geography \
    [--concurrency 1] [--target-ids 006.jpg,cover.jpg] [--model <id>] \
    [--cli-bin <path>] [--timeout-minutes 20]

# 4. Original beside each candidate, as one static HTML page.
python scripts/image_pass.py review --project home-geography

# 5. The writer. Back up, convert to the original's name and format, replace.
python scripts/image_pass.py apply --project home-geography --dry-run \
    --json-file projects/home-geography/.harness/images/decisions.json
python scripts/image_pass.py apply --project home-geography \
    --json-file projects/home-geography/.harness/images/decisions.json [--max-side 1600]

# 6. Undo, and audit.
python scripts/image_pass.py revert --project home-geography --images 006.jpg   # or: all
python scripts/image_pass.py verify --project home-geography
```

An image is named by its path under `images/`: `006.jpg` and `images/006.jpg` are the
same image everywhere a command takes one.

### Jobs (`prepare --json-file`)

```json
[
  {"image": "006.jpg", "mode": "translate",
   "instruction": "Keep the engraved serif capitals.",
   "labels": {"A COMPASS.": "UNA BRÚJULA.", "NORTH": "NORTE"},
   "candidates": 2},
  {"image": "031.jpg", "mode": "restore",
   "instruction": "Heavy foxing lower left; leave the plate mark."},
  {"image": "cover.jpg", "mode": "cover",
   "instruction": "A schoolroom globe on a wooden desk, 1890s chromolithograph.",
   "labels": {"title": "Geografía del hogar"}}
]
```

| Field | |
|---|---|
| `image` | required. For `cover`, one of `cover.jpg` / `cover.jpeg` / `cover.png` — and the one the book already has, if it has one |
| `mode` | `translate` (swap lettering, touch nothing else) · `restore` (clean the scan, keep the picture) · `cover` (make or edit the cover) · `replace` (a new illustration for the same slot) |
| `instruction` | required. What *this* image needs, from having looked at it |
| `labels` | source → replacement map. **Required for `translate`.** Optional elsewhere: lettering to put on a cover, or to keep legible in a replacement |
| `candidates` | 1–4, default 1. Each one is a separate Codex run |
| `input` | `original` (default) or `current`. A redo of an already-replaced image starts from the publisher's file again unless you say `current` |
| `reference` | `cover` only: another of the book's images to draw the cover from (`"reference": "052.jpg"`). It is attached as source material, so the cover is a new portrait picture, not that image's proportions |

`prepare` is **all-or-nothing**: one invalid job refuses the batch and writes nothing.
Jobs merge into the manifest by image, so re-preparing one image leaves the others
alone; `--replace` makes the manifest exactly this batch.

**Changing a job's prompt — or what it starts from — archives its candidates** to
`jobs/<id>/previous/<stamp>/` rather than deleting them — each cost plan usage. An
unchanged job keeps its candidates and `generate` skips them.

### Decisions (`apply --json-file`)

```json
[
  {"image": "006.jpg", "candidate": 2},
  {"image": "031.jpg", "verdict": "redo", "note": "still foxed lower left"},
  {"image": "cover.jpg", "verdict": "skip", "note": "keeping the current one"}
]
```

`apply` is per-image: one refused row lands the rest and is reported by name. `skip`
and `redo` write nothing to `images/`; they are recorded in the ledger
(`.harness/images/images.jsonl`) with the note. A candidate larger than the slot is
downscaled — to twice the original's longest side, never below 1024 px (2560 px for a
cover) — so 87 replacements do not multiply the EPUB's size by ten.

## Subscription-only, by enforcement

`generate` runs `codex exec` through `src/harness/headless.py`, the only module in
the repo allowed to spawn a CLI. Two layers, same as the translate waves:

- **Scrub.** The child environment loses `OPENAI_*`, `CODEX_API_KEY` and
  `CODEX_ACCESS_TOKEN`. This matters twice over: Codex prefers a key to the ChatGPT
  session, and the bundled `$imagegen` skill has a "CLI fallback" that bills
  `OPENAI_API_KEY` directly. The prompts also forbid that fallback in words.
- **Probe.** `codex login status` must say `Logged in using ChatGPT`. An API key, an
  access token, workload identity, Bedrock, or anything unrecognised refuses. It runs
  before the estimate is reported and again before **every** job. It reports the
  login on file and nothing else — it does not notice a key in the environment, which
  is why the scrub is not optional.

- **Provider pin.** Every job passes `-c model_provider=openai`, so a
  `~/.codex/config.toml` that routes Codex to a third-party endpoint cannot apply —
  `login status` reports the login on file, not where requests go.

There is no override flag, and you must never look for one. A top-level `error` with
empty `wrote` / `failed` means nothing ran and nothing was spent: relay it verbatim.
The fix for a logged-out or API-key Codex is the user's to run — suggest
`! codex login` (and `! codex logout` first if it was an API key).

**If Codex cannot produce an image on this machine, stop and report.** Do not reach
for the OpenAI API, the `openai` package, or any other metered path to "finish the
job". That is a different decision and it is the user's.

## Flow

### 1. `inventory` — then actually look

```bash
python scripts/image_pass.py inventory --project home-geography
```

`Read` `inventory_path` for the rows (file, dimensions, bytes, chapter, alt text,
missing-on-disk, ledger status). Then `Read` the **image files** the user named — or
all of them, for a small book. A job written from a filename and an alt text is a
guess; the alt text is the caption, not the lettering inside the picture.

Relay `missing` (a token whose file is gone — the reader shows nothing there today)
and `unreferenced` (a file nothing points at) whether or not anyone asked.

### 1b. `backfill` — before any job on a Gutenberg book

A Gutenberg page shows a thumbnail and links it to a scan two or three times the
size. A book ingested before the ingest preferred those has the thumbnails, and on a
250-pixel map half the lettering cannot be read — by you or by Codex. `backfill`
puts the larger scan behind the same filename. No Codex, no usage, no spend.

```bash
python scripts/image_pass.py backfill --project home-geography --dry-run
```

Do it **before `prepare`**: a job starts from whatever the original is, and a
candidate drawn from a thumbnail is wasted usage.

- **It cannot be reverted.** The larger scan becomes the original: no backup of the
  thumbnail is kept, and a backup already in `images_original/` is upgraded in
  place. That is the point (a redo should start from the better file), and it is why
  `--dry-run` comes first.
- **Relay `relinked_from`.** Publishers mislink. A scan is only taken if it measures
  as the same picture (`score`, 1.0 = identical); when the linked one does not, the
  page's other scans are searched and the one that matches is used. Say which images
  that happened to.
- **`unlike` is a person's call.** No scan on the page matched. `Read` the image and
  the scan in `cache_dir` side by side. A re-cropped or re-proportioned plate of the
  same picture goes in `--accept`; a different picture is left alone and reported.
- `unmatched` has no larger scan linked; `split` is two placeholders that are halves
  of one scan — reported, never joined (that is a text edit in four places).
- `left_alone` means `images/<file>` is already a replacement: only its original was
  upgraded, and its job has to be re-prepared (`stale_jobs`) and generated again.

`inventory` again afterwards, and look at the images again: lettering you could not
read is the reason you ran this.

### 2. STOP — G1: the jobs gate

Propose one job per image, **in the chat**: the mode, the instruction, and for
`translate` the full label map.

**For `translate`, the label map is the deliverable of this gate.** List every piece
of English lettering you can read in the image — including the small ones: scale
bars, compass points, legends, "Fig. 3" — and propose the replacement for each.

- Take terms from the book's `glossary.json` first. A map label that contradicts the
  running text is worse than an untranslated one.
- Match the casing and punctuation of the original label (`THE GREAT BEAR.` →
  `LA OSA MAYOR.`).
- Say plainly which labels you could not read. A label you guessed at is a label the
  user has not approved.
- Proper names that stay as they are still go in the map, mapped to themselves, so the
  list is visibly complete.

Codex will spell exactly what the map says and nothing checks it afterwards except
the user's eyes at G3. Print the map as text. `AskUserQuestion` may pick *which
images* to do; it must never be where a label map lives — the widget truncates.

On approval, `Write` `projects/<slug>/.harness/images/jobs.json` and run `prepare`.

**END THE TURN before estimating.**

### 3. STOP — G2: the usage gate

```bash
python scripts/image_pass.py generate --project home-geography --estimate
```

No dollars, but real plan usage and real minutes. Quote `plan` and `limit_warning`
**verbatim** and get consent in a separate turn:

- `plan.candidates` Codex runs, `plan.estimated_minutes` in all.
- `plan.minutes_measured: false` means `minutes_per_image` is an assumption (2 min),
  not a measurement — say so. After the first run on a project it is that project's
  own median.
- `plan.model`. If it reads "(Codex default…)" the job runs whatever
  `~/.codex/config.toml` pins, which a ChatGPT login may not be allowed to use. A
  rejected model fails in seconds and spends nothing; re-run with `--model <id>`.
- The limit burn: an image costs roughly 3–5x an ordinary Codex turn against the
  ChatGPT plan's window. A big batch can run out part-way.

An honest line reads: *"12 images × 2 candidates = 24 Codex runs, about 48 minutes at
an assumed 2 min each (not yet measured here). Images burn the ChatGPT plan's limits
3–5x faster than text, so this may hit the window before it finishes — what is
already generated is kept. Run it?"*

For a first run on a book, propose **one or two images** before the batch. It
measures the minutes and proves the harvest on this machine for the price of two
runs.

Then:

```bash
python scripts/image_pass.py generate --project home-geography
```

- `--concurrency` defaults to 1 and should stay there unless the user asks: parallel
  runs burn the window faster, and more than one has not been tried against the real
  CLI.
- A usage-limit error **stops the batch** (`counts.not_run`) instead of failing every
  remaining candidate against it. Relay the `error`.
- **Re-running `generate` is the recovery.** It fills only the candidates still
  missing. Never re-`prepare` to recover.
- `status: partial` means some landed. Each `failed` row carries its own error;
  `jobs/<id>/run_NN/events.jsonl` holds that run's raw Codex event stream.

### 4. `review` — and look again

```bash
python scripts/image_pass.py review --project home-geography
```

Give the user `review_path` to open in a browser. Then `Read` every candidate
yourself, beside its original, and report **before** asking for picks:

- **Lettering.** Check each label against the map, letter by letter. Image models
  drop accents, turn `Ñ` into `N`, double a letter, or leave one English label behind.
- **Artwork.** In `translate` and `restore` the picture must be the same picture. A
  redrawn coastline or a river that moved is a failure even if the lettering is
  perfect — on a map it is a factual error.
- **Proportions.** `flags` already names a candidate whose aspect ratio is off the
  original's by more than 5%.

Say what you could not judge. A 250-pixel original gives you little to compare.

### 5. STOP — G3: the pick gate

Per image the user chooses: **accept candidate N**, **redo with a note**, or **skip**.
The candidates are pictures, so the user decides from the review page, not from your
description of it. An id-picker is fine here once they have looked.

`Write` `decisions.json`, then:

```bash
python scripts/image_pass.py apply --project home-geography --dry-run --json-file <decisions.json>
python scripts/image_pass.py apply --project home-geography --json-file <decisions.json>
```

`--dry-run` first, always: it writes nothing and reports `planned` with each
`backup_action` and any `warnings` (`aspect_changed`). Relay those, then run it live.

A `redo` is a new job: change the `instruction` (carry the user's note into it), re-run
`prepare` for that image, and go back to G2 for the extra runs.

### 6. `verify`, then hand back

```bash
python scripts/image_pass.py verify --project home-geography
python scripts/harness.py epub --project home-geography
```

| Code | Means | |
|---|---|---|
| `missing_file` | a token names a file that is not in `images/` | broken |
| `unreadable` | the file is not a decodable image | broken |
| `no_backup` | replaced, and `images_original/` no longer holds the original | broken |
| `backup_changed` | the backup is not the file recorded at apply | broken |
| `aspect_changed` | the replacement's proportions differ from the original's | warn |
| `changed_since_apply` | the file is not the one `apply` wrote | warn |
| `backup_without_ledger` | a backup with no ledger row | warn |

The reader serves the new files on the next page load. **An EPUB built earlier keeps
the old pictures** — rebuild it. That is translate-harness's step; this skill ends at
`verify`.

## What a live run established (codex-cli 0.157.0, Windows, 2026-10-05)

Two runs on a scratch copy of `home-geography`: `006.jpg` in `restore`, then `007.jpg`
(the compass-points diagram) in `translate`. Update this section when a later CLI
behaves differently.

- **Login probe.** `codex login status` writes one line to **stderr**:
  `Logged in using ChatGPT`, exit 0. Logged out: `Not logged in`, exit 1. The binary's
  other lines are `Logged in using an API key - <masked>`, `… access token`,
  `… personal access token`, `… workload identity`, and two Amazon Bedrock forms; all
  of them are refused.
- **The probe does not see environment keys.** With `OPENAI_API_KEY` or
  `CODEX_API_KEY` set, `login status` still says `Logged in using ChatGPT`. The scrub is
  the only thing that keeps those out of a job, exactly as with `ANTHROPIC_BASE_URL` on
  the Claude side. (`CODEX_ACCESS_TOKEN` is read even by `login status`.)
- **The model in `~/.codex/config.toml` may not be usable.** This machine pins
  `gpt-5.4`, and the job failed in ten seconds, nothing spent, with *"The 'gpt-5.4'
  model is not supported when using Codex with a ChatGPT account."* `generate` stops
  the batch on that error and says to pass `--model`. The ids this plan offers are in
  `~/.codex/models_cache.json`; both runs used `--model gpt-6-luna` ("fast and
  affordable"), which is enough — the model only has to call one tool.
- **Where the image lands.** `$CODEX_HOME/generated_images/<thread id>/exec-<uuid>.png`,
  where the thread id is the `thread_id` of the stream's first event
  (`{"type":"thread.started",…}`). `generate` reads that folder
  (`harvested_from: "codex_home:thread"`), which is exact at any `--concurrency`.
- **The `--json` stream carries no image events.** Only `thread.started`,
  `turn.started`, `item.started` / `item.completed` (`agent_message`,
  `command_execution`, `error`), `error`, `turn.failed` (`error.message`) and
  `turn.completed` (token usage). No `saved_path` ever appears in it.
- **Do not ask the model to save the file.** The first run's prompt did. The tool hands
  the model image data with no path, the sandboxed shell does not see `CODEX_HOME`, and
  writing 1.8 MB of base64 through a command line fails with Windows error 206 — so it
  **generated the image four times** hunting for a way (274 s, four images of plan
  usage) and saved nothing. The contract is now "call the tool exactly once, do not
  save or resize, run no shell command", the sandbox is `read-only`, and the second run
  made one image in 85 s. A row with `images_generated > 1` means a model ignored that:
  say so, because it cost that many images.
- **Time.** About 85 s for one image at one generation. `--estimate` assumes 2 minutes
  until a project's own `usage.jsonl` has a measurement.
- **Output size.** Roughly 1350 px on the long side from a 250 px original, with the
  original's proportions kept to within a pixel. `apply` downscales to 1024 px.
- **Quality, from two images only.** The restoration was faithful. The translation
  spelled all eight labels correctly, merged each two-line diagonal label into one
  word as instructed, and kept the line work — including which three diagonals are
  dashed, which was only checkable against a 4x enlargement of the 250 px original.
  Enlarge a small original before judging "same picture?"; at native size you cannot.

- **Backfill.** Gutenberg hosts an `NNN_l.gif` behind every thumbnail of ebook 12228.
  The dry run on the scratch copy found 80 linked scans for 87 images (the other seven
  were already the large files). 75 measured 0.99 or better against their thumbnail.
  Two were **crossed on Gutenberg's side** — `005.jpg` (the star chart) links to the
  compass and `006.jpg` to the star chart — and were relinked to each other's scan.
  `036.jpg` (0.82) and `042.jpg` (0.87) are the same plates re-proportioned and went in
  with `--accept`. `084.jpg` (the huts) links to the oasis from `019`, and so does the
  unlinked `084_l.jpg`: Gutenberg has no larger scan of it. `images/` went from 2.3 MB
  to 8.6 MB. GIF-to-JPEG at quality 90 moved pixels by 1.1 grey levels on average.

Not yet seen: a usage-limit error (so the usage-limit stop is tested only against a
fake), a `cover` or `replace` job, a label with an accent or `ñ`, and
`--concurrency` above 1 against the real CLI.

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths for anything under a book.
- **`python -X utf8` on every Python you run.** Windows stdout defaults to cp1252,
  which mangles every accent in a label map.
- `.harness/images/` layout: `inventory.json`, `manifest.json`, `usage.jsonl`,
  `images.jsonl` (the ledger), `review.html`, `backfill/` (the fetched scans), and
  `jobs/<id>/` holding `job.json`, `prompt.txt`, `cand_NN.png`, `run_NN/` and
  `previous/`.
- The prompt templates are `prompts/image_pass/{translate,restore,cover,replace}.txt`.
  The output contract appended to each (built-in tool only, one call, save nothing) is
  in `src/image_pass/jobs.py`, because the harvest depends on it.
- `generate` copies each result out of `$CODEX_HOME/generated_images/<thread id>/` and
  leaves the source there (about 1.8 MB an image). Nothing in this skill deletes from
  Codex's own folder; clearing it is the user's call.
- `images.jsonl` is append-only. "Durable" means *survives the next run*, not
  *survives a re-clone* — the same is true of `images_original/`. A book whose
  originals matter beyond this checkout can always be re-ingested from its source URL.
- A job sees one file. A picture the importer brought in as two halves has to be
  merged before an image pass, not by it.
- Requires `Pillow` (`pip install -r requirements.txt`).

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/image-pass/`. When the
user asks for a friction log — or a run wasted significant usage or operator time and
they would plausibly want one — invoke the **`friction-log`** skill rather than
hand-rolling the file.
