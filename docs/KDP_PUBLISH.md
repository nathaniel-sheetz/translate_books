# KDP publishing: the listing and the EPUB metadata

The last mile from a finished translation to a Kindle listing. The `kdp-publish`
skill (`.claude/skills/kdp-publish/SKILL.md`) drives it; this is the engineering
reference for what it calls.

Two problems, two tools, in this order:

1. **The file.** Every build path resolves metadata the same way, and a book carries
   a generated title page and copyright page. What those say (the edition: title,
   credits, rights line, copyright page, edition version, description) is drafted by
   the `book-metadata` skill and written by `scripts/book_metadata.py apply`, along
   with the site's texts and «Sobre este libro». A book posted on the site with no
   Kindle listing stops here.
2. **The store page.** KDP asks for the title (with a public-domain descriptor),
   subtitle, contributors, description, reading age, three categories, seven keywords,
   an AI disclosure and territories. The categories and keywords should rest on
   evidence about Amazon.com, not intuition. `kdp-publish` drafts it and
   `scripts/kdp_publish.py apply` writes it to `kdp_listing.json`, and to nothing else.

The order matters because the first tool still changes the book: the gaps it reports
lead to hand edits and new notes, and every count stated in the listing follows the
final build. `kdp_publish.py check` therefore takes the book as given
(`listing.book_state`) and refuses a listing that names it differently
(`edition_mismatch`), states another note count (`note_count`), or was drafted before
the edition was applied (`edition_not_applied`).

## Pieces

| Module | Does |
|---|---|
| `scripts/book_metadata.py` | The edition's CLI: `status`, `facts`, `notes`, `changes`, `source-diff`, `draft`, `check`, `apply`, `site`, `verify`. One JSON object per call, mirrored to `.harness/metadata/last_output.<command>.json` |
| `src/edition.py` | The `edition` block of `texts.json`: its rules, and the `project.json` / `edition_notice.json` writes they plan |
| `src/site_texts.py` | The rest of `texts.json` (site texts, «Sobre este libro»), `status`, the draft file kept in step, and the writes |
| `src/edition_record.py` | What the record shows: the EPUB's notes by chapter, the hand edits that changed a number or a name, the diff against the reviewed source |
| `src/epub_readback.py` | A built EPUB's pages and notes, and the note counts a text states: the one place a stated count is compared |
| `scripts/kdp_publish.py` | The store's CLI: `facts`, `suggest`, `comps`, `category`, `check`, `report`, `apply`, `verify-epub`, `published`. One JSON object per call, mirrored to `.harness/kdp/last_output.<command>.json` |
| `src/kdp/facts.py` | Book and edition facts → `.harness/kdp/facts.json`. Both CLIs call it |
| `src/kdp/gutenberg.py` | The Gutenberg catalog record (RDF) and the rights flags drawn from it |
| `src/kdp/suggest.py` | Amazon search autocomplete, aggregated by phrase |
| `src/kdp/amazon.py` | Parsers for search, product and best-seller pages, plus a Playwright session |
| `src/kdp/validate.py` | KDP's listing rules: errors block `apply`, warnings go in the report |
| `src/kdp/listing.py` | The book as the listing must match it (`book_state`), what `apply` writes (`kdp_listing.json` only), the KDP form in page order, and `mark_published` |
| `src/kdp/report.py` | The dated report: `reports/kdp_<YYYYMMDD_HHMMSS>.md` |
| `src/kdp/verify.py` | Reads a built EPUB's OPF and spine back |
| `src/book_metadata.py` | The one metadata resolver every EPUB build path uses |

## Metadata resolution (`src/book_metadata.py`)

`load_book_metadata(project_dir)` reads the following:
- **Title:** `project.json` `spanish_title`, then `title`, then `.harness/config.json`
  `title`, then `ingest_report.json` `metadata.title`.
- **Author:** the same three files.
- **Language:** `target_lang_code`, then `language_code`, then `es`.
- **Everything else** comes from `project.json` alone: `subtitle`, `translator`,
  `description`, `rights`, `source_title`, `publisher`, `subjects`,
  `publication_date`, `front_matter`, `edition` (`{version, status}`), `site_slug`
  (else the slugified title).
- **Sidecars:** `translator_note.json`, `edition_notice.json` and `about_book.json`
  («Sobre este libro»).
- **`publish`:** the house settings from `prompts/publish_defaults.json` (gitignored;
  the `.example` twin is the fallback): the license, the site and feedback links, the
  edition-label format. A book's own `project.json` `license` overrides the house one
  field by field, and `"license": null` opts it out.
- **Site-only fields:** `forms`, `adaptations` and `parent_notes` are for the
  mislibrosvivos.com book page and never go into either EPUB.
  `load_site_metadata(project_dir)` reads them, not `load_book_metadata`. The Export
  tab's Website box edits them through `/api/project/<id>/site-metadata`. The
  `book-metadata` skill drafts them, with `about_book.json`, from the content review
  and the record of what the translation changed; `scripts/book_metadata.py` checks
  and writes them (`src/site_texts.py`).
  - **`forms`:** one or more recommended forms, which the site shows instead of an age
    range: `I` (years 1–3), `II` (4–6), `III/IV` (7–9), `V/VI` (10–12). They are
    stored in that order, and `book_metadata.FORMS` is the list.
  - **`adaptations`:** what this edition adapted or added.
  - **`parent_notes`:** the page's «Lo que los padres deben saber» (`padres.md`).

  Both texts are Markdown. The KDP reading age (`audience.reading_age_min/max`) is
  separate and is not derived from `forms`.

`epub_kwargs(project_dir, **overrides)` turns that into `build_epub` keyword
arguments. A non-blank override wins. Callers:

| Path | Before | Now |
|---|---|---|
| Web UI Build EPUB | `project.json` only, no subtitle/subjects/front matter | the loader; request values still win and are still saved back |
| `harness.py epub` | setup config `title`/`author` (the English source title) | the loader, so `spanish_title` wins |
| `build_epub.py` | `--title`/`--author` required, nothing else passed | flags optional; everything comes from the loader |
| `translate_book.py` epub and footnotes stages | slug + `"Unknown"` | the loader, then the slug / `"Unknown"` |
| `corrections_apply.rebuild_epub` | `project_name` + `"Unknown"` | the loader, then the legacy config |

**Behaviour changes worth knowing:**
- The CLI paths now append `translator_note.json` when a book has one. Before, only
  the web UI did.
- `harness.py epub` titles a book by its `spanish_title` when set.
- Nothing else changes for a book whose `project.json` lacks the new keys.

## The EPUB additions (`src/epub_builder.py`)

New `build_epub` keyword arguments (also passed through `build_epub_from_chunks`):

| Argument | OPF / spine |
|---|---|
| `subtitle` | a second `dc:title id="subtitle"` + `<meta refines="#subtitle" property="title-type">subtitle</meta>` |
| `subjects` | one `dc:subject` each |
| `date` | `dc:date` |
| `front_matter={"title_page": True}` | `titlepage.xhtml` (title, subtitle, author, "Traducción de …", imprint), first in the spine |
| `edition_notice_heading` / `_body` | `copyright.xhtml`, after the title page |

Both generated pages come before `nav` in the spine and stay out of the TOC: they are
pages a reader turns past, not places they navigate to. Both are opt-in. A build with
none of these arguments produces the same OPF and spine as before (covered by
`tests/test_epub_builder.py::test_no_front_matter_keys_leaves_spine_unchanged`).

## Build targets: `kindle` and `libre`

`build_epub(..., target=...)`, `build_collection_epub(..., target=...)`, and
`--target` on `build_epub.py`, `harness.py epub` and `harness.py collection-epub`. The
Export tab has a picker beside Build EPUB.

| | `kindle` (default) | `libre` |
|---|---|---|
| File | `<Spanish title>.epub` in the project folder | `libre/<site_slug>.epub` |
| `[texto](https://…)` | `texto` | a live link |
| `[KINDLE]` / `[LIBRE]` blocks | `[KINDLE]` kept | `[LIBRE]` kept |
| Edition line («Versión 1.2 (beta)») | on the title page when `edition` is set | required; the title page is turned on |
| House license | only if `license.targets` lists `kindle` | appended to the copyright page and `dc:rights`, with the Scripture carve-out when `quotations.json` has rows |
| `quotations.json` over the limits | builds | refuses |

The markup is read only in the generated pages: the translator note, the copyright
page and «Sobre este libro» (`about_book.xhtml`, after `nav` and first in the TOC).
Chapters are never touched. In those pages, `{{SITIO}}` and `{{COMENTARIOS}}` resolve
to the house site and feedback URL. The feedback URL gets `libro=<site_slug>` and
`edicion=<status>`. A blank one leaves its link as plain text and logs a warning.
A page that is blank in a target (all `[LIBRE]`, say) is left out of that build.

`verify(project_dir, epub, target=...)` reads a build back under its target's rules.
`kdp_publish.py verify-epub` reads the Kindle file; `book_metadata.py verify` reads
every build that exists.
- **`no_external_links`** (Kindle only): fails on any live link. The libre file
  carries them by design.
- **`no_leftover_markup`:** fails on any tag or variable that reached the output.
- **`note_counts_agree`:** every note count stated on the copyright page or in «Sobre
  este libro» equals the notes on the notes page. Those pages are written at different
  times, and a note added after either one leaves it stale.
- **`footnotes_look_like_glosses`:** fails on a note that reads like a reader's memo
  saved as a note («missing line break», «citation?»); a short note with no final
  period is named in the detail without failing. A bare Scripture reference is a note.

## Collections

A collection (`src/collection.py`, `harness.py collection-*`) is a volume bound from
several projects. Its folder carries a book's `project.json` and sidecars, so
`load_book_metadata`, `book_metadata.py` and `kdp_listing.apply` work on it unchanged
(`book_metadata.py status` lists each member's content review, and `changes` reports
each member's hand edits); a blank `author`,
`translator` or language falls back to the members' (one distinct author names the
volume, several are joined "A, B y C").

- **Build:** `build_collection_epub` (title page, copyright, nav, then per member a part
  page and its chapters, then one Notas page and the volume's translator note). One
  `dc:creator` per distinct member author, one `dc:source` per member's original title.
- **Facts:** `gather` on a collection dir runs `gather_collection`: per-member catalog
  fetches and rights flags (unioned, tagged with the work), summed notes, images, words
  and authorship, a plural notice draft (`_collection_notice_lines`, or the template's
  own `edition_notice.collection_lines`), and per-work `needs_input`.
- **Quotations:** the collection's `quotations.json` is rebuilt by every `facts` run from
  the members' records (`src/quotations/record.py:build_collection_record`): unique verses
  unioned, occurrences and quoted words summed, the share over the volume's words.
- **Checks:** `validate.check` treats `contributors` with role `Author` as metadata
  (keywords must not repeat them); `verify` requires every member author among the
  `dc:creator`s.

## Evidence sources

**Gutenberg catalog** (`gutenberg.org/ebooks/<id>.rdf`, fetched once and cached as
`.harness/kdp/gutenberg_<id>.rdf`). The ingest keeps only the body, so this is where
the following come from:
- LCSH subjects, LCC class and bookshelves
- the source edition's year (`marc906`) and an automatic summary
- a reading-ease line
- every credited person with their birth and death years

The id is parsed from `project.json` `gutenberg_url` or `pipeline_state.json` `url`.

**Rights flags** (`gutenberg.rights_flags`):
- `life_plus_70`: someone credited on the source edition died fewer than 70 full years
  ago, so it is in copyright in life-plus-70 territories. Bambi's English text is
  Whittaker Chambers' 1928 translation (he died in 1961), with Kurt Wiese's
  illustrations (he died in 1974).
- `mexico_life_plus_100`: past 70 years but inside Mexico's 100.
- `us_term`: the edition year is too recent for US public domain.
- `unknown_death`: a credited person with no death year on record.

They feed the listing's `territories` and the check `territories_vs_rights`.

**Autocomplete** (`completion.amazon.com/api/2017/suggestions`, plain HTTPS,
throttled):
- **Request:** `alias=digital-text`, `lop=es_US`, plus the search box's own widget
  parameters, which widen the replies.
- **Scope:** the Kindle scope is best-effort. The reply's `alias` says when it fell back
  to all departments (`aps`), and even Kindle-aliased replies fuzz a prefix they cannot
  complete ("bambi a" → "bamboo pajamas"). So a phrase is kept only if it contains every
  word of its seed.
- **`--expand`** adds seed + a…z, about 27 requests per seed.

**Comparable books and categories** (headless Chromium via Playwright, optional):
- **Access:** plain HTTP gets a bot page from Amazon search; a browser that loads the
  homepage first gets the real one. On a captcha or error page the session reloads the
  homepage and retries once.
- **`comps`:** searches the Kindle store, opens the top organic results, and keeps the
  title, byline, breadcrumb, publisher, date, language, print length, reading age,
  stars, ratings, price and the **Best Sellers Rank ladder with browse-node ids**.
- **`category`:** reads a node's best-seller page (its path in the tree, siblings,
  children and top items), then opens the books at ranks 1/10/20 for their overall
  Kindle Store rank. That is the demand and competition signal. Measured 2026-10-19:
  - Children's in Spanish (8407404011): #20 at overall ~161k.
  - Children's Classic Literature (155191011): #20 at ~9k.
- **Spanish branch:** Amazon.com has a Spanish-language branch, Kindle eBooks ›
  Foreign Languages › Spanish (7735161011), with 30 subcategories (Infantil, Juvenil,
  Literatura y Ficción, …).

Tokens: only the extracted fields leave `src/kdp/amazon.py`. A product page is about
2 MB of HTML. Its record in `comps.json` is a few hundred bytes, and the CLI prints a
summary of that.

## KDP rules enforced (`src/kdp/validate.py`)

Sources: KDP Help G200743940 (public domain), G201298500 (keywords), G200652170
(categories), G201189630 (description), G200672390 (content guidelines, AI). Read
2026-10-19.

| Code | Severity | Rule |
|---|---|---|
| `missing_copyright_basis` | error | `own_copyright` only: `copyright_basis.statement` says what the copyright covers |
| `own_copyright_kdp_framing`, `own_copyright_ai_translation`, `own_copyright_no_original_content` | warning | `own_copyright` only: KDP files translations under public domain; a disclosed AI translation is protected only in its human contribution (the warning quotes `facts.authorship`); no original content is listed |
| `missing_descriptor` | error | Public domain only. A differentiated public-domain title carries (Translated)/(Annotated)/(Illustrated) in the title field. A Spanish "(Traducido…)" passes, with `descriptor_not_english` as a warning |
| `no_opening_summary`, `opening_summary_too_long` | error | Public domain only. The description opens with a bulleted summary of what is original, ≤ 80 characters per bullet (KDP's own example runs four bullets, ~180 characters together). One line ending in a colon may introduce it, as KDP's example does |
| `opening_summary_late` | warning | The colon line introducing the bullets is longer than 120 characters, so the bullets are not really "at the beginning" |
| `description_too_long`, `description_bad_tag` | error | ≤ 4000 characters including tags; tags limited to `p br b i u em strong ul ol li h4 h5 h6` |
| `description_url/email/phone/price/emoji` | error | Prohibited description content |
| `keyword_count`, `keyword_too_long` | error | 7 keywords, each ≤ 50 characters |
| `keyword_quotes/html/program_name/claim` | error | Quotation marks, HTML, Amazon program names, quality/price/time claims |
| `edition_not_applied` | error | `project.json` has no translator or the book has no copyright page: `book_metadata.py apply` has not run |
| `edition_mismatch` | error | The listing's title, subtitle, author or translator differs from the book's. A collection's joined author line is not compared |
| `note_count` | error | A note count in the description or in `copyright_basis` differs from the built EPUB's |
| `select_on_site` | error | `kdp_select` is true and the book is on the site (`facts.site.on_site`: a folder in the site repo, or a libre build). KDP Select requires the ebook to be exclusive to Amazon, the publisher's own site included. `select_bars_site` is the warning when the book is not on the site yet |
| `description_diverges`, `reading_age_vs_forms`, `revision_overclaim`, `epub_block_ignored` | warning | The store description does not carry the book's description as written; the reading age does not overlap the site's forms; the copyright statement claims sentence-by-sentence revision with no measured share; an old listing's `epub` block, which is no longer read |
| `gutenberg_mention` | error | Project Gutenberg's trademark license bars the name on a commercial derivative. Checked in the title, subtitle, description and keywords here, and in the rights line and the copyright page by `book_metadata.py check`; `verify-epub` checks the built book too. Only the trademark forms count (`gutenberg.names_trademark`: "Project/Proyecto Gutenberg", `gutenberg.org`, "Gutenberg-tm"/"eBook"), so Johannes Gutenberg in a chapter on printing passes; a keyword fails on the bare word, since as a search term it targets the brand |
| `category_count`, `category_duplicate` | error | 3 distinct categories |
| `missing_author`, `missing_translator` | error | A translation credits the translator and the original author |
| `reading_age_inverted` | error | min ≤ max |
| `keyword_repeats_metadata/category`, `keyword_stoplist`, `keyword_duplicate`, `keyword_maybe_claim`, `keyword_no_evidence` | warning | KDP's advice on wasted keyword characters |
| `title_subtitle_long`, `reading_age_range`, `category_not_kindle`, `ai_disclosure_incomplete`, `territories_vs_rights` | warning | House checks. `territories_vs_rights` becomes an `info` line once `territories.rationale` records why worldwide sale stands |

## Publishing Rights: public domain or owned copyright

`listing.publishing_rights` (default: `kdp_defaults.json` `publishing_rights`, else
`public_domain`) decides which rules apply.

- **`public_domain`:** KDP's differentiated-public-domain rules: the descriptor in the
  title field and the opening bullet summary. 35% royalty only, no KDP Select.
- **`own_copyright`:** "I own the copyright and I hold the necessary publishing
  rights". A translation is a derivative work with its own copyright (17 U.S.C.
  §§ 101, 103), and translators do list translations of public-domain works this way
  in Kindle Unlimited. But KDP's public-domain page files translations under public
  domain, and for an AI-drafted translation US copyright covers only the human
  contribution. So the listing must carry `copyright_basis` (`statement`,
  `original_content`), and the report's Copyright basis section sets it beside the
  measured record.

**`facts.authorship`** is that record:
- **`changed_words` / `changed_fraction`:** final words outside the longest matching
  runs against `.harness/translate/<chunk>.draft.txt`, the last model draft.
- **Sentence edits:** `reader_saves_typed` and `reader_saves_from_finding` count rows
  in `corrections_applied.jsonl` with no `source`, i.e. reader-view Saves. A save
  that started from a checker's finding carries `from_finding`. `model_fixes` counts
  `judge`/`harness` rows.
- **Limits:** books translated before the harness have no drafts. A redo replaces
  the draft, so a rewrite done by re-translating is not counted as human work.

The report also quotes the "What the record shows" table of `metadata_draft.md`, which
`book-metadata` writes: every change the edition made, where, and the evidence.

**KDP Select and royalties.** Neither answer makes a book on the house site eligible
for Select: the libre EPUB is given away there, and Select's exclusivity covers the
publisher's own website (KDP Help G200798990, read 2026-11-15). So an owned-copyright
title on the site earns 70% within KDP's price band, except in Brazil, Japan, Mexico
and India, where the 70% rate requires Select and sales otherwise pay 35% (G200634500,
read 2026-11-15). `kdp_form` carries the Select answer with its reason.

## A live listing

`kdp_publish.py published --asin B0…` writes `asin` and `published_at` into
`kdp_listing.json`, and `apply` keeps them. `facts.existing_listing` is `{file, asin,
published_at}`; for a book listed by hand before that command existed, the ASIN is read
from the description examples when a heading there names the book's title
(`asin_from`). With an ASIN on record the run is a refresh of that listing: the report
says so above the form.

## Per-user data

| File (gitignored) | Tracked twin | Holds |
|---|---|---|
| `prompts/kdp_defaults.json` | `kdp_defaults.example.json` | the canonical translator name, imprint, marketplace and locale, descriptor wording by what the edition adds, rights and copyright-page templates, keyword stoplist |
| `prompts/kdp_description_examples.md` | `kdp_description_examples.example.md` | the descriptions the drafting step imitates, annotated |

## Tests

- `tests/test_book_metadata.py`: resolution order, opt-in, overrides.
- `tests/test_kdp_validate.py`: the error and warning codes.
- `tests/test_kdp_facts.py`: URL forms, RDF parsing, rights flags, footnote and image
  counts.
- `tests/test_kdp_amazon.py`: parsers against trimmed live pages in
  `tests/fixtures/kdp/`, plus the autocomplete aggregation.
- `tests/test_kdp_listing.py`: edition applied → listing `apply` → build →
  `verify-epub`, report, `published`, CLI.
- `tests/test_kdp_verify.py`: the EPUB read-back, stated note counts, memo-like notes,
  the libre target.
- `tests/test_edition.py`, `tests/test_site_texts.py`, `tests/test_site_texts_edition.py`:
  the edition block, the site texts, the draft file, chapter references.
- `tests/test_edition_record.py`: notes by chapter, the hand edits listed, the source
  diff.
- `tests/test_epub_builder.py`: subtitle/subjects/date, front matter order, unchanged
  spine without opt-in.
- `tests/test_harness_flow.py`: `harness.py epub` title precedence.

Amazon's markup changes. When a parser test still passes but a live run comes back
empty, re-capture the page (save `page.content()` from an `AmazonSession`), trim it the
way the fixtures were trimmed, and fix the selector against it.
