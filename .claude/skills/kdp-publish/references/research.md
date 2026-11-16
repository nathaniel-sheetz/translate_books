# The Amazon evidence: seeds, comparable books, categories

Read this at step 3 of the kdp-publish flow. The research only feeds
`kdp_listing.json` (keywords, categories, the reading-age check). Nothing in it
reaches the book, which was built before this skill started.

## Three rules, each learned on three books

- **Run `suggest` and `comps` one after the other, never at the same time.** A
  concurrent run comes back blank: every listing with `title: ""` and `bsr.overall:
  null`, exit 0, no error. Run `suggest` in the background and `comps` only after it
  has finished, or the other way round.
- **Never end a turn while research runs.** Everything in the listing except keywords
  and categories can be drafted meanwhile: the description, the copyright basis, the
  AI disclosure, territories, positioning. Draft it, run `check`, and keep going.
- **A blocked run is a finding, not a reason to wait.** After one retry at `--delay 3`
  comes back blocked, go on with the category evidence, mark the category rationale
  **inferred**, and say so in `manual_checks`. Retries at rising delays have bought
  nothing. A store run tomorrow costs nothing, because the book does not wait on it.

## The working files

They live in `projects/<slug>/.harness/kdp/`. Read the keys here before parsing a
file; do not guess a shape.

| File | Top-level keys | Inside |
|---|---|---|
| `facts.json` | `titles`, `author`, `site`, `catalog`, `rights_flags`, `footnotes`, `images`, `quotations`, `authorship`, `ai`, `edition`, `existing_listing`, `needs_input`, … | the book and edition facts |
| `suggestions.json` | `seeds`, `prefixes_queried`, `kindle_scoped_replies`, `phrases`, `errors` | `phrases[]`: `phrase`, `hits`, `best_position`, `kindle`, `prefixes` |
| `comps.json` | `queries`, `listings` | `listings[]`: `asin`, `title`, `publisher`, `language`, `reading_age`, `price`, `ratings`, `bsr` `{overall, categories[]}` |
| `categories.json` | `categories` | `categories[]`: `node`, `name`, `path`, `siblings`, `children`, `top`, `sample[]` `{category_rank, overall}` |
| `listing.json` | yours | the draft, the default `--listing-file` |
| `last_output.<command>.json` | the command's own summary | `comps` has `kindle_categories`; `category` has each node's path and sample ranks |

`comps.json` accumulates across runs; `--reset` starts it over. The count of Kindle
nodes the Spanish-language comps rank in (`kindle_categories`) is in
`last_output.comps.json`, not in `comps.json`.

`comps` and `category` need Playwright, which is optional:
`pip install -r requirements-kdp.txt && python -m playwright install chromium`. Without
it they exit with that hint. Fall back to WebSearch for comparable editions and mark
the category evidence **inferred** in the listing's rationale.

## Seeds

Write 10–15 Spanish seed phrases to `.harness/kdp/seeds.txt`:
- the Spanish title, and the author in the form Spanish readers search
- genre + audience ("libros de animales para", "novelas para")
- "libros en español para"
- themes

Short open-ended prefixes produce more than finished phrases do. Then:

```bash
python -X utf8 scripts/kdp_publish.py suggest --project <slug> --seeds-file projects/<slug>/.harness/kdp/seeds.txt --expand
```

`--expand` sends about 27 requests per seed (4 to 8 minutes for 14 seeds): run it in
the background, with its stderr kept in a file, and draft while it runs. A phrase is
kept only if it contains every word of its seed. `kindle: false` means it was seen
only in Amazon's all-department fallback: that is shopper language, not proof of
Kindle demand.

## Comparable books

Run 2–4 queries: the Spanish title, and genre + "en español" with the audience.

```bash
python -X utf8 scripts/kdp_publish.py comps --project <slug> --delay 3 --query "bambi una vida en el bosque" --query "novelas clasicas juveniles en español"
```

`kindle_categories` counts the Kindle nodes the Spanish-language comps rank in. Two
details matter:
- **Spanish branch.** Amazon.com has a Spanish-language branch, Kindle eBooks ›
  Foreign Languages › Spanish › {Infantil, Juvenil, Literatura y Ficción, …}, and
  comps rank in it under English names ("Children's in Spanish", node 8407404011).
- **Print nodes.** A `(Books)` rung is a print node, not a Kindle one.

**A comp published by the house is this book or a sibling.** When a comp's publisher
is the house imprint and its title is this book's, the book is already for sale: see
"A book that is already live" in the skill.

## Categories

Take the 4–6 strongest candidate nodes:

```bash
python -X utf8 scripts/kdp_publish.py category --project <slug> --delay 3 --node 8407404011 --node 155191011
```

Each node gives its tree path (what KDP's picker shows), its siblings and children
(the alternatives), and the **overall** Kindle Store rank of its #1, #10 and #20 books.
That last number is the demand and competition signal:
- **#20 at overall ~160k:** a few sales a day puts a book on the category's first page.
- **#20 at ~9k:** that page is out of reach.

A sample whose `overall` is null was not read (a blocked product page gives no error):
count the nulls, and treat a node with most of them missing as not sampled.

One category in the Spanish branch, where the book can be seen, plus one or two
accurate English-tree categories is usually right. Earlier books' `categories.json`
files hold Spanish-branch node ids worth reusing, and a root node's `children` lists
the child ids under it.

## When Amazon blocks

- **A page:** the session reloads the homepage and retries once, then records `error`
  for that ASIN and moves on.
- **A whole run:** `comps` reports the block. Try once more at `--delay 3` with nothing
  else running, then proceed as above. Do not switch to a browser skill, and do not
  open Amazon pages yourself.
- **What the suggestion endpoint can and can't do:** it scopes to Kindle only some of
  the time, and a long-tail Spanish phrase may return nothing. Few phrases is a finding
  (thin Kindle demand in Spanish), not a failure.
