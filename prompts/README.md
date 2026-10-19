# prompts/

Every LLM call in this project renders a plain-text template from this directory.
Editing these files is how you change what the models are told.

Full reference: [`docs/PROMPT_GUIDE.md`](../docs/PROMPT_GUIDE.md).

## Format

Plain text with `{{variable}}` placeholders, substituted by a literal string replace
(`render_prompt` in `src/utils/file_io.py`). **Not Jinja2** — no conditionals, loops, or
filters. An unfilled placeholder raises `KeyError` naming the variable.

Leading `#` comment lines are stripped: everything before the first line of eighty `=`
characters never reaches the model, so version notes and variable lists are free.

## The `.example.txt` rule

These prompts are meant to be edited per-user. Each ships as a checked-in
`<name>.example.txt`; the loader prefers your own `<name>.txt` when it exists, and your
copy is gitignored so it never collides with an update to the shipped default.

```bash
cp translation.example.txt translation.txt   # now yours
```

| Your copy (gitignored) | Shipped default |
|---|---|
| `translation.txt` | `translation.example.txt` |
| `style_guide_generate.txt` | `style_guide_generate.example.txt` |
| `style_guide_questions.txt` / `.json` | `style_guide_questions.example.txt` / `.json` |
| `glossary_bootstrap.txt` | `glossary_bootstrap.example.txt` |
| `dialogue.txt` | `dialogue.example.txt` |
| `translator_note_default.txt` | `translator_note_default.example.txt` |
| `house_style_rules.json` | `house_style_rules.example.json` |
| `house_lexicon.json` | `house_lexicon.example.json` |
| `book_groups.json` | `book_groups.example.json` |
| `decisions_taxonomy.json` | `decisions_taxonomy.example.json` |
| `kdp_defaults.json` | `kdp_defaults.example.json` |
| `kdp_description_examples.md` | `kdp_description_examples.example.md` |

Every other file here is tracked directly — edit in place and commit.

## What's in here

| Group | Files |
|---|---|
| Translation | `translation.txt`, `retranslate_sentence.txt`, `dialogue.txt` |
| Setup beats | `style_guide_questions.*`, `style_guide_generate.txt`, `glossary_bootstrap*.txt`, `address_map_generate.txt` |
| Judges | `judge_dialogue*.txt`, `judge_address*.txt`, `address_forms.txt`, `judge_absolute*.txt`, `judge_pairwise*.txt` |
| Standards | `house_style_rules.json` — the house rules the translator and the judges read and the drafting steps pre-fill from (JSON data, not a template); `house_lexicon.json` — house renderings that pre-fill the glossary; `book_groups.json` — series and domains whose books share precedent; `decisions_taxonomy.json` — the kinds of hard decision the decisions scan looks for; `proposals/` — house-rule proposals from the decisions stage (generated, gitignored) |
| Annotations | `annotation_word_choice.txt`, `annotation_inconsistency.txt`, `annotation_footnote.txt`, `annotation_flag.txt` |
| End matter | `translator_note_default.txt` |
| Archives | `history/`, `previous/` |

## Two things to know before editing

- **`dialogue.txt` is what the dialogue judge checks against.** Change your conventions
  here, or the judge will keep flagging prose that matches your actual intent.
- **Section order in `translation.txt` is cache-load-bearing.** The file is split into a
  fixed prefix and a per-chunk suffix so the prefix stays byte-identical across a book and
  hits the provider's prompt cache. Reordering sections silently costs money on the API
  backend.

## Checking a change

```bash
# Renders the real prompt per chunk under .harness/translate/ — no spend
python scripts/harness.py translate-prepare --project projects/my-book --chapters 1
```
