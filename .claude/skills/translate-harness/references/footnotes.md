# Footnotes — translate + apply / drop

Load this only when `footnotes.json` exists and `config.footnotes_decision` is not `none`/`drop`. Rarely needed.

## Step 4C — Footnotes: translate the notes, then embed them (only if imported)

**Skip this whole step** when `config.footnotes_decision` is `none` or `drop`, or when
`footnotes.json` is absent. Otherwise load it only if footnotes were imported
(`projects/<slug>/footnotes.json` exists / `setup` reported `footnotes_mode: import` and the user
didn't drop them). This runs **after** the chapters are translated **and aligned** (the embed reads
`alignments/`), and **before** the final EPUB.

Footnotes translate on the **same backend you chose for the chapters** (`references/translate-api.md` / `references/translate-workers.md`) — the note bodies
never force a different path. `footnotes translate` resolves it automatically from
`config.backend` / the `backend` run-log beat (`--backend {auto,api,headless,subagent}`,
default `auto`; pass an explicit value only to override). Once the chapters are done,
**STOP and ask the user (its own beat): "Translate the footnotes now?"** — a separate-turn
go-ahead, never folded into an earlier approval. Notes are few and short.

- **Yes** → in a later turn, translate on the resolved backend, then embed. Pick the matching path:

  - **API backend** — a metered step (like `references/translate-api.md`): confirm cost in this beat, then
    ```bash
    python scripts/harness.py footnotes translate --project projects/<slug> --yes
    ```
    `footnotes translate` refuses without `--yes` on the API backend.
  - **Headless backend** — no dollars (subscription usage, enforced at launch: the wave
    refuses to start on a metered login), so no `--yes`. One command runs a
    `claude -p` wave and writes the bodies back:
    ```bash
    python scripts/harness.py footnotes translate --project projects/<slug> \
      [--effort low|medium|high|xhigh|default] [--prompt-cache auto|5m|1h|off]
    ```
    Footnote waves run at `--effort high` by default, from their own key
    `headless_effort_footnotes` (`config-set` to persist, `--effort` for one
    run). Separate from the judge/annotation keys, and separate from
    `headless_effort_translate` — notes are short, so this is the cheapest
    place to try a lower level, but it is still book prose and still
    unmeasured. See `docs/LLM_PROVIDERS.md`.
  - **Subagent (Task) backend** — spawn workers exactly like the chapters:
    ```bash
    python scripts/harness.py footnotes translate-prepare --project projects/<slug>
    ```
    Then spawn one `translator` Task subagent per manifest `entries[]` item (`.claude/agents/translator.md`,
    pinned `worker_model`): *"Read `<prompt_path>`. Write ONLY the `N| <translation>` lines to
    `<draft_path>`. Reply `done <batch_id>`."* Then land them:
    ```bash
    python scripts/harness.py footnotes translate-commit --project projects/<slug>
    ```
    `translate-commit` reports `committed` / `pending`; re-prepare + re-spawn any `pending` notes.

  In every case, once the bodies are filled, embed them:
  ```bash
  python scripts/harness.py footnotes apply --project projects/<slug>
  ```
  `footnotes apply` converts every surviving `[FOOTNOTE:N]` token into an anchored reader footnote,
  strips the raw tokens from the stored translation, and **rebuilds the EPUB** with a numbered
  back-matter section.
- **No** → still run **`footnotes apply`** alone, so the raw `[FOOTNOTE:N]` tokens don't leak into
  the EPUB as literal text. Warn the user the notes will appear in the **source language**
  (untranslated); they can translate them later (`footnotes translate` [`--yes` on the API backend],
  then `footnotes apply`).

`footnotes apply` is idempotent — on the **API path**, `translate` already auto-ran the footnotes
stage once (with source text, since the bodies weren't translated yet), and re-applying simply
re-converts with the translated bodies (prior imported-footnote annotations are replaced). Running
`references/epub.md` `epub` afterward stays consistent — it renders the persisted footnote annotations.
