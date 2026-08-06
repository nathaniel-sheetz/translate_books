"""
Book-wide rollups of a project's annotations, for list pages.

The chapter list already badges per-chapter annotation counts; the reader home
page needs the same numbers rolled up per book, plus one the chapter list does
not compute: how many ``footnote`` marks carry an anchor and nothing else.

Those blank footnotes matter because ``src/endnotes.py`` silently drops them
from the built EPUB — a note the reader placed but never wrote is lost content,
not a cosmetic gap.

Deliberately free of any ``web_ui`` import, like the rest of this package.
"""

from __future__ import annotations

from pathlib import Path

from src.annotations.anchors import is_effectively_blank
from src.annotations.store import ANNOTATION_TYPES, load_active

# The three types the reader folds into one "to review" count. ``footnote`` is
# excluded on purpose: it feeds endnotes rather than a review decision.
REVIEW_ANNOTATION_TYPES = ("word_choice", "inconsistency", "flag")


def project_annotation_summary(project_dir: Path) -> dict[str, int]:
    """Roll a book's live annotations up into the two counts a card shows.

    Args:
        project_dir: ``projects/<slug>/``.

    Returns:
        ``{"awaiting_review": int, "empty_footnotes": int}``.

        ``awaiting_review`` is every live :data:`REVIEW_ANNOTATION_TYPES`
        annotation — the same number the chapter list badges as "to review",
        summed over chapters. ``empty_footnotes`` is every live ``footnote``
        whose content is nothing but its ``[anchor]``.

    Unknown ``type`` values coerce to ``flag`` (and so count as awaiting
    review), matching how the reader treats them.
    """
    awaiting_review = 0
    empty_footnotes = 0

    for record in load_active(Path(project_dir)):
        ann_type = record.get("type")
        if ann_type not in ANNOTATION_TYPES:
            ann_type = "flag"
        if ann_type in REVIEW_ANNOTATION_TYPES:
            awaiting_review += 1
        elif ann_type == "footnote" and is_effectively_blank(record.get("content") or ""):
            empty_footnotes += 1

    return {"awaiting_review": awaiting_review, "empty_footnotes": empty_footnotes}
