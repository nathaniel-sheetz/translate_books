"""
footnote-pass: author new editorial footnotes for a translated book.

The counterpart to ``src.annotations``, which *reviews* notes a reader already
left. Nothing there can mint one: ``review.apply`` only replaces the text of an
annotation the review already drafted against. This package is the create path —
infer the book's own footnote style from the notes it already carries, scan
chapters for more spots that deserve the same treatment, and append validated
``type: "footnote"`` records through ``src.annotations.store``.

Five modules, each one stage of that:

- :mod:`corpus` — read the existing footnotes as a style-inference corpus.
- :mod:`scan`   — render scan prompts, fan them out, collect candidates.
- :mod:`ledger` — the append-only log of proposals, keeps, and drops.
- :mod:`write`  — ``add`` and ``verify``: the validated write, and the audit.
- :mod:`report` — the dated markdown candidate report.

The validation in :mod:`write` is the reason the package exists. ``src/endnotes.py``
drops a footnote silently in three ways — no alignment row for the ``es_idx``, an
aligned sentence not findable in the chapter body, and (logging nothing at all) a
note whose display text is empty once the ``[anchor]`` bracket is stripped. Every
one of those is a named error here, before the record reaches the file.
"""

from __future__ import annotations

FOOTNOTE_TYPE = "footnote"

# Stamped on every record this package writes, so a later audit can tell a
# skill-authored note from a reader's own and from a Gutenberg import. Only
# ``origin == "gutenberg"`` is special-cased downstream
# (``src/annotations/targets.py``), so this value is inert by design.
ORIGIN = "footnote_pass"

__all__ = ["FOOTNOTE_TYPE", "ORIGIN"]
