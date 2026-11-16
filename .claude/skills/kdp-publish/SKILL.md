---
name: kdp-publish
description: |
  The last mile to a Kindle listing for a book whose edition is already written:
  read the book as book-metadata left it, research Amazon.com for the categories
  comparable Spanish books rank in and the phrases shoppers actually search, and
  draft a KDP-compliant listing (publishing rights as public domain or owned
  copyright, the KDP title field, the store description, 7 keywords, 3 categories,
  reading age, AI disclosure, territories, KDP Select). After approval it writes
  kdp_listing.json and verifies the Kindle build. It does not write the book: the
  title page, the copyright page and the book's description belong to
  book-metadata, which runs first. No API spend: the research is scripted HTTP and
  a headless browser.
  Use when asked to "publish to KDP", "prepare the KDP listing", "pick categories and
  keywords", "write the Amazon description", "refresh the Amazon listing", or
  "kdp-publish".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - WebSearch
  - WebFetch
  - AskUserQuestion
---

# kdp-publish

You do the judgment work: positioning, keyword seeds, the store description, choosing
among the categories. **`scripts/kdp_publish.py`** does everything deterministic and
every fetch from Amazon, and returns only the extracted fields. Do not open Amazon
pages yourself (no WebFetch of amazon.com, no browser skill): one listing page is tens
of thousands of tokens of markup, and the script reads it for about a hundred. One STOP
gate (G1) sits before anything is written.

## Where this sits

```
book-metadata: the edition and the site texts → both EPUBs built and verified
  → kdp-publish: the store listing → verify the Kindle build → publish → record the ASIN
```

- **This skill writes the store, not the book.** `apply` writes one file,
  `kdp_listing.json`: the approved listing plus `kdp_form`, the KDP fields in page
  order, which the user copies into KDP.
- **The book comes from `book-metadata`.** The title, subtitle, author, translator,
  copyright page and description are in `project.json` and `edition_notice.json`
  already. The listing has to agree with them, and `check` refuses one that does not.
  To change any of them, run book-metadata, rebuild, and come back.
- **Nothing here needs a rebuild.** The Amazon research is the slow, flaky half, and it
  no longer stands between the user and the EPUB.

## The CLI (read first)

`python -X utf8 scripts/kdp_publish.py <command> --project <slug>` prints one JSON
object and mirrors it to `projects/<slug>/.harness/kdp/last_output.<command>.json`.
**Read that file** rather than piping stdout into another interpreter.

```bash
python -X utf8 scripts/kdp_publish.py facts       --project <slug> [--no-fetch]
python -X utf8 scripts/kdp_publish.py suggest     --project <slug> --seeds-file <file> [--expand]
python -X utf8 scripts/kdp_publish.py comps       --project <slug> --query "<q>" [--query ...] [--asin B0...] [--max 6]
python -X utf8 scripts/kdp_publish.py category    --project <slug> --node <id> [--node ...] [--sample 1,10,20]
python -X utf8 scripts/kdp_publish.py check       --project <slug> [--listing-file <path>]
python -X utf8 scripts/kdp_publish.py report      --project <slug> [--listing-file <path>]
python -X utf8 scripts/kdp_publish.py apply       --project <slug> [--listing-file <path>] [--dry-run]
python -X utf8 scripts/kdp_publish.py verify-epub --project <slug> [--epub <path>]
python -X utf8 scripts/kdp_publish.py published   --project <slug> --asin B0XXXXXXXX [--date YYYY-MM-DD]
```

The working files live in `.harness/kdp/`. Their keys, and everything about the Amazon
research, are in **`references/research.md`**: Read it at step 3. A collection volume
is in **`references/collection.md`**: Read it when `--project` is a collection.

## Flow

### 1. Facts, and the book as it stands

```bash
python -X utf8 scripts/kdp_publish.py facts --project <slug>
```

**`edition_applied` must be true.** When it is false, the book has no translator on
record or no copyright page: stop and run **book-metadata** first. Do not draft the
edition here.

Then read, in full:
- **`.harness/kdp/facts.json`.** What matters here:
  - `book` in the command's output: the title, subtitle, translator, edition version
    and note count the listing must match.
  - `site.on_site`: the book has a folder in the site repo or a libre build. It decides
    KDP Select (below).
  - `existing_listing`: `asin` set means the book is already for sale (below).
  - `catalog`: LCSH subjects, bookshelves, the automatic summary, reading ease.
  - `edition.descriptor`, `edition.additions`: what this edition adds, for the title
    field and the opening bullets.
  - `quotations`: the Bible version, the exact verse counts and `within_limits`. This
    is the copyright analysis for quoted Scripture; don't estimate it by hand.
  - `ai`, `authorship`, `rights_flags`, `publishing_rights_default`.
- **`projects/<slug>/metadata_draft.md`,** book-metadata's draft. Its "What the record
  shows" table is the evidence of what the translator changed and wrote, and its
  `forms` evidence says who the reader is.
- **`project.json` `description`:** the book's description, which the store
  description is built from.

`needs_input` should be empty, because book-metadata settled it. An item still open
(`footnotes`: the note store and the built EPUB disagree; `quotations`) is settled
there, not here.

**Settle Publishing Rights.** `facts.publishing_rights_default` is the house answer:
- **`public_domain`**: "This is a public domain work". The title carries the
  descriptor and the description opens with KDP's bullet summary. Only the 35% royalty.
- **`own_copyright`**: "I own the copyright". No descriptor and no required bullets,
  and the 70% royalty where a territory allows it. KDP's own help files a translation
  of a public-domain work under public domain, so this is a position the user takes,
  and the listing must say what it rests on (`copyright_basis`). Show the user
  `facts.authorship` (how much of the final text differs from the model's drafts, and
  how many sentence edits they saved) before they choose. The claim is strongest
  when the book carries original content the translator wrote: an introduction,
  study or narration questions, a teaching guide.

Confirm the answer for this book with the user; don't carry it over silently.

**KDP Select.** Select requires the ebook to be exclusive to Amazon, and that includes
the publisher's own website. A book on the site is given away there, so it is not
enrolled: with `site.on_site` true, `kdp_select` stays false and `check` refuses
anything else. For a book that is not on the site, ask whether it will be before
offering Select.

**Territories.** `rights_flags` were read aloud in book-metadata; here they decide
`territories`. A source edition whose translator or illustrator died within seventy
years (`life_plus_70`) is in copyright across most of Europe, and
`mexico_life_plus_100` matters because Mexico is the readership. When the user keeps
worldwide sale with a flag standing, record why in `territories.rationale`.
`quoted_text_over_limit` means the quoted Bible text exceeds the version's free terms;
commercial sale then needs the publisher's written approval, so stop and raise it.

Ask rights, Select and territories in one AskUserQuestion, with the authorship numbers
and the flags inside the dialog.

**A book that is already live.** With `existing_listing.asin` set, this run refreshes
that listing:
- Tell the user at once, before any research.
- Keep the live title and the live reading age unless the user wants them changed. A
  changed title on a live book is expensive to undo.
- The hand-off says "edit the existing title", never "create a title".

### 2. Positioning

From the catalog subjects, `readability`, the forms evidence in the draft and the book
itself (read a chapter opening if needed), decide:
- **Genre and themes.**
- **Audience and reading age.** KDP's 0–18 range; leave it blank for an adult book.
  Check it against the comps' reading ages once you have them. `check` warns when it
  does not overlap the site's forms: the two are set separately but describe one
  reader.

The BISAC and LCSH headings are already in the book (`project.json` `subjects`).

### 3. Evidence

**Read `references/research.md` now.** In short:
- Write the seeds and start `suggest --expand` in the background.
- **While it runs, draft `listing.json`** with everything except keywords and
  categories, and run `check`. Never end a turn on a background job.
- Run `comps` only after `suggest` has finished, never at the same time, then
  `category` on the 4–6 strongest nodes.
- A blocked run is recorded as a finding and the evidence marked **inferred**.

### 4. Draft `listing.json`

Read the description examples at `facts.description_examples`: the user's own
`prompts/kdp_description_examples.md`, or the tracked `.example.md` when they have none.
Then write `.harness/kdp/listing.json`:

```json
{
  "language": "Spanish",
  "title": "<the book's title>", "descriptor": "(Traducido)", "subtitle": "<the book's subtitle>",
  "author": "<the book's author>",
  "contributors": [{"role": "Translator", "name": "<the book's translator>"}],
  "series": {"name": "<facts.edition.series, or omit>"},
  "description_html": "<p>Esta edición incluye:</p><ul><li>…≤80 chars each…</li></ul><p>…the book's description…</p>",
  "publishing_rights": "public_domain|own_copyright",
  "copyright_basis": {"statement": "…own_copyright only: what the copyright covers…",
                      "original_content": ["…what the translator wrote: introducción, preguntas de narración…"]},
  "kdp_select": false,
  "primary_audience": {"sexually_explicit": false, "reading_age_min": 10, "reading_age_max": 14},
  "marketplace": "Amazon.com",
  "categories": [{"path": ["Kindle eBooks", "…"], "name": "…", "node": "…", "store": "digital-text",
                  "rationale": "…cite comps and the #1/#20 sample…"}],
  "category_alternatives": [{"path": ["…"], "why": "…"}],
  "keywords": [{"phrase": "…", "evidence": "autocomplete 5x / LCSH … / comps series"}],
  "territories": {"mode": "worldwide|individual", "exclude": ["…"], "rationale": "…"},
  "ai_disclosure": {"text": {"ai_generated": false},
                    "images": {"ai_generated": true, "tool": "…", "detail": "cover"},
                    "translations": {"ai_generated": true, "tool": "…", "detail": "entire work, edited"}},
  "positioning": {"genre": "…", "audience": "…"},
  "manual_checks": ["…anything you could not verify…"]
}
```

There is no `epub` block: the book is not written from here.

Rules that `check` cannot fully enforce:
- **Title, subtitle, author, translator.** Copy them from the book (`facts` → `book`).
  `check` refuses a difference (`edition_mismatch`).
- **Description.** The body is the book's description, the same text the site and
  the EPUB carry; `check` warns when it drifts (`description_diverges`). Add only what
  the store needs: for public domain, KDP's opening bullet summary of what is original
  (at most 80 characters per bullet, optionally after one short line ending in a
  colon); for owned copyright the bullets are optional, but the house pattern keeps
  them. It contains no reviews, prices, links, emoji, program names or keyword lists,
  and never the name Project Gutenberg.
- **Counts.** A note count in a bullet or in `copyright_basis` is the built EPUB's
  (`facts` → `book.endnotes`); `check` refuses another number.
- **Claims.** Any fact in the description beyond the book and its facts was sourced
  in book-metadata's draft. A new one gets one search before G1, and its URL goes in
  `manual_checks`. Never cite from memory.
- **Owned copyright.** Omit the descriptor (`"descriptor": ""`).
  `copyright_basis.statement` names what is the user's own: the revised translation,
  the notes, and each piece of original content, drawn from the draft's "What the
  record shows" table. Never state more human revision than `facts.authorship` shows:
  with no measured share, give the number of sentence edits, not "frase por frase".
  Original content counts only if a human wrote it, or substantially rewrote a draft.
  Name "the translator", not a pronoun.
- **Descriptor** (public domain). It comes from `facts.edition.descriptor`. "Anotado"
  needs at least 10 editorial notes; "ilustrado" needs at least 10 *new*
  illustrations. Translated lettering, a new cover and art repainted from the source's
  pictures count toward neither.
- **House fields.** When `facts.edition.series` is set, use it as the Series unless the
  user says otherwise. Name the Spanish as `facts.edition.variety_label` when it is set.
- **Keywords.** Use the 7 phrases with the best evidence that are not already in the
  title, contributors or chosen categories. Amazon indexes those already.
- **AI disclosure** is what KDP asks on the Content page:
  - A machine translation is **AI-generated translation**, even when edited
    (`facts.ai.translation` names the model).
  - Cover art or image edits made through image-pass are AI-generated images. A book
    older than the image ledger shows `images.models: []`: look for `*old*` siblings
    in `images/` and ask the user what was made and with which tool.

Run `check` and iterate until it reports no errors:

```bash
python -X utf8 scripts/kdp_publish.py check --project <slug>
```

### 5. STOP, G1: approve the listing

```bash
python -X utf8 scripts/kdp_publish.py report --project <slug>
```

Read the report (`reports/kdp_<stamp>.md`), then end the turn with these in your
message, verbatim:
- Book Title, Subtitle and Contributors
- the description
- the 3 categories with their evidence lines
- the 7 keywords
- reading age, territories, KDP Select and AI disclosure
- Publishing Rights, and for `own_copyright` the report's Copyright basis section
- every ❌/⚠️/ℹ️/⚖️ line from Compliance
- the report's path

**Ask for approval in plain text, at the end of that message.** Do not open an
AskUserQuestion dialog here: text printed just before a dialog is hidden behind it.
Apply the user's edits to `listing.json` and re-run `check` and `report` after any
change. A change to the title, the description or a count is made in book-metadata
first.

### 6. Write and verify

```bash
python -X utf8 scripts/kdp_publish.py apply --project <slug> --dry-run
python -X utf8 scripts/kdp_publish.py apply --project <slug>
python -X utf8 scripts/kdp_publish.py verify-epub --project <slug>
```

Relay the dry-run's `changes` before the live apply. They are all in
`kdp_listing.json`; on a refresh they are the difference from the listing approved
last time. `apply` refuses while `check` has errors, and it keeps an ASIN already on
record.

`verify-epub` reads the **Kindle** build, the default `harness.py epub` target, which
book-metadata built, and must come back `ok`. It is the same check book-metadata ran,
with the store's rule added: no live link. The free-library build
(`libre/<site_slug>.epub`) is never uploaded to KDP.
- **`note_counts_agree`** failing means a count on the copyright page or in «Sobre
  este libro» is stale: a note was added after book-metadata. Fix it there.
- **`no_gutenberg_trademark`** names the chapter file that still carries the name. That
  text is in the book's body and must be removed from the translation.
- An EPUB older than the last `book_metadata.py apply` is rebuilt first
  (`python scripts/harness.py epub --project <slug>`).

### 7. Hand-off

Give the user the `kdp_listing.json` path and the manual checklist:
- **A live book:** edit the existing title (`existing_listing.asin`); do not create
  one.
- **Descriptor** (public domain): KDP's wording is English; the first review may
  question "(Traducido)".
- **Publishing Rights** (owned copyright): if KDP asks, answer from the report's
  Copyright basis section. Keep `.harness/translate/` drafts and
  `corrections_applied.jsonl`: they are the record of the human work.
- **Categories:** pick them in KDP's picker by matching the `path`. The picker's labels
  can differ from the best-seller names.
- **Territories:** choose individual territories if `rights_flags` call for it.
- **KDP Select: leave it off** for a book on the site.
- **Royalty:** public-domain titles get only the 35% royalty. An owned-copyright title
  gets 70% within KDP's price band, except in Brazil, Japan, Mexico and India, where
  the 70% rate requires KDP Select: without it those sales pay 35%. Mexico is the
  readership, so say so.
- **Kindle Previewer:** open the EPUB and check the cover, title page and copyright page.

**After the book is live,** record it, so the next run knows:

```bash
python -X utf8 scripts/kdp_publish.py published --project <slug> --asin B0XXXXXXXX
```

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths. A project in a group folder (`projects/.macdonald/…`,
  `projects/.published/…`) resolves by its slug; `ls projects/` does not show it.
- **Start every Bash call from the repo root** (`cd /c/…/translate_books && …`), and
  never send a background job's stderr to `/dev/null`.
- **Rerunning is cheap and safe.** `facts`, `check`, `report` and `verify-epub` are
  read-only apart from their own output files. `comps` and `category` merge into
  their files.
- **No catalog?** `facts` says so in `needs_input: source`. For a Gutenberg book, set
  `project.json` `gutenberg_url` to the ebook's URL and run `facts` again.
- **Per-user defaults:** `prompts/kdp_defaults.json` (gitignored; tracked twin
  `kdp_defaults.example.json`) holds the canonical translator name, the imprint, the
  descriptor wording, and the rights and notice templates. book-metadata reads the
  same file for the edition.
- Engineering reference: `docs/KDP_PUBLISH.md`.

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/kdp-publish/`. When the user asks
for a friction log, or a run wasted significant tokens or operator time and they would
plausibly want one, invoke the **`friction-log`** skill rather than hand-rolling the
file. It owns the location, naming and section skeleton. Those prior logs are also the
best standing record of this skill's known rough edges: **read the latest one in full
before the run**, not only its filename.
