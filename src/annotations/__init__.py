"""
Annotation review: a post-human-review LLM pass over the notes a reader leaves.

Translation and the tailored judges (``src/judges``) both run *before* a human
reads the book. This package runs *after*: it reads the reader's own annotations
from ``projects/<slug>/annotations.jsonl``, researches each one against the style
guide, the glossary and the whole book, and drafts a resolution — a
recommendation for ``word_choice``, a book-wide verdict for ``inconsistency``, an
actual gloss for ``footnote``, an investigation for ``flag``.

It writes a dated markdown report and, only with an explicit selection, appends a
brief version of each finding back into the annotation itself. It never edits
translated prose — that is ``run_judges.py apply``'s job.
"""

from src.annotations.anchors import bare_hint, is_effectively_blank, parse_anchors
from src.annotations.store import (
    ANNOTATION_TYPES,
    append_record,
    load_active,
    target_key,
)

__all__ = [
    "ANNOTATION_TYPES",
    "append_record",
    "bare_hint",
    "is_effectively_blank",
    "load_active",
    "parse_anchors",
    "target_key",
]
