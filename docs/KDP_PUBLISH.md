# KDP publishing: the listing and the EPUB metadata

The last mile from a finished translation to a Kindle listing. The `kdp-publish`
skill (`.claude/skills/kdp-publish/SKILL.md`) drives it; this is the engineering
reference for what it calls.

Two problems, one tool:

1. **The store page.** KDP asks for the title (with a public-domain descriptor),
   subtitle, contributors, description, reading age, three categories, seven keywords,
   an AI disclosure and territories. The categories and keywords should rest on
   evidence about Amazon.com, not intuition.
2. **The file.** Before this, only the web UI's Build EPUB wrote translator,
   description, rights or publisher, and `harness.py epub` shipped the English source
   title. Now every build path resolves metadata the same way, and a book can carry a
   generated title page and copyright page.

## Pieces

| Module | Does |
|---|---|
| `scripts/kdp_publish.py` | The CLI: `facts`, `suggest`, `comps`, `category`, `check`, `report`, `apply`, `verify-epub`. One JSON object per call, mirrored to `.harness/kdp/last_output.json` |
| `src/kdp/facts.py` | Book and edition facts → `.harness/kdp/facts.json` |
| `src/kdp/gutenberg.py` | The Gutenberg catalog record (RDF) and the rights flags drawn from it |
| `src/kdp/suggest.py` | Amazon search autocomplete, aggregated by phrase |
| `src/kdp/amazon.py` | Parsers for search, product and best-seller pages, plus a Playwright session |
| `src/kdp/validate.py` | KDP's listing rules: errors block `apply`, warnings go in the report |
| `src/kdp/listing.py` | What `apply` writes, and the KDP form in page order |
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
  `publication_date`, `front_matter`.
- **Sidecars:** `translator_note.json` and `edition_notice.json`.

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
| `gutenberg_mention` | error | Project Gutenberg's trademark license bars the name on a commercial derivative. Checked in the title, subtitle, description, keywords, rights and edition notice; `verify-epub` checks the built book too. Only the trademark forms count (`gutenberg.names_trademark`: "Project/Proyecto Gutenberg", `gutenberg.org`, "Gutenberg-tm"/"eBook"), so Johannes Gutenberg in a chapter on printing passes; a keyword fails on the bare word, since as a search term it targets the brand |
| `category_count`, `category_duplicate` | error | 3 distinct categories |
| `missing_author`, `missing_translator` | error | A translation credits the translator and the original author |
| `reading_age_inverted` | error | min ≤ max |
| `keyword_repeats_metadata/category`, `keyword_stoplist`, `keyword_duplicate`, `keyword_maybe_claim`, `keyword_no_evidence` | warning | KDP's advice on wasted keyword characters |
| `title_subtitle_long`, `reading_age_range`, `category_not_kindle`, `ai_disclosure_incomplete`, `territories_vs_rights` | warning | House checks |

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

## Per-user data

| File (gitignored) | Tracked twin | Holds |
|---|---|---|
| `prompts/kdp_defaults.json` | `kdp_defaults.example.json` | the canonical translator name, imprint, marketplace and locale, descriptor wording by what the edition adds, rights and copyright-page templates, keyword stoplist |
| `prompts/kdp_description_examples.md` | `kdp_description_examples.example.md` | the descriptions the drafting step imitates, annotated |

## Tests

- `tests/test_book_metadata.py`: resolution order, opt-in, overrides.
- `tests/test_kdp_validate.py`: every error and warning code.
- `tests/test_kdp_facts.py`: URL forms, RDF parsing, rights flags, footnote and image
  counts.
- `tests/test_kdp_amazon.py`: parsers against trimmed live pages in
  `tests/fixtures/kdp/`, plus the autocomplete aggregation.
- `tests/test_kdp_listing.py`: apply → build → `verify-epub`, report, CLI.
- `tests/test_epub_builder.py`: subtitle/subjects/date, front matter order, unchanged
  spine without opt-in.
- `tests/test_harness_flow.py`: `harness.py epub` title precedence.

Amazon's markup changes. When a parser test still passes but a live run comes back
empty, re-capture the page (save `page.content()` from an `AmazonSession`), trim it the
way the fixtures were trimmed, and fix the selector against it.
