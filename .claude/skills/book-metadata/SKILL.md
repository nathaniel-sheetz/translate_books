---
name: book-metadata
description: |
  Draft everything a finished translation says about itself, before it is posted on
  the site or published: the edition (title page, copyright page, edition version,
  the book's description and subjects), the book page's description and card
  summary, «Sobre esta edición» (what this edition is and what it changed), «Lo que
  los padres deben saber» (the parent notes), the recommended forms, and the EPUB's
  «Sobre este libro» page. Reads the family-review or theology-review, checks every
  flag against the translation (rewritten, noted or unchanged), finds the silent
  changes, counts the notes in the built EPUB, and lists the gaps a small edit would
  settle. Saves a draft file and shows it in the conversation; after approval, writes
  project.json, edition_notice.json, about_book.json and the site repo's book folder,
  then builds and verifies both EPUBs. Run after translation and after the content
  review, and before kdp-publish, which reads what this wrote. Use when asked to
  "draft the book metadata", "write the parent notes", "write Sobre este libro",
  "prepare the book page", "what did this edition change", "get this book ready for
  the site", "fill in the epub metadata", "write the copyright page", "write the
  book description", or "book-metadata".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - WebFetch
  - WebSearch
  - AskUserQuestion
---

# book-metadata

You do the judgment work: what to name, how to word it, which gaps matter.
**`scripts/book_metadata.py`** says what a draft can rest on, reads the record, checks
the drafted texts and does every write. One STOP gate (G1) sits before anything is
written.

## Where this sits

```
footnote-pass, image-pass, quotation-pass → family-review or theology-review
  → book-metadata: the edition and the site texts → build both EPUBs → verify
  → kdp-publish: the store listing               (only for a book that goes to KDP)
```

- **This skill writes the book.** Everything in either EPUB or on the site is written
  here, so a book that goes to the site with no Kindle listing is finished when this
  skill is.
- **`kdp-publish` writes the store.** It runs afterwards, reads the edition this skill
  wrote, and writes only `kdp_listing.json`. It refuses a book this skill has not
  reached.
- **Why this order.** The gaps this skill reports lead to hand edits and new notes, and
  every count stated anywhere has to follow the final build.

## What this drafts, and where it lands

| Text | Stored in | Shown in | Rendering |
|---|---|---|---|
| `edition` | `project.json` (title, subtitle, author, translator, publisher, rights, subjects, original year, `edition` version), `edition_notice.json` | both EPUBs: title page, copyright page, OPF metadata | the notice is paragraphs, `_italic_`, `[LIBRE]`/`[KINDLE]` paragraphs |
| `description` | `project.json` `description` as plain text; site `libros/<slug>/descripcion.md` | `dc:description`, top of the book page, and the body of the KDP description | Markdown |
| `resumen` | site `libros/<slug>/libro.toml` | the book's card, the page's meta description | one sentence |
| `forms` | `project.json` | site book page | codes from `book_metadata.FORMS`: `I`, `II`, `III/IV`, `V/VI` |
| `adaptations` | `project.json` → site `adaptaciones.md` | «Sobre esta edición» | Markdown |
| `parent_notes` | `project.json` → site `padres.md` | «Lo que los padres deben saber» | Markdown |
| `about_book` `{heading, body}` | `about_book.json` | both EPUBs, first page after the contents | paragraphs, `_italic_`, `[texto](https://…)`, `[LIBRE]`/`[KINDLE]` paragraphs, `{{SITIO}}`/`{{COMENTARIOS}}`. **No bold, lists or subheadings** |

**Each text has one job.**
- **The edition:** what the book is called, who made it and under what rights.
- **Description:** why this book is worth reading. Nothing about the edition. It is
  the one description: the site, the EPUB and the store all start from it, so it
  carries no link, price or program name.
- **«Sobre esta edición»:** what this edition is and what it changed.
- **«Lo que los padres deben saber»:** what the parent has to judge or act on.
- **«Sobre este libro»:** the short form of the last two, in the EPUB. There is no
  separate short parent-notes field.

On the site the two boxes open folded to about four lines behind «Ver más», so the
first lines carry the most. `forms`, `adaptations` and `parent_notes` never go into
either EPUB.

## When to run it

After the last translated chapter, after **family-review** or **theology-review**, and
after `footnote-pass`, `image-pass` and `quotation-pass`, because the texts count the
notes, describe the images and carry the Bible version's notice. Before the final
build: the pages show up only in an EPUB built after they were written.

## The CLI (read first)

`python -X utf8 scripts/book_metadata.py <command> --project <slug>` prints one JSON
object and mirrors it to `projects/<slug>/.harness/metadata/last_output.<command>.json`.
**Read that file** rather than piping stdout into another interpreter.

```bash
python -X utf8 scripts/book_metadata.py status      --project <slug> [--no-facts] [--no-fetch]
python -X utf8 scripts/book_metadata.py facts       --project <slug> [--no-fetch]
python -X utf8 scripts/book_metadata.py notes       --project <slug> [--epub <path>]
python -X utf8 scripts/book_metadata.py changes     --project <slug>
python -X utf8 scripts/book_metadata.py source-diff --project <slug> --against <path> [--min-words N]
python -X utf8 scripts/book_metadata.py draft       --project <slug> [--pull] [--dry-run]
python -X utf8 scripts/book_metadata.py check       --project <slug> [--texts-file <path>] [--epub <path>]
python -X utf8 scripts/book_metadata.py apply       --project <slug> [--texts-file <path>] [--dry-run]
python -X utf8 scripts/book_metadata.py site        --project <slug> [--site-dir <path>] [--dry-run]
python -X utf8 scripts/book_metadata.py verify      --project <slug> [--target kindle|libre]
```

Two files are yours:
- **`projects/<slug>/.harness/metadata/texts.json`:** the texts as data, which `check`,
  `apply` and `site` read. Write it with the Write tool, never a heredoc.
- **`projects/<slug>/metadata_draft.md`:** the draft the user reads and edits. `draft`
  starts it and keeps its fenced blocks in step with `texts.json`; the evidence around
  the blocks is yours.

```json
{
  "edition": {
    "title": "…", "subtitle": "…", "author": "…", "translator": "…", "publisher": "…",
    "source_title": "…", "original_year": 1890,
    "subjects": ["JUVENILE NONFICTION / …", "…an LCSH heading…"],
    "version": "1.0", "status": "revisada",
    "notice": {"heading": "Créditos", "body": "…paragraphs separated by blank lines…"}
  },
  "forms": ["II"],
  "adaptations": "- …\n- …\n- …",
  "parent_notes": "…\n\n**…** …",
  "about_book": {"heading": "Sobre este libro", "body": "…\n\n…\n\n[LIBRE] En [nuestro sitio]({{SITIO}}) …"},
  "description": "**…**\n\n…",
  "resumen": "…"
}
```

A key left out is left alone wherever it would land, and that holds inside `edition`
too. A book whose edition is already written needs only the keys that change.

**Say one line to the user at each step** (status read, review read, flags checked,
silent changes, notes, draft). The flag check is many tool calls, and a silent stretch
looks like a stall.

## Flow

### 1. Status, facts and the guide

```bash
python -X utf8 scripts/book_metadata.py status --project <slug>
```

- **`needs`:** settle each item first.
  - No content review in the project folder: ask the user where it is, or stop. The
    drafts rest on it.
  - An EPUB older than the last edits is rebuilt before any note is counted
    (`python scripts/harness.py epub --project <slug>`).
  - A review written on another text than `source.txt`: the source was edited before
    translation, and step 4 compares the two.
  - Items that start `facts:` come from the book's facts (below).
- **`reviews`:** `family-review.md` (family-review) or `content-review.md`
  (theology-review), each with `matches_source`.
- **`epub.endnotes`:** the note count. It comes from the built EPUB, never from an
  earlier draft: it changes after each footnote-pass.
- **`edition`:** the edition as `project.json` and `edition_notice.json` hold it now.
- **`chapters`:** `chapter_NN` → the number the EPUB heading shows, and the last
  number. The review cites the source's numerals, and `chapters/chapter_NN.txt` is
  offset by the front matter. The texts cite the number shown here.
- **`current.path`:** every text as it is written today, in the shape of `texts.json`.
  A book with texts in place is a revision: read that file, and say in the draft what
  changes.
- **`site`:** whether the book has a folder in the site repo yet.
- **`facts`:** what the repo already knows about the edition; the whole of it is at
  `facts.path`.
  - `additions` and `notice_draft`: the "Esta edición incluye…" sentence and a draft
    copyright page, with the Bible version's notice when the book quotes one.
  - `footnotes`, `images`: the notes by origin, the new cover, the images with
    translated lettering or redone.
  - `rights_flags`: read them aloud to the user. A source edition whose translator or
    illustrator died within seventy years is in copyright across most of Europe, and
    `mexico_life_plus_100` matters because Mexico is the readership.
    `quoted_text_over_limit` means the quoted Bible text exceeds the version's free
    terms: stop and raise it.
  - `needs_input`: `original_year` (the candidates may name a translation's year:
    research it), `author` (with candidates), `source` (no catalog record, so the
    rights were not checked), `quotations` (run quotation-pass first, or have the user
    confirm the book quotes no Bible version), `footnotes` (the note store and the
    built EPUB disagree).

**Settle the open questions in one AskUserQuestion**, with the evidence inside the
dialog: everything in `ask` (the edition version and status, the original year) and
whatever `needs_input` could not be researched. Do it before drafting. A version is a
release decision: never guess it.

Then **Read `guide.path` in full.** It is the user's writing guide: the voice, the
rules for each text and the texts to imitate. Its rules outrank your own sense of what
a good parent note is. A previous `metadata_draft.md` in a sibling project is a second
model when the guide points to one.

### 2. Read the content review in full

List every item the texts may have to name:
- the review's **Cut/rewrite** items,
- its **Preview** and **Skip aloud** items,
- its worldview, theology and accuracy flags,
- anything the user asked to have named.

Note the audience the review gives, and what it rates as needing no action.

### 3. Check every flag against the translation

For each item record one of **rewritten**, **noted**, **unchanged**, with the Spanish
sentence and the note number. This decides the wording: «una nota lo corrige» against
«este pasaje no lleva nota». Never write the first without having seen the note.

```bash
python -X utf8 scripts/book_metadata.py notes --project <slug>
```

- **`notes`** lists every endnote under the chapter heading the reader sees, so a
  flagged passage's note is found by its chapter. `memo_like` names a note that reads
  like a reader's memo saved as a note: report it as a gap.
- **Cut/rewrite items first.** They were written before translation, and nothing
  guarantees the translation acted on them.
- **Where to look:** the translated chunk for the passage (`chunks/*.json`,
  `source_text` against `translated_text`).
- **Search on distinctive Spanish words near the passage,** not on the flagged English
  word. «negro» is mostly a colour.
- **One chapter at a time.** A whole-book grep with no chapter filter floods the
  context.
- **A claim about every chapter** («cada sermón termina con una oración») is checked by
  what is stable in the text, not by a heading that only some chapters carry.

### 4. Find the silent changes

The site promises that every kind of change is declared, so report what the record
shows even when it differs from what the user remembers.

```bash
python -X utf8 scripts/book_metadata.py changes --project <slug>
python -X utf8 scripts/book_metadata.py source-diff --project <slug> --against "<the reviewed text>" --min-words 10
```

- **`changes`:** the hand edits where a number or a proper noun changed, which is
  where a silent correction of the author hides. `left_out` counts the rest: style
  edits say nothing here, and quotation-pass rows are declared by the Bible version.
- **`source-diff`:** run it when `status` says the review read another text. It
  compares that text with `source.txt` word by word, blind to case, punctuation and
  line breaks. `by_size` counts every difference; `--min-words 10` lists the
  rewrites and leaves a scan's small fixes out. Read the small ones (`--min-words 1`)
  only for a pattern, such as references added throughout.
- **`chunks/*.json`:** what the translator changed with no hand edit, such as an
  epithet made neutral at translation time.
- **A correction that blames the source** needs support from outside the pipeline: a
  second edition, another translation, a catalogue. Say in the draft what the support
  was, and offer a wording that claims less when there is none.

### 5. Outside the chapters

- **Front and back matter.** What `source.txt` has that the EPUB does not: a preface,
  a list of illustrations, an advertisement. What was kept, and under what heading.
- **Images.** `facts.images` gives the counts. `images_original/`,
  `.harness/images/manifest.json` and `cover/` say which images were replaced or
  dropped and whether the captions are translated. Art repainted from the source's
  own pictures is «basadas en las originales», not new illustrations.
- **Provenance.** The source edition, an intermediate translation, and the notes the
  translator wrote. Give a year for an intermediate edition only when a catalogue
  confirms it.

### 6. Draft

**The edition.** Start from `facts` and `status.edition`:
- **Title and subtitle:** `facts.titles` has every title the book carries. The cover's
  split is usually right. When `facts.house_subtitle` is set, it is the subtitle
  unless the user says otherwise.
- **Translator and publisher:** the house spelling, `facts.translator.default`.
- **Original year and title:** first publication of the *original* work, as settled
  in step 1.
- **Subjects:** two or three BISAC headings and the catalog's LCSH headings.
- **Notice:** start from `facts.notice_draft`, or, on a revision, from the notice the
  book has (`status.edition.notice`), changing only what is stale. The draft drops a
  line it has no value for, such as the original title: put it back. Keep the
  Scripture notice line word for word; it is the wording the version requires. The
  notes sentence gives the count in digits. Never the name Project Gutenberg, anywhere
  in the book.
- **Version and status:** as the user answered.

**Forms.** Look the book up as the guide says, and map the level to a code. Cross-check
against the site's `edades`. Give the evidence in the draft, not in the texts.

**The texts,** following the guide:
- **`description`** and **`resumen`**, with two or three alternative hooks. Any fact
  beyond the book and its facts (a credential, a famous admirer) gets one search, and
  its source goes in the draft.
- **`adaptations`**, true as of today's build. A Cut/rewrite the translation acted on
  is declared by its purpose, not by what the original said.
- **`parent_notes`**. Only what the edition does not already handle. Content the user
  decides to keep becomes a parent note, even where the review asked nothing of the
  parent.
- **`about_book`**. The same content as the last two in plain paragraphs: edition
  facts, then translation decisions, then a summary for the parent. A site link goes in
  a `[LIBRE]` paragraph.

**The EPUB page is stricter than the site about spoilers.** A child reading alone sees
«Sobre este libro» before chapter 1. It names no death and no event from the last
chapters; it gives the hard chapters by number and sends the parent to the site. How
the book resolves a religious or worldview thread is not plot: state it there too,
without the scene.

Write `texts.json`, then:

```bash
python -X utf8 scripts/book_metadata.py draft --project <slug>
python -X utf8 scripts/book_metadata.py check --project <slug>
```

Fix every error. `check` refuses:
- markup the EPUB page would show as typed characters, and site-only or EPUB-only
  markup in the wrong text;
- a note count that differs from the built EPUB, in any text and on the copyright
  page;
- a chapter cited past the last one;
- a copyright page without the Bible version's notice, or with the Gutenberg name;
- a blank title, author or translator.

It warns when the draft file and `texts.json` differ. It cannot judge voice, length or
spoilers: reread each text against the guide before the gate.

### 7. Gaps and decisions

A **gap** is something the texts would have to hedge on and that a small edit would
settle: a dangerous passage with no note, an uncorrected error, a leftover epithet, a
Cut/rewrite item that was not done, a memo printed as a note. List each with the line
of the draft it would change. Fixing a gap is the user's call and another skill's work
(a hand edit, `footnote-pass`); this skill only reports it.

List separately what you left out of the parent notes on purpose, and why.

### 8. Finish `metadata_draft.md`

`draft` wrote one marked, fenced block per text. Around them, add:

1. **Header:** the date, what the draft was made from (the review, the EPUB and its
   date), and that nothing has been written yet.
2. **Under the description:** the alternative hooks, and, when the book has no folder
   in the site repo, a proposed `libro.toml` modelled on an existing book's.
3. **Under `forms`:** the evidence.
4. **What the record shows:** a table of finding, where, evidence. Every claim in the
   texts traces to a row. `kdp-publish` quotes this table as the basis of a copyright
   claim, so it names what the translator changed and wrote.
5. **Decisions:** the gaps, each with the draft line it would change, and what was left
   out on purpose.

Leave the `<!-- text: … -->` marker above each block: it is how the block is found.

### 9. STOP, G1: approve the texts

End the turn with the texts in your message: the edition (title, subtitle, credits
page, version), the description and hooks, `resumen`, `forms`, `adaptations`,
`parent_notes`, the `about_book` body, then the gaps and open decisions, then the
draft's path. Show the texts verbatim, not summarized.

**Ask in plain text, at the end of that message.** Do not open an AskUserQuestion
dialog here: text printed just before a dialog is hidden behind it.

Then iterate:
- **The user answers in the conversation:** change `texts.json`, run `draft` and
  `check`, and show the changed texts again.
- **The user edits the draft file, or a copy of it:** run `draft --pull`. Their wording
  is final: it is carried into `texts.json` verbatim. Smooth nothing, and tell them
  about anything `check` then refuses.
- **A gap gets fixed** (a note added, a passage edited): rebuild the EPUB, re-run
  `status` for the new count, and update every line that states it.

Write nothing outside those two files until the user approves.

### 10. Write

```bash
python -X utf8 scripts/book_metadata.py apply --project <slug> --dry-run
python -X utf8 scripts/book_metadata.py apply --project <slug>
```

Relay the dry-run's `changes` before the live run, above all any `replaced` row: that
is a text the user wrote or approved earlier, or a title typed by hand. `apply` writes
the edition, `forms`, `adaptations`, `parent_notes` and the plain description to
`project.json`, the copyright page to `edition_notice.json` and the page to
`about_book.json`. It refuses while `check` has errors.

Then the site, when `status.site.book_dir_exists` is true:

```bash
python -X utf8 scripts/book_metadata.py site --project <slug> --dry-run
python -X utf8 scripts/book_metadata.py site --project <slug>
```

`site` writes `adaptaciones.md` and `padres.md` from `project.json`, `descripcion.md`
from the description, and the `resumen` line of `libro.toml`. It touches nothing else
in `libro.toml`, and it never commits or pushes: that stays with the user, in the site
repo.

**When the book has no folder in the site repo,** `site` stops and says so. Creating a
book page (cover, release, ages, order) is a deliberate step there. Leave the proposed
`libro.toml` in the draft, and say that `site` is ready to run once the folder exists.

### 11. Build, verify and hand off

Build both targets, unless another pass is still to come, then read them back:

```bash
python scripts/harness.py epub --project <slug>
python scripts/harness.py epub --project <slug> --target libre
python -X utf8 scripts/book_metadata.py verify --project <slug>
```

`verify` checks every build that exists and must come back `ok`:
- **`note_counts_agree`:** the copyright page and «Sobre este libro» state the count
  the notes page has.
- **`footnotes_look_like_glosses`:** fails on a memo; a short note with no final period
  is named for someone to look at.
- **`no_leftover_markup`:** a `[LIBRE]`/`[KINDLE]` tag or a `{{SITIO}}` variable
  reached a page that does not resolve it, usually a chapter.
- **`no_gutenberg_trademark`:** names the chapter file that still carries the name.
  That text is in the book's body and is removed from the translation.
- The Kindle build may carry no live link; the libre build carries them by design.

Tell the user:
- what was written, and where;
- what is left to them: the commit and push in the site repo, a book folder to create,
  an EPUB to upload;
- the gaps still open;
- **for a book that goes to KDP: run `kdp-publish` next.** It reads this edition and
  this draft. A book on the site is not enrolled in KDP Select.

**Update the guide.** When the user's feedback on this book changed how a text is
written, name each rule in one line and, with their yes, add it to
`prompts/book_metadata_guide.md` under the text it governs, with the date and the
book. That file is how the next book starts from where this one ended. When the user
has no copy of their own yet (`status.guide.kind` is `example`), offer to start one
from the example and this book's approved texts.

## A collection (several works, one volume)

Pass the **collection slug** as `--project`. The volume is one book: its edition, its
«Sobre este libro» and its site texts are written to the collection folder, never to a
member's.

- **`status`** lists each member's review as `<member>/content-review.md` and says
  which members have none. `collection` names the works.
- **The flag check runs per member:** each was reviewed, translated and corrected in
  its own folder. `changes` reports each work's hand edits; `source-diff` takes a
  member's slug.
- **`notes`** reads the volume's EPUB, with each note under its work and chapter.
- **The notice** is plural, as `facts.notice_draft` drafts it.
- **Build** with `python scripts/harness.py collection-epub <slug>`.

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths.
- **Python from Git Bash cannot open `/c/Users/...` paths.** Pass `C:/Users/...`, and
  always run `python -X utf8`.
- **Temp files go in the scratchpad,** not under `../`, which lands outside the repo.
- **Never `cat > file` without a heredoc:** it waits on stdin until the timeout.
- **Rerunning is safe.** `status`, `notes`, `changes`, `source-diff`, `check` and
  `verify` write only their own output files (`status` also refreshes the facts).
  `apply` and `site` report `unchanged` for a text already in place.
- **The Export tab's Website box** edits `forms`, `adaptations` and `parent_notes`
  too, through `/api/project/<id>/site-metadata`. A text the user changed there after
  the draft shows up as a `replaced` row in the dry-run: ask before overwriting it.
- **`harness.py site-export`** writes only `adaptaciones.md` and `padres.md`. `site`
  here does the same and adds the description and the `resumen`.
- **House wording.** The translator's name, the imprint, the rights line and the notice
  template come from `prompts/kdp_defaults.json`; the license, the site links and the
  edition label from `prompts/publish_defaults.json`. Both are gitignored, with tracked
  `.example` twins.
- The per-user guide is `prompts/book_metadata_guide.md` (gitignored; tracked twin
  `book_metadata_guide.example.md`).

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/book-metadata/`. When the
user asks for a friction log, or a run wasted significant tokens or operator time and
they would plausibly want one, invoke the **`friction-log`** skill rather than
hand-rolling the file. It owns the location, naming and section skeleton. Those prior
logs are also the best standing record of this skill's known rough edges: **read the
latest one in full before the run**, not only its filename.
