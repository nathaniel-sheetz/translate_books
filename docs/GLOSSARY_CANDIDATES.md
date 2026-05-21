# Glossary Candidate Extraction

Heuristic glossary-term extractor that scans a book's source text and produces a ranked list of candidate terms — character names, place names, technical vocabulary, and other words that should be translated consistently — without any LLM calls. The same module also builds the bootstrap prompt that asks an LLM to propose translations for those candidates.

This doc is the source of truth for both the rules that drive extraction and the shape of the bootstrap prompt. CLI defaults match `extract_candidates()` and the web UI; if you change behaviour, update this doc in the same PR.

- Script: `scripts/extract_glossary_candidates.py`
- Prompt builder: `src/glossary_bootstrap.py`
- Context helpers: `src/utils/glossary_context.py`
- Web UI entry points: `/api/setup/<project_id>/extract-candidates` and `/api/setup/<project_id>/prompts/glossary`

## Contents

1. [Quick start](#quick-start)
2. [The pipeline](#the-pipeline)
3. [Source loading and pre-processing](#source-loading-and-pre-processing)
4. [Tokenization and sentence splitting](#tokenization-and-sentence-splitting)
5. [Dictionaries and the literary-frequency baseline](#dictionaries-and-the-literary-frequency-baseline)
6. [Extractors](#extractors)
7. [Post-merge passes](#post-merge-passes)
8. [Scoring and ranking](#scoring-and-ranking)
9. [Word lists and constants](#word-lists-and-constants)
10. [CLI reference](#cli-reference)
11. [Bootstrap prompts](#bootstrap-prompts)
12. [Web UI controls](#web-ui-controls)
13. [Output schema](#output-schema)
14. [Requirements](#requirements)

## Quick start

```bash
# Heuristic extraction only — writes ranked candidates JSON
python scripts/extract_glossary_candidates.py projects/mybook/source.txt -o candidates.json

# Write the bootstrap prompt to disk for copy/paste (no LLM call)
python scripts/extract_glossary_candidates.py projects/mybook/source.txt \
    -o candidates.json \
    --prompt-out glossary_prompt.txt \
    --bootstrap-context-mode word

# End-to-end: extract, propose translations via API, interactive review
python scripts/extract_glossary_candidates.py projects/mybook/source.txt \
    -o candidates.json \
    --bootstrap \
    --style-guide projects/mybook/style.json \
    --provider anthropic --model claude-sonnet-4-20250514
```

## The pipeline

`extract_candidates()` runs the following steps in order. Each step's verbose log line is shown in italics:

1. **Normalize source text** — newlines, image placeholders, curly/modifier apostrophes.
2. **Split into sentences** — abbreviation-aware (`Mr.`, `Lord St. Vincent`).
3. **Five independent extractors** produce per-key candidate dicts:
   - *Proper noun candidates*
   - *Uncommon word candidates*
   - *N-gram candidates*
   - *Repeated capitalized candidates* (safety net)
   - *Rare literary word candidates* (dictionary words rare in literary English)
4. **Merge** all five dicts, keeping the higher-priority type guess and the union of detection reasons.
5. **Collapse possessive variants** (`Nelson's` → `Nelson`, summing frequencies).
6. **Prune contained terms** — drop candidates whose every occurrence sits inside a longer candidate (leftmost-longest overlap resolution).
7. **Filter demonyms** — drop single-word nationality words and demonym-led adjectival phrases.
8. **Inject forced terms** — merge any terms from `forced_glossary_terms.json` that appear in the source, bypassing steps 4–7 entirely. Detection reason: `forced_user_term`.
9. **Exclude glossary terms** that already exist in the optional `-g glossary.json`.
10. **Score and rank**, then cap at `--max-candidates`.

Each step in 3–8 is implemented as a standalone, testable function. Tests live in `tests/test_extract_glossary_candidates.py`.

## Source loading and pre-processing

### Project-aware source loading

`_read_source_text()` does NOT always read the file you point it at. When the source lives inside a project directory, it delegates to `load_clean_source_text()`, which prefers (in order):

1. `chunks/*_chunk_*.json` `source_text` — immutable source-language text written by the chunker. Survives Stage 6 (combine) overwriting `chapters/`.
2. `chapters/chapter_*.txt` — clean per-chapter text, but on Stage-6'd projects these files may now contain *translated* text, so they're a fallback only.
3. The raw `source_file` you passed (front matter, TOC, copyright and all).

This is why pointing the script at `projects/mybook/source.txt` is correct: it auto-promotes to clean chapter or chunk text without you doing anything.

### Image placeholders

`strip_image_placeholders()` replaces `[IMAGE:path:description]` tokens with equal-length whitespace before tokenization. This keeps word counts and sentence boundaries stable while preventing image filenames, the literal word `IMAGE`, or description fragments from surfacing as glossary candidates.

### Curly-apostrophe normalization

Right-single-quote `'` and modifier-letter `ʼ` are mapped to straight `'` so `doesn't` survives as one token and dictionary lookups (`enchant.Dict("en_US").check("doesn't")`) succeed. Without this, fragments like `doesn`, `isn`, `wouldn` would slip through the OOV filter and show up as candidates.

## Tokenization and sentence splitting

### Token pattern

```python
TOKEN_PATTERN = re.compile(r"[\w'’ʼáéíóúüñÁÉÍÓÚÜÑ]+")
```

Matches words plus all three apostrophe variants and common Spanish accented characters. `tokenize_with_spans()` returns each token with `(text, start, end)` offsets so callers can inspect the punctuation between adjacent tokens.

### Sentence splitting

`split_into_sentences()` splits on sentence-ending punctuation followed by whitespace and an uppercase / open-quote character, then runs `_rejoin_abbreviation_splits()` to glue back any fragment that ended with a known title abbreviation:

- `Mr.`, `Mrs.`, `Ms.`, `Dr.`, `St.`, `Jr.`, `Sr.`, `Prof.`, `Rev.`, `Hon.`, `Capt.`, `Col.`, `Gen.`, `Lt.`, `Sgt.`, `Maj.`

That's why `Lord St. Vincent took command of the fleet.` stays one sentence and `Mrs. Ford spoke about the engine.` doesn't get split at `Mrs.`.

### Sequence-break characters

Inside `extract_proper_nouns()` and `extract_frequent_ngrams()`, the multi-token builders use `_has_hard_break()` to refuse to bridge tokens whose interstitial characters contain any of:

```
,  ;  :  "  “  ”
```

This is what prevents `Emile, Jules, and Claire` from coalescing into a fake multi-word name `Emile Jules` and stops the dialogue boundary `said Jules. "Even ...` from gluing `Jules Even` together. Periods are *not* in this set because abbreviations (`Mr. Smith`, `Lord St. Vincent`) must keep their sequences intact.

### Title-period restoration

When a multi-word sequence is finalized, `_restore_title_periods()` appends `.` to any token whose lowercase form is in `_ABBREV_NO_SPLIT`. The tokenizer drops the period; this puts it back, so the surface forms `Mrs. Ford` / `Lord St. Vincent` are rendered correctly. Bare title abbreviations on their own (`Mr`, `Mrs`, `Dr`, `Capt`, ...) are dropped before they can become standalone candidates.

## Dictionaries and the literary-frequency baseline

### DictionaryChecker

Wraps PyEnchant. Loads `en_US` and `en_GB` if available; `is_english_word()` returns True if **either** dictionary recognises the word. The British dictionary catches words like `honour`, `centre`, `colours`, `harbour`, `defence`, `learnt` that would otherwise be flagged as OOV by `en_US` alone.

`available` is True if at least one dictionary loaded. If neither is available, the OOV-based extractors silently degrade — `extract_uncommon_words()` falls back to the literary-frequency baseline, and the other extractors run without OOV gating.

### FrequencyChecker

A literary-English Zipf baseline built from NLTK's Brown (fiction, mystery, romance, humor categories) plus the Gutenberg corpus, pickled to `~/.cache/translate_books/literary_freqdist.pkl` after first build. When NLTK isn't available or those corpora aren't downloaded, falls back to `wordfreq.zipf_frequency()`.

`literary_zipf(word)` returns a Zipf score: higher = more common. Range roughly 1.0 (very rare) to 7.0 (the/and). Two CLI-tunable thresholds use this score:

- **`max_zipf_capitalized`** (default `4.0`) — admit always-capitalized dictionary words below this Zipf (catches `Merlin`, `Mammon`, `Tell` — character names that collide with uncommon dictionary words).
- **`max_zipf_mixed`** (default `3.0`) — admit mixed-case dictionary words below this Zipf as "rare in literary English" (catches `palmer`, `gaoler`, `halyard`, `carpel` — archaic or domain vocabulary).

The web UI exposes a single sensitivity slider (`-1..+1`, step `0.25`) that nudges **both** thresholds by the same offset. Lower offset → fewer candidates, more rigorous threshold; higher → more permissive.

First-run setup for the NLTK corpora:

```bash
python -c "import nltk; nltk.download('brown'); nltk.download('gutenberg')"
```

## Extractors

All five extractors return `dict[str, GlossaryCandidate]` keyed by the lowercase surface form. Their outputs are merged downstream.

### 1. `extract_proper_nouns`

Builds capitalized multi-word sequences token-by-token and records each as a candidate.

**Walk rules**
- Start at every capitalized token whose lowercased form is not in `SEQUENCE_BREAKERS` or `BLOCKED_WORDS`.
- Greedily extend through adjacent capitalized tokens, stopping when the next token fails the capitalized check, hits a sequence breaker / blocked word, *or* a hard-break character lives between this token and the next (see `_has_hard_break`).
- Sequences of length ≥ 2 are recorded as a multi-word candidate with type-guess priority:
  - Preceded by a `TITLE_WORDS` word (or starting with one) → `CHARACTER`, with detection reason `title_word_prefix`.
  - Starting with a `GEO_WORDS` word → `PLACE`, with detection reason `geo_word_prefix`.
  - Otherwise → `CHARACTER` (default for repeated capitalized sequences).
- Each token inside a sequence is also recorded standalone so the >80% capitalized-ratio check can use accurate totals.
- Sentence-start single capitalized tokens (`i == 0`) only contribute to total counts, not to candidate emissions, since `The`/`He`/`She`/etc. would otherwise dominate.
- Bare title abbreviations (`Mr`, `Mrs`, `Dr`, ...) appearing alone are never emitted as standalone candidates.

**Final filter on this extractor's output**
- Must appear ≥ `min_frequency` total occurrences.
- Capitalized-ratio (capitalized / total) must exceed `0.80`. Anything below 80% capitalized is probably a regular word that occasionally starts a sentence.
- Single-word candidates that are in the dictionary are dropped (multi-word survive even if individual tokens are dictionary words).
- Bare title abbreviations dropped a second time as a safety net.

### 2. `extract_uncommon_words`

Counts every non-stopword, non-blocked, non-special-case token in the text and emits each word that:

- Appears ≥ `min_frequency` times,
- Is NOT a key already produced by `extract_proper_nouns`,
- Is NOT in either English dictionary (when a dictionary is available),
- Has length > 2.

When **no dictionary** is available, falls back to the literary-frequency baseline: a word is dropped if `freq_checker.literary_zipf(word) >= max_zipf_mixed`. This keeps `honour` / `centre` from getting surfaced as OOV just because the en_GB dict didn't load.

Type guess: `TECHNICAL` if `count >= 3`, else `OTHER`. Detection reason: `not_in_dictionary`.

### 3. `extract_frequent_ngrams`

Scans bigrams and trigrams within each sentence. **Drops** an n-gram if any of these are true:

| Rule | Drop when... |
|------|--------------|
| Hard break inside | Any pair of adjacent tokens has a sequence-break char between them (comma, semicolon, colon, double quote) |
| All-stopword | Every token is in `STOPWORDS` |
| Special-case token | Any token is a single non-vowel letter or pure number |
| `DIALOGUE_VERBS` | Any token is `said`, `asked`, `replied`, ... or related narration glue |
| `BLOCKED_WORDS` | Any token is a day-of-week or month |
| Stopword at edge | First or last token is in `STOPWORDS` |
| `FINITE_VERBS` | Any token is `was`, `is`, `had`, `made`, `lived`, `named`, ... — signals a clause, not a name |
| `LEADING_PREPOSITIONS` | First token is `off`, `on`, `at`, `by`, `from`, `into`, ... |
| Lowercased title word leads | First token is lowercase AND in `TITLE_WORDS` (`his uncle, Captain Maurice` → reject `uncle Captain`) |
| `QUOTE_ATTRIBUTION` | Any token is `quote`, `wrote`, `writes`, `says`, ... |
| Frequency | Count below `min_frequency` |
| Already a proper noun | Lowercase n-gram key equals a `proper_noun_keys` entry |
| Conjoined names | Trigram is `Word and Word` or `Word or Word` |
| All dictionary words | Every token in dict (when dict available) — drops banal English phrases |
| Name-plus-dict noise | n-gram contains at least one known single-word proper noun key AND every non-proper token is a dictionary word. Possessives are stripped first so `Emile's comment` also matches. Catches `Jules looks`, `Uncle Paul let`, `Claire when`, `Emile Jules`. |

Surviving n-grams are emitted as `TECHNICAL` with detection reason `frequent_ngram`. Title periods are restored on the surface form before storage.

### 4. `extract_repeated_capitalized` (safety net)

Catches always-capitalized words that the proper-noun extractor missed — typically names that mostly appear at sentence starts.

**Admits** a word when:

- It's never seen lowercase in the text (`lower_counts[word] == 0`).
- It appears capitalized ≥ `min_frequency` times.
- It's not already in `already_found` (the union of keys from extractors 1, 2, 3).
- Length > 1.
- Not a bare title abbreviation.
- **Either** it's not in any dictionary (reason: `always_capitalized` + `not_in_dictionary`),
- **or** it IS in the dictionary AND its literary Zipf < `max_zipf_capitalized` (reason: `always_capitalized` + `rare_in_literary_english`).

The dict-but-rare-in-literary branch is what catches names like `Merlin` or `Mammon` that English dictionaries do contain but real fiction rarely uses. Type guess: `OTHER`.

### 5. `extract_rare_literary_words`

Surfaces dictionary words that are rare in literary English — archaic or domain-specific vocabulary that the other extractors would let through unflagged.

**Admits** a word when:

- It appears ≥ `min_frequency` times.
- Not in `already_found` (keys from extractors 1, 2, 3, 4).
- IS in at least one dictionary (otherwise extractor 2 would have got it).
- Length > 2.
- Not a stopword, blocked word, or special case.
- `freq_checker.literary_zipf(word) < max_zipf_mixed`.

Type guess: `TECHNICAL`. Detection reason: `rare_in_literary_english`.

## Post-merge passes

### Merge (`merge_candidates`)

For each duplicate key across the five extractor outputs:

- Keep the higher-priority type guess: `CHARACTER (5) > PLACE (4) > TECHNICAL (3) > CONCEPT (2) > OTHER (1)`.
- Union the detection reasons.
- Keep the **max** frequency seen.

### Possessive collapse (`collapse_possessive_keys`)

Strips trailing `'s`, `’s`, `ʼs`, `'`, `’`, `ʼ` from each key (and the surface form's last token), then merges all variants into the bare key. `Nelson` + `Nelson's` → `Nelson`, frequencies **summed**, reasons unioned, higher type-priority kept. Surface form prefers the non-possessive variant.

Processes bare keys first so the bare surface claims the merged slot. When only a possessive variant was emitted (`Hood's` alone), the collapse pass still surfaces it under the bare key `Hood` with the bare surface form.

### Contained-term pruning (`prune_contained_terms`)

Drops candidates whose every occurrence in the source text is contained inside a longer candidate. Implementation:

1. For each candidate, build a regex via `_term_pattern()` from `glossary_context.py` (apostrophe-normalized, word-boundary for singles, `[^A-Za-z0-9]+` between multi-word tokens to tolerate `dictator Aulus` matching `dictator, Aulus`).
2. Collect every match position across all candidates.
3. Sort by `(start_asc, length_desc)` and walk leftmost-longest: at each position past the last claim, the longest candidate starting there gets credit, everything overlapping is shadowed.
4. Drop any candidate whose **standalone** (unshadowed) hit count falls below `min_frequency`.

Python regex alternation is leftmost-first, not leftmost-longest, so a naive union regex would let `uncle Captain Maurice` shadow `Captain Maurice Suckling` whenever the two start at different positions. We sidestep that by collecting all matches and resolving overlaps explicitly.

Result: `Burnham` and `Thorpe` are dropped when every occurrence is inside `Burnham Thorpe`; `Captain Maurice` and `Maurice Suckling` are dropped when every occurrence is inside `Captain Maurice Suckling`. `London` survives next to `London Bridge` if it has at least `min_frequency` standalone occurrences.

### Demonym filter (`filter_demonyms`)

Two rules:

- **(a)** Single-word demonym (`British`, `Spaniards`, `Frenchmen`, `Englishman`, `Danes`, ...) → drop.
- **(b)** Multi-word phrase whose first token is a demonym AND every other token in the **surface** form is lowercase (`British ships`, `Spanish navy`) → drop.

Multi-word phrases with at least one capitalized non-demonym token survive: `British Empire`, `Spanish Inquisition`, `French Revolution`. The full `DEMONYMS` list is in [Word lists and constants](#word-lists-and-constants).

### Forced glossary candidates (`build_forced_candidates`)

After demonym filtering, the pipeline injects any terms listed in `forced_glossary_terms.json` that actually occur in the source. This lets you pin domain words that extraction heuristics would otherwise bury — for example, "stall" (*establo* vs *casilla*) or "gobbler" (*pavo macho* vs *guajolote*) — so they always appear in the candidate report.

**Setup:** copy `forced_glossary_terms.example.json` to `forced_glossary_terms.json` (gitignored) in the project root and add your terms:

```json
{
  "terms": [
    { "term": "stall", "type_guess": "TECHNICAL" },
    { "term": "gobbler", "type_guess": "OTHER" }
  ]
}
```

Fields per entry:

| Field | Required | Description |
|-------|----------|-------------|
| `term` | yes | Word or phrase to force. |
| `type_guess` | no | `CHARACTER`, `PLACE`, `TECHNICAL`, `CONCEPT`, or `OTHER` (default `OTHER`). |
| `detection_reasons` | no | Extra reasons merged with `forced_user_term`. |

**Matching rules:**
- Case-insensitive, whole-word.
- Single-word entries match with optional `-s` / `-es` plural suffix (`gobbler` → `gobblers`, `stall` → `stalls`).
- Multi-word phrases match exactly (no plural inflection).
- Terms with zero occurrences in the source are silently skipped.

**What forced injection bypasses:** min-frequency, contained-term pruning, and demonym filters. **What it still respects:** the existing-glossary exclusion list (`-g glossary.json`).

Detection reason added to every forced candidate: `forced_user_term`.

The feature is implemented in `build_forced_candidates()` (`scripts/extract_glossary_candidates.py`) and `load_forced_glossary_terms()` (`src/app_config.py`).

### Glossary exclusion (`exclude_glossary_terms`)

When `-g glossary.json` is provided, drops candidates whose lowercased key OR surface form matches any existing glossary term (via `Glossary.find_term()`). Excluded count is reported as `excluded_glossary_terms` in the JSON output.

## Scoring and ranking

After all filters, `score_and_rank()` computes a 0–1 score per surviving candidate:

| Component | Weight | What it measures |
|-----------|--------|------------------|
| `log(frequency+1) / log(max_freq+1)` | 0.35 | How often the term appears, log-normalized |
| OOV bonus | 0.25 | 1.0 if the key isn't in either English dictionary |
| Multi-word bonus | 0.20 | 1.0 if the key contains a space |
| Detection breadth | 0.10 | `min(len(detection_reasons) / 3, 1.0)` |
| Rare-in-literary bonus | 0.10 | 1.0 if `rare_in_literary_english` is in the reasons |

Candidates are sorted by `(score desc, frequency desc)` and truncated to `max_candidates`. Each surviving candidate has its `context_sentence` populated with the first sentence containing the term (truncated to ~200 chars with ellipses around the match if longer).

## Word lists and constants

These are defined as module-level sets at the top of `scripts/extract_glossary_candidates.py`. Modify them there and update this section if you make changes.

- **`TITLE_WORDS`** — title prefixes that flag the next capitalized sequence as `CHARACTER`. `mr`, `mrs`, `ms`, `miss`, `dr`, `prof`, `professor`, `uncle`, `aunt`, `auntie`, `brother`, `sister`, `father`, `mother`, `sir`, `lady`, `lord`, `king`, `queen`, `prince`, `princess`, `captain`, `major`, `colonel`, `general`, `sergeant`, `saint`, `st`.
- **`GEO_WORDS`** — geographic prefixes that flag the next capitalized sequence as `PLACE`. `mount`, `mt`, `lake`, `river`, `cape`, `bay`, `gulf`, `isle`, `island`, `valley`, `fort`, `port`, `north`, `south`, `east`, `west`, `new`, `old`, `great`, `upper`, `lower`.
- **`STOPWORDS`** — English stopwords used by n-gram filtering and rare-word extraction. Common articles, pronouns, auxiliaries, prepositions, conjunctions; see source for the full list.
- **`SEQUENCE_BREAKERS`** — capitalized words that shouldn't bridge a multi-word proper-noun sequence (`I`, `The`, `He`, `Yes`, `Oh`, prepositions, auxiliaries, ...).
- **`DIALOGUE_VERBS`** — verbs and connectives that signal narration glue rather than a glossary phrase (`said`, `asked`, `replied`, `exclaimed`, `cried`, `whispered`, ...).
- **`FINITE_VERBS`** — auxiliary and main verbs whose presence in an n-gram signals a clause fragment (`was`, `is`, `had`, `made`, `lived`, `named`, `knew`, `seemed`, ...).
- **`LEADING_PREPOSITIONS`** — prepositions/adverbs that often start a phrase fragment when they're not already in `STOPWORDS` (`off`, `against`, `back`, `down`, `through`, `across`, `around`, `under`).
- **`QUOTE_ATTRIBUTION`** — quote-attribution words that signal narration glue (`quote`, `wrote`, `writes`, `writing`, `says`, `say`, `said`).
- **`DEMONYMS`** — nationality words filtered after merge. `british`, `spanish`, `french`, `frenchman`, `frenchmen`, `spaniard`, `spaniards`, `danes`, `dane`, `portuguese`, `russian`, `italian`, `american`, `englishman`, `englishmen`, `dutch`, `austrian`, `austrians`, `prussian`, `prussians`, `indian`, `indians`.
- **`BLOCKED_WORDS`** — universally-known terms that never need a glossary entry. Days of the week (`monday`–`sunday`) and months (`january`–`december`).
- **`_ABBREV_NO_SPLIT`** — title abbreviations whose terminating `.` does NOT end a sentence. `mr`, `mrs`, `ms`, `dr`, `st`, `jr`, `sr`, `prof`, `rev`, `hon`, `capt`, `col`, `gen`, `lt`, `sgt`, `maj`.
- **`_SEQ_BREAK_CHARS`** — characters that break a multi-token sequence between adjacent tokens. `,  ;  :  "  “  ”`.

## CLI reference

```bash
python scripts/extract_glossary_candidates.py SOURCE -o OUTPUT [options]
```

### Core flags

| Flag | Default | Description |
|------|---------|-------------|
| `source_file` | required | Plain text file or project `source.txt`. Project paths auto-promote to `chunks/`/`chapters/`. |
| `-o`, `--output` | required | Output JSON path for the candidate report. |
| `-g`, `--glossary` | none | Existing `glossary.json` whose terms will be excluded. |
| `--min-frequency` | `2` | Minimum total occurrences for a term to qualify (and the threshold for contained-term pruning). |
| `--max-candidates` | `500` | Cap on candidates after scoring. |
| `-v`, `--verbose` | off | Print per-extractor counts and pipeline progress. |
| `--dry-run` | off | Print the summary but don't write the output file. |

### Frequency thresholds

| Flag | Default | What it controls |
|------|---------|------------------|
| `--max-literary-zipf-capitalized` | `4.0` | Max literary Zipf for `extract_repeated_capitalized` to admit a dictionary word as "rare in literary English". Higher = more permissive. |
| `--max-literary-zipf-mixed` | `3.0` | Max literary Zipf for `extract_rare_literary_words` (and for the no-dict fallback in `extract_uncommon_words`). Higher = more permissive. |

### Bootstrap (LLM proposal generation)

| Flag | Default | Description |
|------|---------|-------------|
| `--bootstrap` | off | After extraction, send candidates to an LLM, parse proposals, run interactive review, merge accepted terms into the project's `glossary.json`. |
| `--bootstrap-output` | `<output_dir>/glossary.json` | Where to write/merge the bootstrapped glossary. |
| `--prompt-out` | none | Build the bootstrap prompt and write it to this file, then exit (no LLM call). Works with or without `--bootstrap`. Useful for piping into another tool or pasting into a chat. |
| `--style-guide` | none | Style guide JSON whose `content` is interpolated into the prompt. |
| `--provider` | `anthropic` | LLM provider (`anthropic` or `openai`). |
| `--model` | `claude-sonnet-4-20250514` | Model name for the bootstrap call. |
| `--bootstrap-context-mode` | `full-text` | Prompt shape: `full-text` (flat list + first 10 KB of source) or `word` (per-term fragments, sorted by first appearance). See [Bootstrap prompts](#bootstrap-prompts). |
| `--bootstrap-fragments-per-term` | `2` | **Word-mode only.** Number of in-text fragments to attach to each candidate. |
| `--bootstrap-words-before` | `10` | **Word-mode only.** Word tokens to include before each match. |
| `--bootstrap-words-after` | `6` | **Word-mode only.** Word tokens to include after each match. |

When `--bootstrap` runs, each proposal is presented interactively:

```
  Nelson -> Nelson (character)
    Context: Eponymous protagonist; keep English form.
    Alternatives: Horacio Nelson
  [y]es / [n]o / [e]dit / [s]kip:
```

Accepted terms are merged into the target `glossary.json` (or created if it doesn't exist) without overwriting existing entries.

## Bootstrap prompts

The prompt is built by `build_glossary_prompt()` in `src/glossary_bootstrap.py`. Templates live in `prompts/`; the runtime resolver (`_resolve_prompt_path()`) prefers `prompts/<name>.txt` and falls back to `prompts/<name>.example.txt`, so you can override either template by dropping a customized non-example copy beside it.

### Full-text mode (`glossary_bootstrap.txt`)

The legacy shape. One line per candidate, plus the first 10 KB of source text appended for context. Each candidate line is:

```
- King Alfred (type guess: character, frequency: 7)
```

Use when you want the LLM to see a representative chunk of the book's prose. Cheaper to build (no per-term scan).

### Word mode (`glossary_bootstrap_word.example.txt`)

Per-term context fragments, sorted by first appearance in the book. Each candidate is rendered as a numbered header plus 1–2 short fragments showing the term in situ:

```
1. King Alfred  [character | freq=7]
   source: "KING ALFRED AND THE CAKES. [IMAGE:images/image_005 ..."
   source: "KING ALFRED AND THE CAKES. [IMAGE:images/image_005.jpg:KING ALFRED AND THE CAKES.] Many years ago ..."

2. Alfred  [other | freq=10]
   source: "KING ALFRED AND THE CAKES. [IMAGE:images/image_005 ..."
   ...
```

Fragments come from `find_first_word_contexts()` in `src/utils/glossary_context.py`: each match is expanded by `--bootstrap-words-before` word tokens before and `--bootstrap-words-after` after, using the original surrounding characters so punctuation and spacing are preserved. Fragments are bracketed with `...` when clipped. Candidates with no in-text match are emitted with `(no in-text context found)` and reported in the CLI summary.

The word-mode template references the book title and a context-unit label (e.g. `fragments (~10 words before / 6 words after)`), so the LLM knows how much surrounding context it's getting per term.

Use when you want the LLM to disambiguate meaning, register, or gender from real usage — particularly helpful for books with many characters or domain-specific vocabulary that's hard to judge from a single sample chunk.

### LLM response schema

Both prompts ask for a JSON array of objects, parsed by `parse_glossary_response()` (handles markdown code fences):

```json
[
  {
    "english": "Uncle Paul",
    "spanish": "Tío Pablo",
    "type": "character",
    "context": "Recurring uncle figure in the Mont Ventoux stories.",
    "alternatives": ["el tío Pablo"]
  }
]
```

`type` is normalized to one of `character`, `place`, `concept`, `technical`, `other`; unknown types fall back to `other`.

## Web UI controls

Dashboard wires the same pipeline into two endpoints:

- `POST /api/setup/<project_id>/extract-candidates` — accepts `{ zipf_offset: -1.0..+1.0 }`. The offset is added to both `max_zipf_capitalized` (default 4.0) and `max_zipf_mixed` (default 3.0). Clamped to `[-1, +1]` server-side. Returns the post-rank, post-cap candidate list (each item includes `term`, `type_guess`, `frequency`, and `context_sentence`).
- `POST /api/setup/<project_id>/prompts/glossary` and `POST /api/setup/<project_id>/.../generate` — accept `{ candidates, target_lang, glossary_guidance, context_mode }` and delegate to `_build_glossary_prompt_for_request()`. Word-mode requests scan the full project text on the server and enrich each candidate with fragments before building the prompt.

UI controls:

- **Sensitivity slider** (`extract-zipf-slider`) — range `-1..+1`, step `0.25`, sends `zipf_offset`. The readout next to it (`extract-zipf-readout`) shows the effective `mixed / capitalized` thresholds after offset.
- **Context mode select** (`glossary-context-mode`) — `full-text` or `word`. Read by both the copy/paste prompt button and the "Generate via API" button. Word mode disables the prompt button with a loading state while the server scans the book.

Web defaults (`words_before=10`, `words_after=6`, `fragments_per_term=2`) currently mirror the CLI defaults and are not user-tunable in the UI.

## Output schema

```json
{
  "source_file": "projects/mybook/source.txt",
  "total_words": 91342,
  "total_unique_words": 7900,
  "candidates": [
    {
      "term": "Uncle Paul",
      "type_guess": "character",
      "frequency": 96,
      "score": 0.935,
      "context_sentence": "The children gathered around Uncle Paul in the garden.",
      "detection_reasons": ["capitalized_sequence", "title_word_prefix"]
    }
  ],
  "excluded_glossary_terms": 11,
  "generated_at": "2026-05-16T10:30:00"
}
```

### Detection reasons reference

A candidate's `detection_reasons` list is the union of reasons across every extractor that surfaced it (after the merge pass). Possible values:

| Reason | Source | Meaning |
|--------|--------|---------|
| `capitalized_sequence` | proper nouns | Captured as a multi-word capitalized sequence. |
| `capitalized_mid_sentence` | proper nouns | Single capitalized token, not at sentence start. |
| `title_word_prefix` | proper nouns | Sequence is preceded by, or starts with, a `TITLE_WORDS` token. |
| `geo_word_prefix` | proper nouns | Sequence starts with a `GEO_WORDS` token. |
| `not_in_dictionary` | uncommon / proper / repeated-cap | Word isn't in `en_US` or `en_GB`. |
| `frequent_ngram` | n-grams | Survived all n-gram filters. |
| `always_capitalized` | repeated-cap safety net | Word appears capitalized in 100% of occurrences. |
| `rare_in_literary_english` | repeated-cap / rare-literary | Literary Zipf below the relevant threshold. |
| `forced_user_term` | forced glossary candidates | Term was listed in `forced_glossary_terms.json` and matched in the source. |

## Requirements

- **PyEnchant** with at least `en_US` installed; `en_GB` strongly recommended for British-spelling books. Without either, the dictionary-based extractors disable themselves and the frequency baseline picks up the slack — but candidate quality drops noticeably. See [DICTIONARY_SETUP.md](DICTIONARY_SETUP.md).
- **NLTK Brown + Gutenberg corpora** for the literary-frequency baseline. First-run setup:
  ```bash
  python -c "import nltk; nltk.download('brown'); nltk.download('gutenberg')"
  ```
  The compiled frequency distribution is cached at `~/.cache/translate_books/literary_freqdist.pkl` (version-tagged; rebuilt automatically if the cache version changes).
- **wordfreq** as a graceful fallback if NLTK isn't available. `pip install wordfreq`.

When all three (`enchant`, `nltk`, `wordfreq`) are present, every extractor runs. When only some are present, the script logs a warning and skips the relevant extractors but still produces output.
