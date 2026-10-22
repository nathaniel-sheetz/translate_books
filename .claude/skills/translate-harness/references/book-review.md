# Book review (after a translate wave)

Load this when `status.book_review.due` is set, which `suggested_reference` routes
here:

- **canary**: the first translate wave of a partial book has landed and nothing
  has reviewed it;
- **fully translated**: the book is done and chunks were translated after the
  last review (or it was never reviewed).

Thin: it **delegates** to the **book-review** skill
(`.claude/skills/book-review/SKILL.md`), which owns the CLI and its gates. Hand it
the project slug and say why the review is due.

## Why it runs here

The review runs every judge over the chapters that owe it, then reads all the
findings together for problems no single finding shows: a glossary entry that
never reached the translator, a style guide that contradicts the address map, a
rule the translator keeps missing. Each of those is one upstream fix. After the
canary batch, fixing it costs a glossary or style-guide edit. After the whole
book, it costs a re-translation.

## Declining

The judge sweep spends subscription tokens and needs consent. If the user
declines the review for this wave:

```bash
python scripts/book_review.py skip --project <slug>
```

This marks the current translations reviewed, so the router moves on until new
chunks land. To turn the stage off for the book:

```bash
python scripts/harness.py config-set --project <slug> --key book_review_canary --value off
python scripts/harness.py config-set --project <slug> --key book_review_after_translate --value off
```

## After the review

Apply what the user approved (glossary, style guide or address map edits), then
continue where `status` points: `references/translate-workers.md` 4B-f (or the API
path) for the rest of the book, or footnotes and EPUB for a finished one. An
artifact change made after the canary reaches every chunk translated from then on.
Chapters already translated keep the old wording unless they are redone
(`references/retranslate.md`).
