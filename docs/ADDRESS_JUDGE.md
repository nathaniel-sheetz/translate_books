# Forms-of-Address (usted/tú) Judge

Checks whether a Spanish translation addresses characters with the expected
register — formal **usted** vs. informal **tú** — throughout the book's dialogue.

Unlike the [dialogue judge](JUDGES_FRAMEWORK.md), which checks universal house
rules, the *correct* form here is **book-specific**: it depends on who is speaking
to whom, their relationship, whether the scene is public or private, and it can
change as the story progresses. So the judge checks against a reviewed, per-book
**address map** (`projects/<slug>/address_map.json`) rather than a fixed rules file.

There are two artifacts:

1. **The address map** — the per-book expectations, drafted and approved once (a
   harness beat), then reused for every judge run.
2. **The judge** (`address`) — a verdict judge that reads the map plus a universal
   detection rubric and flags lines whose register doesn't match.

## The address map

`projects/<slug>/address_map.json` (schema: `src.models.AddressMap`):

```json
{
  "content": "<prose the judge reads — states every pair's expectation plainly>",
  "pairs": [
    {
      "a": "Ricardo",
      "b": "Astrida",
      "relationship": "foster child ↔ guardian (later betrothed)",
      "directions": {
        "a_to_b": [ { "form": "tú", "when": "default", "since": "chapter_01" } ],
        "b_to_a": [
          { "form": "usted", "when": "public",  "notes": "formal deference before others" },
          { "form": "tú",    "when": "default" }
        ]
      }
    }
  ],
  "global_rules": "tú among family and close friends; usted to strangers, elders, and authority figures.",
  "version": "1.0"
}
```

Key design points:

- **Direction is independent.** `a_to_b` and `b_to_a` are separate ordered lists,
  so asymmetric address (a child says usted to a parent who says tú back) is
  expressible.
- **Situation matters.** Within a direction, rules are tried in order: the first
  whose `when` (e.g. `public` / `private` / a free-text situation) matches the
  scene wins. Every non-empty direction must end with a `when: "default"` rule so
  any scene resolves — this is enforced by the model validator.
- **Story evolution.** `since` / `until` (chapter ids) or an `after_event` note on
  a rule capture relationships that shift partway through the book.
- **v1 scope.** tú/usted singular only. voseo (`vos`) and plural address
  (`ustedes`/`vosotros`) are out of scope.

The judge reads the human-readable `content` prose (so it must state the
asymmetric/contextual rules plainly); `pairs` / `global_rules` are the structured
mirror for future UI / deterministic checks.

### Building the map (harness beat)

The map is drafted and approved once, like the style guide and glossary. It is an
**optional, non-blocking** beat in the translate-harness — translation proceeds
without it — and can also be built standalone whenever you want to run the judge:

```bash
# Samples the book's highest interpersonal-dialogue chapters (a spread across the
# whole book) and renders a drafting prompt.
python scripts/harness.py address-map prepare --project <slug>

# (Draft the map JSON to the printed draft_path, refine with the user.)

# Validates the draft against the AddressMap schema and writes address_map.json.
python scripts/harness.py address-map commit --project <slug>
```

`prepare` seeds the prompt from the glossary's cast, the full style guide
(`style.json` content), and a whole-book spread of dialogue-dense chapters
(chosen by `src/harness/address_sample.py`), so the map reflects relationships as
they actually play out — including how they change — not just the opening chapters.

## Running the judge

Same CLI and backends as the dialogue judge (`scripts/run_judges.py`); just pass
`--judge address`. The CLI loads `address_map.json` into the judge context
automatically. If the map is missing, it returns `status: "error"` telling you to
build it first.

```bash
# API backend (cost-gated); add --persist --confirm to save findings + light badges.
python scripts/run_judges.py run --project <slug> --judge address --scope chapter:chapter_03

# Subagent backend (no API spend): prepare -> spawn judge-workers -> commit.
python scripts/run_judges.py prepare --project <slug> --judge address --scope chapter:chapter_03
python scripts/run_judges.py commit  --project <slug> --persist
```

Findings persist to `evaluations/<chunk>.json` under `judges.address`, fold into
the dashboard badges, and render in the reader (enable the **Address (usted/tú)**
review type). Approved findings can be applied to the text with
`run_judges.py apply --judge address`.

## Severity policy (broad coverage)

- **error** — a clear violation of an *explicit* pair expectation
  (`wrong-form-usted-expected` / `wrong-form-tu-expected`, or a clear
  `inconsistent-address`).
- **warning** — a likely violation inferred from the map's `global_rules` when no
  explicit pair covers the addressee (`global-rule-violation`).
- **info** — `ambiguous` cases where the relationship or addressee is unclear.

Scoring reuses the shared severity-weighted, per-rule-capped compliance score in
`src/judges/scoring.py` (also used by the dialogue judge).

## Files

| Purpose | Path |
|---|---|
| Data model | `src/models.py` (`AddressMap` / `AddressPair` / `AddressRule`) |
| Load/save + validate | `src/utils/file_io.py`, `src/harness_guard.py` (`validate_address_map_file`) |
| Chapter sampler | `src/harness/address_sample.py` |
| Harness beat | `src/harness/flow.py` (`address_map_prepare` / `address_map_commit`), `scripts/harness.py` |
| Drafting prompt | `prompts/address_map_generate.txt` |
| Judge | `src/judges/address_judge.py`, `src/judges/registry.py` |
| Judge prompts | `prompts/address_forms.txt` (rubric), `prompts/judge_address.txt`, `prompts/judge_address_batch.txt` |
| Backend wiring | `scripts/run_judges.py` (`_build_judge_context`) |
| Shared scoring | `src/judges/scoring.py` |
