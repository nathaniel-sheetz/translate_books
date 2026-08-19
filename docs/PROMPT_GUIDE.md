# Prompt Templates

Every LLM call in this project renders a plain-text template from `prompts/`. Editing
those files is how you change what the models are told — no code change required.

---

## The template format

Templates are **plain text with `{{variable}}` placeholders**. Substitution is a literal
string replace (`render_prompt` in `src/utils/file_io.py`), not Jinja2:

- No conditionals, no loops, no filters, no expressions.
- A placeholder left unfilled raises `KeyError` naming the variable, so a typo fails
  loudly instead of shipping `{{styl_guide}}` to the model.
- Anything you don't recognize, leave alone — the caller decides what gets passed.

(Jinja2 *is* used elsewhere in the repo, for the edit-review HTML report at
`web_ui/templates/edit_review_report.html.j2`. That is a report template, not a prompt.)

### Header comments

A prompt file may open with `#` comment lines documenting its version and variables.
Everything before the first line of eighty `=` characters is stripped before the prompt
reaches the model, so those notes cost you nothing:

```
# Translation Prompt Template
# Version: 1.3
# Available variables: book_title, source_text, ...

================================================================================
YOUR ROLE
================================================================================
...
```

---

## Customizing a prompt: the `.example.txt` rule

Six prompts are meant to be edited per-user. Each ships as a checked-in
`<name>.example.txt`, and the loader prefers your own `<name>.txt` when it exists
(`_resolve_prompt_path` in `src/style_guide_wizard.py`, `_resolve_dialogue_path` in
`src/utils/text_utils.py`). Your copy is gitignored, so your edits never collide with an
update to the shipped defaults.

```bash
# Take ownership of the translation prompt
cp prompts/translation.example.txt prompts/translation.txt

# Edit it — it is now the one that gets used
```

The overridable six, exactly as listed in `.gitignore`:

| Your copy (gitignored) | Shipped default | Controls |
|---|---|---|
| `prompts/translation.txt` | `translation.example.txt` | The main translation prompt |
| `prompts/style_guide_generate.txt` | `style_guide_generate.example.txt` | How the style guide is drafted |
| `prompts/style_guide_questions.txt` / `.json` | `style_guide_questions.example.*` | The style-guide question set |
| `prompts/glossary_bootstrap.txt` | `glossary_bootstrap.example.txt` | Glossary candidate translation |
| `prompts/dialogue.txt` | `dialogue.example.txt` | Spanish dialogue formatting rules |
| `prompts/translator_note_default.txt` | `translator_note_default.example.txt` | Pre-filled "Note from the Translator" |

Every other prompt in `prompts/` is tracked directly — edit it in place and the change is
a normal commit.

> `prompts/dialogue.txt` is the one the dialogue judge checks translations against. If you
> change your dialogue conventions, change them here, or the judge will keep flagging
> prose that matches your actual intent.

---

## What each prompt does

### Translation

| File | Used by |
|---|---|
| `translation.txt` | Every translation call, on all three backends |
| `retranslate_sentence.txt` | The reader's per-sentence Retranslate button |
| `dialogue.txt` | Injected into translation prompts as `{{dialogue_instructions}}` |

### Setup beats

| File | Used by |
|---|---|
| `style_guide_questions.txt` / `.json` | The fixed + conditional question set |
| `style_guide_generate.txt` | Drafting the style guide from your answers |
| `glossary_bootstrap.txt` | Proposing translations for glossary candidates |
| `glossary_bootstrap_word.example.txt` | Single-word glossary variant |
| `address_map_generate.txt` | Drafting the usted/tú address map |

### Judges

| File | Used by |
|---|---|
| `judge_dialogue.txt`, `judge_dialogue_batch.txt` | Dialogue-compliance judge |
| `judge_address.txt`, `judge_address_batch.txt` | Forms-of-address judge |
| `address_forms.txt` | The shared usted/tú detection rubric both address judges load |
| `judge_absolute.txt` + `_full_context` / `_no_voice` | Absolute-score evaluator variants |
| `judge_pairwise.txt` + `_full_context` / `_no_voice` | Pairwise model comparison |

See [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md) and
[`LLM_JUDGE_EVALUATOR.md`](LLM_JUDGE_EVALUATOR.md).

### Annotations

| File | Used by |
|---|---|
| `annotation_word_choice.txt` | Word-choice doubts |
| `annotation_inconsistency.txt` | Suspected inconsistencies (book-wide verdict) |
| `annotation_footnote.txt` | Drafting an endnote gloss |
| `annotation_flag.txt` | Free-form "Other" notes |

See [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md).

### Other

| Path | What it is |
|---|---|
| `translator_note_default.txt` | Pre-fills the Export stage's translator note (falls back to `.example.txt` until you create your copy) |
| `prompts/history/` | Archived prompt versions |
| `prompts/previous/` | Scratch space for the version you just replaced |

---

## Translation prompt variables

`build_translation_prompt` (`src/api_translator.py`) passes exactly these:

| Variable | What it holds |
|---|---|
| `{{book_title}}` | Project name |
| `{{source_language}}` | e.g. `English` |
| `{{target_language}}` | e.g. `Spanish` |
| `{{source_text}}` | The chunk to translate |
| `{{glossary}}` | Glossary terms relevant to this chunk, or `No glossary provided.` |
| `{{style_guide}}` | Style guide content, or `No style guide provided.` |
| `{{previous_chapter_context}}` | Tail of the previous chapter, for continuity |
| `{{context}}` | Extra context slot (currently empty) |
| `{{dialogue_instructions}}` | Rendered from `dialogue.txt` when the chunk has dialogue |
| `{{image_placeholder_instructions}}` | Structure-preservation rules when the chunk has `[IMAGE:...]` tokens |

The glossary is **filtered per chunk** — only terms that actually appear in this chunk's
source text are included, which keeps the prompt small and the cache stable.

### Section order is cache-load-bearing

`prompts/translation.txt` is split into a fixed prefix and a per-chunk suffix so the
prefix stays byte-identical across every chunk of a book and hits the provider's prompt
cache. **Reordering sections will silently cost you money** on the API backend by
invalidating that prefix.

Two per-book switches decide whether volatile blocks live in the cached prefix:

```bash
python scripts/harness.py config-set --project projects/my-book \
    --key always_include_dialogue --value true
python scripts/harness.py config-set --project projects/my-book \
    --key always_include_image_instructions --value true
```

Both default to auto: on when any chunk in the book has dialogue / images. Forcing them
on puts the block in the fixed prefix for every chunk, which trades a slightly longer
prompt for a stable cache. These are also `setup` flags
(`--always-dialogue` / `--always-images`) and are settable mid-book — you never need to
re-run `setup` to change them.

---

## Testing a change

There is no separate validation command. The fastest check is to render a real prompt and
read it:

```bash
# Renders one prompt file per chunk under .harness/translate/ — no spend
python scripts/harness.py translate-prepare --project projects/my-book --chapters 1
```

Then open `projects/my-book/.harness/translate/chapter_01_chunk_000.prompt.txt`. That is
byte-for-byte what a worker receives.

On the API path, `--dry-run` renders and estimates without spending:

```bash
python scripts/translate_api.py chunks/chapter_01_chunk_000.json --dry-run
```

A missing variable surfaces as a `KeyError` naming it. A prompt that renders but produces
bad output is a prompt problem, not a template problem — iterate on one chunk before
committing to a wave.

---

## Style guide feature detection

The style-guide wizard does not ask a fixed list of questions. Before any prompt is
shown, `src/text_feature_detector.py` runs a deterministic heuristic scan over the
**entire** source and writes a feature manifest to `projects/<id>/text_features.json`.
The manifest decides which conditional questions you see, and a compact summary of it is
embedded in the LLM-generated-questions prompt so the model does not re-ask what the
wizard already covers.

Source text is resolved by `src/utils/source_text.load_clean_source_text` in priority
order: `chunks/*_chunk_*.json` (the immutable `source_text` field) → `chapters/<id>.txt`
→ `source.txt`. Chunks come first so the scan stays in the source language even after
`chapters/` has been overwritten with translated content.

### Manifest shape

```json
{
  "generated_at": "2026-05-07T...",
  "source_mtime": 1746619200.0,
  "total_paragraphs": 251,
  "total_words": 18626,
  "features": {
    "dialogue":             { "name": "dialogue",             "present": true,  "count": 58, "confidence": 0.50, "evidence": ["..."] },
    "scripture_references": { "name": "scripture_references", "present": true,  "count": 45, "confidence": 1.00, "evidence": ["John 3:16 ..."] },
    "footnotes":            { "name": "footnotes",            "present": false, "count": 0,  "confidence": 0.0,  "evidence": [] }
  }
}
```

The cache invalidates automatically when the source mtime is newer than `source_mtime`.

### Detector library

| Feature | What it looks for |
|---|---|
| `dialogue` | Reuses `src/chunker.py` `_is_dialogue()` plus paragraphs opening with a raya (`—`). Present at `count >= 5` and >1% of paragraphs |
| `verse` | Runs of ≥4 consecutive short (<60-char), non-terminal lines separated by blank lines |
| `footnotes` | `[N]` brackets, `^N. ` runs in the back third of the text, repeated `*` markers |
| `epigraphs` | Short (<300-char) paragraph right after a chapter heading with an em-dash attribution |
| `letters` | Salutation (`Dear`, `Mi querido`, `Estimado`) plus valediction (`Sincerely`, `Atentamente`) within 60 paragraphs |
| `scripture_references` | `Book chapter:verse` against a Bible-book wordlist (English + Spanish, including 1/2/3 prefixes) |
| `archaic_language` | `thou/thee/thy/hast/…` or `vos/vuestra merced/heos/…` above 5 per 10K words |
| `foreign_passages` | ≥3 distinct `_italic_` / `*italic*` runs of ≥2 words |
| `lists` | ≥1 run of ≥3 consecutive lines starting with `-`, `*`, `•`, `1.`, `a)` |
| `block_quotes` | Indented (≥4 spaces) long lines, or `…:` followed by a long quoted paragraph |
| `dramatic_format` | ALL-CAPS speaker names with `:`, and/or `[Enter …]` stage directions |
| `measurements_imperial` | `\d+ (miles|feet|inches|lbs|°F|…)` |
| `currency_period` | `$`, `£`, `shilling`, `peso`, `real`, `maravedí` |
| `translator_notes` | `[N. del T.`, `[Translator's note`, `[Nota del traductor` |
| `epicene_animal_speakers` | Animal characters whose English source establishes a sex but whose Spanish noun is epicene (one fixed grammatical gender). Confidence rises when the English sex cue *conflicts* with the Spanish noun's gender — the case where a naive translation silently flips a character's apparent sex |

### Adding a conditional question

`prompts/style_guide_questions.json` holds two arrays: the fixed questions (always asked)
and the conditional ones, each gated on a predicate over the manifest. To add one, write
a detector in `src/text_feature_detector.py` returning a `FeatureResult`, register it in
`detect_all_features`, then add the question with a predicate naming your feature. Each
conditional question shows a one-line `Detected: …` excerpt so the user can see why it is
being asked.

---

## Related

| Document | Contents |
|---|---|
| [`TRANSLATE_HARNESS.md`](TRANSLATE_HARNESS.md) | How prompts are rendered and committed per stage |
| [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) | Providers, models, and prompt caching |
| [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md) | Judge prompts and how to add a judge |
| [`GLOSSARY_CANDIDATES.md`](GLOSSARY_CANDIDATES.md) | The bootstrap prompt in context |
