# A collection (several works, one volume)

Read this when `--project` is a collection (`harness.py collection-create`). A volume
is listed like a book: pass the **collection slug** as `--project` to every command.
Everything reads and writes the collection folder (its `project.json`,
`kdp_listing.json`, `.harness/kdp/`), never a member's, so each member keeps its own
listing.

The volume's edition is written by **book-metadata** run on the collection slug, as
for a book. What differs here:

- **`facts`** loops the members. `facts.works` has each one's original title, year,
  author, catalog and rights flags; `rights_flags` is the union, each tagged `[slug]`
  (the life+70 and Mexico checks must hold for every author). `needs_input` asks per
  work (`original_year.<slug>`, `quotations.<slug>`): set `original_year` in that
  member's own `project.json`. `quotations.json` in the collection folder is rewritten
  as the members' records added together; the share is over the whole volume.
- **Authors.** One distinct author is the Author; for several, the first is Author and
  the rest go in `contributors` with role `Author`. `check` does not compare the
  listing's Author with the volume's joined author line, and `verify-epub` checks one
  `dc:creator` per member author.
- **`copyright_basis.original_content`** collects what the translator wrote across the
  volume: the volume's translator note, each member's translator note
  (`facts.translator_notes_by_work`) and the editorial endnotes
  (`facts.footnotes.editorial`).
- **The build** is `python scripts/harness.py collection-epub <slug>`, which
  book-metadata already ran.
