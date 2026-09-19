"""Machine triage of the deterministic checkers.

The dictionary and grammar evaluators produce roughly 70% of all human
finding-clearing work and are right about one time in ten (7-8% and 16-19%
accept rates over ``_feedback.jsonl``, against 82% for the editorial judge).
This package reads each live finding with the sentence it fired on and asks a
model whether it is noise, so the reader sees the tenth finding rather than all
ten.

Three rules hold the design together:

- **Nothing is ever deleted.** A verdict is a sidecar record; the finding stays
  in ``evaluations/<chunk_id>.json`` exactly as the checker wrote it, so per-rule
  precision stays measurable and this pass's own error rate stays auditable.
- **The verdicts do not live in ``_feedback.jsonl``.** That file is the labelled
  corpus the cutoff is tuned against, and its records carry no author field. See
  ``web_ui.evaluations._TRIAGE_FILENAME``.
- **The pass runs on its own pinned model**, not the book's default backend, so
  it can move to local inference without touching any book's config.

Layout mirrors ``src/audit/``: :mod:`src.triage.findings` collects the work and
:mod:`src.triage.pass_` runs prepare / fanout / commit over it.
"""
