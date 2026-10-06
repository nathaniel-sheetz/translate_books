---
name: image-pass
description: |
  Redo a book's images headlessly: translate the lettering on maps and diagrams,
  clean up scans, make cover art, or replace an illustration. Inventories the
  images a book references, runs Codex on a ChatGPT subscription to produce
  candidates, shows every image on one board in the web UI where the user corrects
  the triage and picks, and swaps the approved one in under the original filename
  with a backup. Subscription-only and fail-closed, no dollars.
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
CLI — **`scripts/image_pass.py`** — with eleven subcommands that each print JSON.

**Division of labour: you look, Codex draws, the user picks.** You `Read` each image
and write its job. Codex only makes pixels. Nothing reaches `images/` until a human
has seen the candidate beside the original. Three STOP gates sit on that path and
none of them is optional.

## The board

The user's side of a run is one page in the web UI: **`/image-pass/<slug>`**. It
lists every image the book references, grouped by where it stands — to review,
proposed, waiting for candidates, replaced, awaiting triage, leave alone — with
thumbnails for the ones that need nothing yet and full cards for the rest. It is
filterable, it shows your triage and your check of each candidate, and it is where
the user answers you:

- **Before a job exists:** change a verdict ("triage missed this one"), write how
  they want an image done, correct or add to the label map, ask for more candidates.
- **Once candidates exist:** accept one, send the image back with a note, or skip it.

Everything typed there is saved to `.harness/images/feedback.json`, and you read it
back with `board`. So at each gate you **give the user the board's `url` and end the
turn**; when they come back, `board` tells you what they asked for. The chat is for
what the page cannot carry — the usage quote, a judgement call you want to raise.

The page needs the web UI running (`python web_ui/app.py`). `board` reports
`server_running: false` when it is not: say so and ask the user to start it, rather
than handing over a link that will not open.

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

`python scripts/image_pass.py <inventory|backfill|triage|board|prepare|generate|check|composite|apply|revert|verify>`
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

# 2. Record what you made of each image you looked at. It shows on the board.
python scripts/image_pass.py triage --project home-geography \
    --json-file projects/home-geography/.harness/images/triage_rows.json [--replace]

# 3. Where the board is, the candidates to look at, and what the user said on it.
python scripts/image_pass.py board --project home-geography [--base-url http://127.0.0.1:5000]

# 4. Validate jobs and render one prompt each. Spends nothing.
python scripts/image_pass.py prepare --project home-geography \
    --json-file projects/home-geography/.harness/images/jobs.json [--replace]

# 5. Estimate, then run. One Codex process per candidate.
python scripts/image_pass.py generate --project home-geography --estimate
python scripts/image_pass.py generate --project home-geography \
    [--concurrency 3] [--target-ids 006.jpg,cover.jpg] [--model <id>] \
    [--cli-bin <path>] [--timeout-minutes 20]

# 6. Record what you found in each candidate. It shows under it on the board.
python scripts/image_pass.py check --project home-geography \
    --json-file projects/home-geography/.harness/images/check_rows.json

# 6b. A candidate right where it was meant to change and wrong elsewhere: keep
#     another picture's pixels and let in only the patch. No Codex, no spend.
python scripts/image_pass.py composite --project home-geography \
    --json-file projects/home-geography/.harness/images/composites.json [--dry-run]

# 7. The writer. Back up, convert to the original's name and format, replace.
#    --from-board applies the picks the user made on the page.
python scripts/image_pass.py apply --project home-geography --from-board --dry-run
python scripts/image_pass.py apply --project home-geography --from-board [--max-side 1600]

# 8. Undo, and audit.
python scripts/image_pass.py revert --project home-geography --images 006.jpg   # or: all
python scripts/image_pass.py verify --project home-geography
```

An image is named by its path under `images/`: `006.jpg` and `images/006.jpg` are the
same image everywhere a command takes one.

### Triage (`triage --json-file`)

```json
[
  {"image": "042.jpg", "verdict": "translate",
   "finding": "The map lesson's map. 16 labels, all legible at 614 px.",
   "labels": {"Tributary": "Afluente", "RIVER": "RÍO", "CITY": "CIUDAD"}},
  {"image": "085.jpg", "verdict": "restore",
   "finding": "No lettering. Muddy halftone, the poorest reproduction in the book."},
  {"image": "032.jpg", "verdict": "leave",
   "finding": "L-shaped block cut for text wrap. Artist signature (H. Hamilton): keep."}
]
```

| Field | |
|---|---|
| `image` | required, and one the book references |
| `verdict` | `translate` · `restore` · `cover` · `replace` · `leave` |
| `finding` | required. One or two sentences: what is in the picture and why this verdict. The user reads it on the card |
| `labels` | every piece of lettering you could read → its replacement, **in the order you read it off the picture**. The board shows it as a table the user can edit |
| `lettering` | `true` for a picture with lettering you are *not* proposing to translate (it then answers to the "Has lettering" filter). Defaults to whether `labels` is given |

All-or-nothing like `prepare`, and merged by image: a second look at three images
leaves the rest alone. An image with no row shows as **awaiting triage**.

### Checks (`check --json-file`)

```json
[
  {"image": "042.jpg", "candidate": 1, "ok": false,
   "finding": "The acute of OCÉANO sits on the C."},
  {"image": "042.jpg", "candidate": 2, "ok": true,
   "finding": "All 16 labels right. A stray tick beside the stream below Afluente."}
]
```

`finding` is required when `ok` is false. A check is stored against the candidate
file's hash, so when that number is generated again the finding goes with the old
picture and the new one shows as unchecked (`counts.unchecked_candidates`).

### Composites (`composite --json-file`)

```json
[
  {"image": "illus59.jpg", "from": 1, "base": "original", "scale": 2, "feather": 1,
   "regions": [[[341, 17], [362, 0], [434, 0], [434, 13], [372, 22], [350, 33]],
               [396, 35, 422, 47]],
   "note": "Poster lettering only; the crowd is the publisher's."},
  {"image": "illus4.jpg", "from": 2, "base": "previous/20261006_011040/cand_01.png",
   "regions": [[231, 819, 348, 867]], "candidate": 5}
]
```

| Field | |
|---|---|
| `image` | required, and one with a prepared job |
| `from` | required. The candidate the patch comes from: its number, or a path under the job's `previous/` folder |
| `base` | what everything outside the outlines comes from: `original` (default), `current`, a candidate number, or a `previous/...` path (a candidate an earlier `prepare` archived) |
| `regions` | required. Each one `[x0, y0, x1, y1]` or a list of `[x, y]` corners, **in the base picture's pixels** |
| `feather` | how soft the join is, in base pixels (default 2, 0-20) |
| `scale` | 1-4: enlarge the base first, so new lettering a few pixels high keeps its detail. The output is that many times the base's size |
| `candidate` | an existing composite's number, to make it again in place after moving an outline. Never a candidate Codex drew |
| `note` | kept in the composite's description |

All-or-nothing. A composite is an ordinary candidate, numbered from 5 so it never
fills a slot `generate` owes: `check` it, the user picks it, `apply` lands it. Beside
it sits `cand_NN.composite.json`; the board reads that to say what the candidate is
made of and to draw the outline over it in the full-size view (`O` toggles it).

Each region reports `offset` (how far the patch was slid to where the drawing around
the outline lines up) and `surround_difference` (the mean grey-level difference left
in that band). Two warnings matter: `surroundings_differ` (the band does not match:
look at the join) and `alignment_at_edge` (the best position was the furthest one
tried).

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

### Decisions (`apply --from-board`, or `--json-file`)

The user's picks on the board are the decisions: `apply --from-board` reads the ones
not yet applied. `board` lists them first under `picks`, in the same shape a
hand-written file takes:

```json
[
  {"image": "006.jpg", "candidate": 2},
  {"image": "031.jpg", "verdict": "redo", "note": "still foxed lower left"},
  {"image": "cover.jpg", "verdict": "skip", "note": "keeping the current one"}
]
```

`--json-file` still takes that list, for a decision the user gave you in the chat
instead. Never write one from your own reading of the candidates.

A pick is tied to the picture it was made on. If the job was re-prepared or the
candidate regenerated since, `--from-board` refuses it (`refused`, "pick again on the
board") rather than landing a picture the user never saw.

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

Then record what you saw, one `triage` row per image you looked at, and run `board`.
Do this *after* `backfill` on a Gutenberg book (1b): a verdict reached from a
thumbnail is the one most likely to be wrong.

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

The proposal is the triage, and the user reads it on the board: give them `url` from
`board`, say in a line or two what you propose (how many to translate, restore,
leave) and anything you could not read, and **end the turn**.

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
the user's eyes at G3. The map lives in the triage row, where the board shows it as
an editable table. `AskUserQuestion` may pick *which images* to do; it must never be
where a label map lives — the widget truncates.

When the user comes back, run `board` and read `requests` — each is something they
set on the page that no prepared job says yet:

| Reason | Do |
|---|---|
| `needs_job` | They want this image done (often one triage left alone). Look at it again, with their `note`, and write the job |
| `labels_differ` | Use their `labels` **verbatim** in the job. Never merge them with yours |
| `mode_differs` · `candidates_differ` | Change the job to match |
| `note_newer_than_job` | Carry the note into the `instruction` and re-prepare |
| `job_unwanted` | They said leave it alone. Drop the job (`prepare --replace` with the rest) |
| `note_unaddressed` | A note on an image with no job and no verdict: look again, then `triage` it |

A `null` field in a request means they did not change it. An image they did not
touch stands as triaged.

Then `Write` `projects/<slug>/.harness/images/jobs.json` — one job per image under
`proposed`, which already reflects what the user changed: an image they set to leave
alone is not in it, and one they added is, marked `by: "user"`. Run `prepare`, then
`board` once more: `requests` should now be empty, and anything still listed is
something the jobs do not yet say.

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

- **Propose `--concurrency` at G2**, as many as there are candidates up to 3, and
  quote the minutes that gives (`plan.estimated_minutes` already divides by it). The
  flag defaults to 1; the user of this repo asked for parallel runs (2026-10-06)
  after thirteen sequential ones took 29 minutes of generation and 1-2% of a 5-hour
  window. More than one has **not yet been run against the real CLI**: the harvest
  is by thread id, which is exact at any concurrency, but the first parallel batch
  is the test. Say so at the gate, and afterwards check that every candidate is the
  right picture for its job before anything else.
- A usage-limit error **stops the batch** (`counts.not_run`) instead of failing every
  remaining candidate against it. Relay the `error`.
- **Re-running `generate` is the recovery.** It fills only the candidates still
  missing. Never re-`prepare` to recover.
- `status: partial` means some landed. Each `failed` row carries its own error;
  `jobs/<id>/run_NN/events.jsonl` holds that run's raw Codex event stream.

### 4. `board`, look again, and `check`

```bash
python scripts/image_pass.py board --project home-geography
```

`jobs` lists each candidate's `path` beside its `original`. `Read` every one
yourself and record what you find with `check` **before** asking for picks, so the
user sees your finding under the candidate it is about:

- **Lettering.** Check each label against the map, letter by letter. Image models
  drop accents, turn `Ñ` into `N`, double a letter, or leave one English label behind.
- **Artwork.** In `translate` and `restore` the picture must be the same picture. A
  redrawn coastline or a river that moved is a failure even if the lettering is
  perfect — on a map it is a factual error.
- **Proportions.** `flags` already names a candidate whose aspect ratio is off the
  original's by more than 5%.

Say what you could not judge — in the check's `finding`, with `ok: false` if that
leaves the candidate unverified. A 250-pixel original gives you little to compare.

### 5. STOP — G3: the pick gate

Per image the user chooses: **accept candidate N**, **send back with a note**, or
**skip**. The candidates are pictures, so the user decides on the board, not from
your description of it: clicking a picture there opens it full size, and the arrow
keys swap original and candidate in place. Give them the `url`, summarise your
checks in a line or two, and **end the turn**.

When they come back:

```bash
python scripts/image_pass.py board --project home-geography      # picks, stale_picks
python scripts/image_pass.py apply --project home-geography --from-board --dry-run
python scripts/image_pass.py apply --project home-geography --from-board
```

`--dry-run` first, always: it writes nothing and reports `planned` with each
`backup_action` and any `warnings` (`aspect_changed`). Relay those, then run it live.
An image in review with no pick is undecided, not skipped: say which are left.

A `redo` is a new job: change the `instruction` (carry the user's note into it), re-run
`prepare` for that image, and go back to G2 for the extra runs.

**Before a second redo, ask whether a `composite` would do.** The image tool redraws
the whole picture every time, so a redo fixes what it was told and breaks something
else, and two kinds of fault no instruction reaches at all: a shaded drawing that
comes back re-rendered, and a very wide strip that comes back padded. If some
candidate already has the wanted lettering right, keep the pixels that are trusted
and let in only the patch:

1. Pick the base: `original` when the artwork must stay the publisher's; an earlier
   candidate (often one in `previous/`) when it is right everywhere but one label.
2. Find the outline. Crop the base and the candidate at 6-10x with a pixel grid drawn
   on them and read the corners off. The outline must cover the old lettering *and*
   the new, and should cross only what both pictures draw the same way (a road, a
   letter both have) or blank paper.
3. `composite --dry-run`, read `offset` and the warnings, then run it, then `Read`
   the result at the join enlarged. Move a corner and remake it with `candidate: N`.
4. `check` it and give the user the board. It costs no usage, so no G2.

A re-`prepare` archives a job's candidates, composites included. To let the user pick
an archived one again, composite it (`base: previous/...`) rather than moving files.

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

## What live runs established (codex-cli 0.157.0, Windows, 2026-10-05)

All on a scratch copy of `home-geography`: two first runs from the 250 px thumbnails,
then, after `backfill`, sixteen more from the larger scans — seven images with
lettering translated, one restore, and a cover on two models. Update this section when
a later CLI behaves differently.

### The CLI

- **Login probe.** `codex login status` writes one line to **stderr**:
  `Logged in using ChatGPT`, exit 0. Logged out: `Not logged in`, exit 1. The binary's
  other lines are `Logged in using an API key - <masked>`, `… access token`,
  `… personal access token`, `… workload identity`, and two Amazon Bedrock forms; all
  of them are refused.
- **The probe does not see environment keys.** With `OPENAI_API_KEY` or
  `CODEX_API_KEY` set, `login status` still says `Logged in using ChatGPT`. The scrub is
  the only thing that keeps those out of a job, exactly as with `ANTHROPIC_BASE_URL` on
  the Claude side. (`CODEX_ACCESS_TOKEN` is read even by `login status`.)
- **A listed model is not necessarily a usable one.** The pin in
  `~/.codex/config.toml` was `gpt-5.4`, and the job failed in ten seconds, nothing
  spent, with *"The 'gpt-5.4' model is not supported when using Codex with a ChatGPT
  account."* `gpt-6.1-sol` failed the same way although `~/.codex/models_cache.json`
  listed it: the cache was stale, and its next refresh dropped the id. `generate` stops
  the batch on that error and says to pass `--model`. The only proof an id works is one
  turn on it. `gpt-6-luna` ("fast and affordable") ran every job here and is enough —
  the model only has to call one tool. `gpt-6-sol` is accepted too.
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
  save or resize, run no shell command", and the sandbox is `read-only`. Every run
  since has made exactly one image. A row with `images_generated > 1` means a model
  ignored that: say so, because it cost that many images.
- **Time.** Sixteen runs, none failed: 56-83 s for an ordinary image (median 72 s) and
  144-216 s for the map with sixteen labels. `--estimate` uses the project's median
  once it has one, so it **under-quotes a dense image by about three times** — say so
  at G2 when a job has many labels.
- **Output size.** 1200-1440 px on the long side for an edit, with the input's
  proportions kept to within 0.1%; 1024 x 1536 for a cover. The paper comes back white
  to within two grey levels. `apply` downscales an edit to twice the original's long
  side, never below 1024 px.
- **`usage.jsonl` does not record the model.** When two models fill candidates of one
  job, write which is which in the decision's `note`.
- **No usage limit was hit** in the sixteen runs after `backfill`, about 27 minutes of
  generation in one afternoon on this plan.

### Translating lettering

Seven images, eleven runs. Every word that was asked for was eventually spelled right,
accents included (`Ó`, `Í`, `É`, `Ñ`, `í`). What went wrong, and the instruction that
fixed it:

- **Tiny labels lose strokes.** Five grain names a few pixels high came back as `IRROZ`
  and `CENT:NO`. Spelling the two out letter by letter, and allowing a label to be
  slightly larger than the original, fixed both in one run.
- **An accent can land on the wrong letter.** One of two map candidates put the acute
  of `OCÉANO` on the `C`. Look at where each accent sits, not only whether it exists.
- **Fragments of the English are left behind**, most often where the replacement is
  shorter: the tail of the `y` in `Tributary` stayed as a hook on the stream, and the
  capital of `Island` stayed beside `Isla`. Lead the instruction with "erase the whole
  of the old word, every letter and every tail of it, and redraw what lies behind it
  before setting the new word", then name any place it already failed.
- **Type drifts toward one style.** Left to itself the model set most of a map in
  italic serif and made light capitals bold. Describing the original type group by
  group ("CITY: light upright sans-serif capitals; Road, Bridge, Cape: small upright
  roman, not italic; …") held on the next run.
- **A label too close to the border is clipped.** Say where it must start.
- **Rotated lettering follows the reader, not the picture.** On a compass dial whose
  letters face the centre, `SW → SO` came back in page reading order. "Each W becomes
  an O in exactly the spot that W occupies; no other letter moves" got it right.
- **A redo fixes what it was told and slips somewhere new.** The map took three
  attempts. On a redo of an image with many labels, ask for two candidates: the third
  attempt's pair had one clean and one with the misplaced accent.
- **A much longer replacement fits** when told how: `CALLEJÓN DE LOS CIRUELOS` went
  into the strip that held `PLUM LANE` with "tighten the letter-spacing or reduce the
  size so it fits within the strip".

Two of those faults were found by the user, not by the first look at the candidate.
Before reporting, enlarge **every** label and its surroundings four or five times
beside the same crop of the original; at page size a clipped letter or a stray hook
does not show. `inventory.picture_similarity(original, candidate)` is a quick check
that the artwork did not move — the engravings scored 0.96-1.0 — but a diagram that is
mostly lettering scores low (0.86) for the right reasons.

### Restoring

- From a 250 px thumbnail, "clean scan noise only" changed a dial numeral from `90`
  to `270`: with nothing to read, the model wrote what a compass ought to say.
- From the larger scan, with every numeral and letter named and declared untouchable,
  the dial came back intact but the whole engraving was re-inked heavier and more
  regular. It is a redraw. After `backfill` a Gutenberg scan is already clean, and a
  restore has little left to do; say so before proposing one.

### Covers

- **A cover from one of the book's own pictures** takes `"reference": "052.jpg"`. The
  result is a recomposed picture of the same scene, not a crop: islands and houses
  were added to fill the taller shape.
- **Name the medium by its mechanics.** "Colourised but keeping the pencil style"
  produced a coloured-pencil drawing. "A hand-coloured wood engraving: all the drawing
  and shading in black engraved line, thin flat transparent tints over it, shading
  only from the density of the lines; not coloured pencil, not a painting" produced
  one, on both `gpt-6-luna` and `gpt-6-sol`. One sample each: the two were closer to
  each other than to anything else, and the difference cannot be put down to the model.
- **This house's covers are art only.** The finished covers in other projects carry a
  typeset band (title, author, translator) across the top, deeper on the left, and a
  round emblem in the bottom-right corner, both added outside this repo. So ask for no
  lettering, sky and far hills in the top third, and plain ground in the bottom-right
  corner. Confirm with the user before putting a title in the picture.

### Shaded drawings, wide strips and many labels (stormy-misty-s-foal, 2026-10-06)

Six images, thirteen runs on `gpt-6-luna` (one on `gpt-6-sol`), each one image,
51-165 s (one 481 s), no usage limit. Four landed from generation; two needed
`composite`.

- **The tool redraws; it does not edit.** Line work on white paper came back the same
  drawing (similarity 0.997-0.998). Soft pencil shading did not: clouds, a shed roof
  and coats came back with a fine swirling texture, and loosely sketched faces came
  back sharper. An instruction naming exactly that ("no new texture, do not define
  any face") changed nothing, and neither did `gpt-6-sol` on the same prompt. Say so
  before proposing a `translate` on a shaded drawing, and plan on a composite.
- **A very wide strip comes back padded.** A 650 x 155 original (4.19 to 1) came back
  2170 x 725 twice, the drawing in rows 104-621 between white bands, whatever the
  instruction said. `composite` fits such a candidate centred, so its patch still
  lands where it belongs in the original.
- **Lettering comes back as a font unless told otherwise.** "Hand-painted" was not
  enough. Describing the strokes was: "thin, light, loosely printed pen capitals,
  each stroke a single thin line, some letters leaning", "heavy capitals brushed by
  hand, strokes of uneven thickness, rough ends, letters of slightly different
  sizes", plus "never clean, even or typeset".
- **A map of thirty-one labels is a lottery per attempt.** Four candidates over two
  prompts: one right in every label, one with the island's own name garbled into
  `ISLA DE / ISLADE ISLA`, one with `CALZADA` missing, one with a line drawn across
  open water where the tail of an erased word had been, and a vertical label turned
  to read downward. Read every label of every candidate; a count is not a check.
  When one is right but for a label, composite that label in instead of redoing.
- **Labels mapped to themselves are redrawn too**, crisply, so a reading that was a
  guess at 4 px comes back looking certain. Say which those were in the check.
- **An artist's own slip is copied faithfully.** A hand-drawn `G` that reads as `Q`
  in the original came back as a `Q` in the Spanish. Name the letter and spell the
  word out when the original's lettering is itself ambiguous.
- **No larger scan is not the same as not having looked.** `backfill` reports what
  the HTML links. For ebook 67298 the directory listings
  (`/files/<id>/<id>-h/images/`, `/cache/epub/<id>/images/`, and `/files/<id>/old/`)
  held the same files at the same sizes, byte for byte. Check them before saying so.
- **The estimate under-quotes dense images**: the median run was 97 s, the map 165 s
  a candidate, and one map run took 481 s.

### Backfill

- Gutenberg hosts an `NNN_l.gif` behind every thumbnail of ebook 12228. The dry run
  found 80 linked scans for 87 images (the other seven were already the large files).
  75 measured 0.99 or better against their thumbnail.
- Two were **crossed on Gutenberg's side** — `005.jpg` (the star chart) links to the
  compass and `006.jpg` to the star chart — and were relinked to each other's scan.
- `036.jpg` (0.82) and `042.jpg` (0.87) are the same plates re-proportioned and went in
  with `--accept`. `084.jpg` (the huts) links to the oasis from `019`, and so does the
  unlinked `084_l.jpg`: Gutenberg has no larger scan of it.
- `images/` went from 2.3 MB to 8.6 MB. GIF-to-JPEG at quality 90 moved pixels by 1.1
  grey levels on average.
- It changed the triage. At 216 px half the map's labels could not be read, by the
  agent or by Codex; at 614 px all sixteen could. Run it before reading the images.

Not yet seen: a usage-limit error (so the usage-limit stop is tested only against a
fake), a `replace` job, a cover with lettering on it, a lower-case `ñ` or an inverted
`¿` / `¡`, and `--concurrency` above 1 against the real CLI (asked for; see G2).

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths for anything under a book.
- **`python -X utf8` on every Python you run.** Windows stdout defaults to cp1252,
  which mangles every accent in a label map.
- `.harness/images/` layout: `inventory.json`, `triage.json`, `manifest.json`,
  `checks.json`, `feedback.json` (the user's requests and picks — written only by the
  board, never by you), `usage.jsonl`, `images.jsonl` (the ledger), `backfill/` (the
  fetched scans), and `jobs/<id>/` holding `job.json`, `prompt.txt`, `cand_NN.png`
  (with a `cand_NN.composite.json` beside a composite), `run_NN/` and `previous/`.
- **A note typed on a card with no pick is not saved.** The board attaches a note to
  a verdict, so "send both back with my notes" reaches `board` only after the user
  clicks *Send back* on each card. If `picks` comes back empty, say that rather than
  writing the note for them.
- **The web UI has to be restarted to pick up a change under `src/` or `web_ui/`.**
  The script is served fresh, but the board's state is built by code the running
  process loaded at start.
- **Never edit `feedback.json`.** A request stops being outstanding when the prepared
  job says the same thing, and a pick when `apply` records it; nothing is ticked off
  by hand.
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
