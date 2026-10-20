---
name: kdp-publish
description: |
  The last mile to a Kindle listing for a finished translation: gather what is known
  about the book and this edition, research Amazon.com for the categories comparable
  Spanish books rank in and the phrases shoppers actually search, draft a KDP-compliant
  listing (publishing rights as public domain or owned copyright, title, Spanish
  description, 7 keywords, 3 categories, reading age, AI disclosure, territories),
  and, after approval, write
  the EPUB-side metadata, title page and copyright page so the next build carries them.
  No API spend: the research is scripted HTTP and a headless browser.
  Use when asked to "publish to KDP", "prepare the KDP listing", "pick categories and
  keywords", "write the Amazon description", "fill in the epub metadata", or "kdp-publish".
allowed-tools:
  - Bash
  - Read
  - Write
  - WebSearch
  - WebFetch
  - AskUserQuestion
---

# kdp-publish

You do the judgment work: positioning, keyword seeds, the description, choosing among
the categories. **`scripts/kdp_publish.py`** does everything deterministic and every
fetch from Amazon, and returns only the extracted fields. Do not open Amazon pages
yourself (no WebFetch of amazon.com, no browser skill): one listing page is tens of
thousands of tokens of markup, and the script reads it for about a hundred. One STOP
gate (G1) sits before anything is written to the book.

## What this writes

Only `apply` writes, and only after G1:

- **`project.json`**: `spanish_title`, `subtitle`, `author`, `translator`, `publisher`,
  `description` (plain text), `rights`, `subjects`, `original_year`,
  `publication_date`, `front_matter`. `src/book_metadata.py` reads these on **every**
  build path (web UI, `harness.py epub`, `build_epub.py`, the corrections and
  `translate_book.py` rebuilds).
- **`edition_notice.json`** (`{heading, body}`): the copyright page, placed after the
  generated title page.
- **`kdp_listing.json`**: the approved listing plus `kdp_form`, the KDP fields in page
  order. This is what the user copies into KDP.

**This is not `harness.py epub`.** That builds the file. This decides what the file
and the store page say. Run it before the final build, and after `image-pass` and
`footnote-pass`, because the facts count the new cover and the notes.

## The CLI (read first)

`python -X utf8 scripts/kdp_publish.py <command> --project <slug>` prints one JSON
object and mirrors it to `projects/<slug>/.harness/kdp/last_output.json`. **Read that
file** rather than piping stdout into another interpreter.

```bash
python -X utf8 scripts/kdp_publish.py facts       --project <slug> [--no-fetch]
python -X utf8 scripts/kdp_publish.py suggest     --project <slug> --seeds-file <file> [--expand]
python -X utf8 scripts/kdp_publish.py comps       --project <slug> --query "<q>" [--query ...] [--asin B0...] [--max 6]
python -X utf8 scripts/kdp_publish.py category    --project <slug> --node <id> [--node ...] [--sample 1,10,20]
python -X utf8 scripts/kdp_publish.py check       --project <slug> [--listing-file <path>]
python -X utf8 scripts/kdp_publish.py report      --project <slug> [--listing-file <path>]
python -X utf8 scripts/kdp_publish.py apply       --project <slug> [--listing-file <path>] [--dry-run]
python -X utf8 scripts/kdp_publish.py verify-epub --project <slug> [--epub <path>]
```

The working files live in `.harness/kdp/`: `facts.json`, `seeds.txt`,
`suggestions.json`, `comps.json` (accumulates across runs; `--reset` starts over),
`categories.json`, and `listing.json` (yours: the default `--listing-file`).

`comps` and `category` need Playwright, which is optional:
`pip install -r requirements-kdp.txt && python -m playwright install chromium`. Without
it they exit with that hint. Fall back to WebSearch for comparable editions and mark
the category evidence **inferred** in the listing's rationale.

## Flow

### 1. Facts

```bash
python -X utf8 scripts/kdp_publish.py facts --project <slug>
```

Read `.harness/kdp/facts.json` in full. It holds the following:
- **Titles:** every title the book carries, including the cover's title/subtitle
  split in `titles.cover_*`, which is usually the right KDP Title/Subtitle split.
- **Catalog:** the Gutenberg catalog record (LCSH subjects, LCC, bookshelves, an
  automatic summary, reading ease, everyone credited with birth and death years).
- **Edition:** what this edition adds (`footnotes.editorial`, `images.*`), the
  suggested descriptor, and a draft copyright page (`edition.notice_draft`).
- **AI:** what was made with AI (`ai`).
- **Rights:** `rights_flags`.

Settle every `needs_input` item before drafting:
- **`original_year`**: first publication of the *original* work. The candidates can
  be a translation's year (Bambi's 1928 is the Chambers translation; the novel is
  1923). Research it, then confirm it with the user.
- **`translator`**: `project.json` and the house default disagree. `apply` writes the
  listing's Translator contributor, which the draft takes from the default. Ask if it isn't obvious.

**Settle Publishing Rights.** `facts.publishing_rights_default` is the house answer:
- **`public_domain`**: "This is a public domain work". The title carries the
  descriptor and the description opens with KDP's bullet summary. KDP Select and the
  70% royalty are not available.
- **`own_copyright`**: "I own the copyright". No descriptor and no required bullets;
  the book can go into KDP Select. KDP's own help files a translation of a
  public-domain work under public domain, so this is a position the user takes, and
  the listing must say what it rests on (`copyright_basis`). Show the user
  `facts.authorship` (how much of the final text differs from the model's drafts, and
  how many sentence edits they saved) before they choose. The claim is strongest
  when the book carries original content the translator wrote: an introduction,
  study or narration questions, a teaching guide.

Confirm the answer for this book with the user; don't carry it over silently.

**Read `rights_flags` aloud to the user.** They are not boilerplate. A source edition
whose translator or illustrator died within seventy years (`life_plus_70`) is in
copyright across most of Europe, and `mexico_life_plus_100` matters because Mexico is
the readership. They decide the listing's `territories`.

### 2. Positioning

From the catalog subjects, the style guide's register (`language.style_excerpt`),
`readability`, and the book itself (read a chapter opening if needed), decide:
- **Genre and themes.**
- **Audience and reading age.** KDP's 0–18 range; leave it blank for an adult book.
  Check it against the comps' reading ages once you have them.
- **Two or three BISAC headings.** Their text goes into `epub.subjects` with the
  LCSH headings.

### 3. Evidence

**Seeds.** Write 10–15 Spanish seed phrases to `.harness/kdp/seeds.txt`:
- the Spanish title, and the author in the form Spanish readers search
- genre + audience ("libros de animales para", "novelas para")
- "libros en español para"
- themes

Short open-ended prefixes produce more than finished phrases do. Then:

```bash
python -X utf8 scripts/kdp_publish.py suggest --project <slug> --seeds-file projects/<slug>/.harness/kdp/seeds.txt --expand
```

`--expand` sends about 27 requests per seed (around 2 minutes for 10 seeds). A phrase
is kept only if it contains every word of its seed. `kindle: false` means it was seen
only in Amazon's all-department fallback: that is shopper language, not proof of
Kindle demand.

**Comparable books.** Run 2–4 queries: the Spanish title, and genre + "en español"
with the audience.

```bash
python -X utf8 scripts/kdp_publish.py comps --project <slug> --query "bambi una vida en el bosque" --query "novelas clasicas juveniles en español"
```

`kindle_categories` counts the Kindle nodes the Spanish-language comps rank in. Two
details matter:
- **Spanish branch.** Amazon.com has a Spanish-language branch, Kindle eBooks ›
  Foreign Languages › Spanish › {Infantil, Juvenil, Literatura y Ficción, …}, and
  comps rank in it under English names ("Children's in Spanish", node 8407404011).
- **Print nodes.** A `(Books)` rung is a print node, not a Kindle one.

**Categories.** Take the 4–6 strongest candidate nodes:

```bash
python -X utf8 scripts/kdp_publish.py category --project <slug> --node 8407404011 --node 155191011
```

Each node gives its tree path (what KDP's picker shows), its siblings and children
(the alternatives), and the **overall** Kindle Store rank of its #1, #10 and #20 books.
That last number is the demand and competition signal:
- **#20 at overall ~160k:** a few sales a day puts a book on the category's first page.
- **#20 at ~9k:** that page is out of reach.

One category in the Spanish branch, where the book can be seen, plus one or two
accurate English-tree categories is usually right.

### 4. Draft `listing.json`

Read the description examples at `facts.description_examples`: the user's own
`prompts/kdp_description_examples.md`, or the tracked `.example.md` when they have none.
Then write `.harness/kdp/listing.json`:

```json
{
  "language": "Spanish",
  "title": "Bambi", "descriptor": "(Traducido)", "subtitle": "Una vida en el bosque",
  "author": "Felix Salten",
  "contributors": [{"role": "Translator", "name": "<facts.translator.default>"}],
  "series": {"name": "<facts.edition.series, or omit>"},
  "description_html": "<p>Esta edición incluye:</p><ul><li>…≤80 chars each…</li></ul><p>…</p>",
  "publishing_rights": "public_domain|own_copyright",
  "copyright_basis": {"statement": "…own_copyright only: what the copyright covers…",
                      "original_content": ["…what the translator wrote: introducción, preguntas de narración…"]},
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
  "positioning": {"genre": "…", "audience": "…", "bisac": ["…"]},
  "epub": {"subjects": ["…"], "original_year": 1923,
           "edition_notice": {"heading": "Créditos", "body": "…paragraphs separated by blank lines…"}},
  "manual_checks": ["…anything you could not verify…"]
}
```

Rules that `check` cannot fully enforce:
- **Owned copyright.** Omit the descriptor (`"descriptor": ""`). The opening bullets
  are optional, but the house pattern keeps them. `copyright_basis.statement` names
  what is the user's own: the revised translation, the notes, and each piece of
  original content. Never state more human revision than `facts.authorship` shows.
  Original content counts only if a human wrote it, or substantially rewrote a draft.
- **Descriptor** (public domain). It comes from `facts.edition.descriptor`. "Anotado" needs at least
  10 editorial notes; "ilustrado" needs at least 10 *new* illustrations. Translated
  lettering and a new cover count toward neither.
- **House fields.** When `facts.edition.house_subtitle` or `facts.edition.series` is
  set, use it as the Subtitle or Series unless the user says otherwise. Name the
  Spanish as `facts.edition.variety_label` when it is set.
- **Description.** For public domain, it opens with KDP's bullet summary of what is original (at most 80
  characters per bullet, optionally after one short line ending in a colon), then the
  hook, then this edition. It contains no reviews,
  prices, links, emoji, program names or keyword lists, and never the name Project
  Gutenberg, anywhere, including the edition notice.
- **Keywords.** Use the 7 phrases with the best evidence that are not already in the
  title, contributors or chosen categories. Amazon indexes those already.
- **AI disclosure** is what KDP asks on the Content page:
  - A machine translation is **AI-generated translation**, even when edited
    (`facts.ai.translation` names the model).
  - Cover art or image edits made through image-pass are AI-generated images.
- **Edition notice.** Start from `facts.edition.notice_draft` and fix the original year
  and the original-language title.

Run `check` and iterate until it reports no errors:

```bash
python -X utf8 scripts/kdp_publish.py check --project <slug>
```

### 5. STOP, G1: approve the listing

```bash
python -X utf8 scripts/kdp_publish.py report --project <slug>
```

Read the report (`reports/kdp_<stamp>.md`), then show the user, verbatim:
- Book Title, Subtitle and Contributors
- the description
- the 3 categories with their evidence lines
- the 7 keywords
- reading age, territories and AI disclosure
- Publishing Rights, and for `own_copyright` the report's Copyright basis section
- every ❌/⚠️/⚖️ line from Compliance

Give the report path as well. Ask for approval with AskUserQuestion. Apply their edits
to `listing.json` and re-run `check` and `report` after any change.

### 6. Write and build

```bash
python -X utf8 scripts/kdp_publish.py apply --project <slug> --dry-run
python -X utf8 scripts/kdp_publish.py apply --project <slug>
python scripts/harness.py epub --project <slug>
python -X utf8 scripts/kdp_publish.py verify-epub --project <slug>
```

Relay the dry-run's `changes` before the live apply. Pay attention to any `before`
value it overwrites (e.g. a hand-typed `spanish_title`). `apply` refuses while `check`
has errors.

`verify-epub` must come back `ok`. A failed `no_gutenberg_trademark` names the chapter
file that still carries the name. That text is in the book's body and must be removed
from the translation, not from the metadata.

### 7. Hand-off

Give the user the `kdp_listing.json` path and the manual checklist:
- **Descriptor** (public domain): KDP's wording is English; the first review may
  question "(Traducido)".
- **Publishing Rights** (owned copyright): if KDP asks, answer from the report's
  Copyright basis section. Keep `.harness/translate/` drafts and
  `corrections_applied.jsonl`: they are the record of the human work.
- **Categories:** pick them in KDP's picker by matching the `path`. The picker's labels
  can differ from the best-seller names.
- **Territories:** choose individual territories if `rights_flags` call for it.
- **Royalty and Select:** public-domain titles get only the 35% royalty and no KDP
  Select. An owned-copyright title can enroll in Select, which requires the ebook
  to be exclusive to Amazon.
- **Kindle Previewer:** open the EPUB and check the cover, title page and copyright page.

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths.
- **Rerunning is cheap and safe.** `facts`, `check`, `report` and `verify-epub` are
  read-only apart from their own output files. `comps` and `category` merge into
  their files.
- **When Amazon blocks a page:** the session reloads the homepage and retries once,
  then records `error` for that ASIN and moves on. A whole run that comes back blocked
  means slow down (`--delay 3`) or try later. Do not switch to a browser skill.
- **What the suggestion endpoint can and can't do:** it scopes to Kindle only some of
  the time, and a long-tail Spanish phrase may return nothing. Few phrases is a finding
  (thin Kindle demand in Spanish), not a failure.
- **Per-user defaults:** `prompts/kdp_defaults.json` (gitignored; tracked twin
  `kdp_defaults.example.json`) holds the canonical translator name, the imprint, the
  descriptor wording, and the rights and notice templates.
- Engineering reference: `docs/KDP_PUBLISH.md`.

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/kdp-publish/`. When the user asks
for a friction log, or a run wasted significant tokens or operator time and they would
plausibly want one, invoke the **`friction-log`** skill rather than hand-rolling the
file. It owns the location, naming and section skeleton. Those prior logs are also the
best standing record of this skill's known rough edges: read the latest one or two when
a run hits something that feels familiar.
