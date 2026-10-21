"""Book review: read a book's judge findings together and name what is systemic.

One judge finding says "this sentence is wrong". Thirty of them can say
something no single one does: that a glossary entry never reached the
translator, that the style guide and the address map disagree, that a rule was
written after the chapters it is judged against. This package collects every
judge finding a book has, runs coded detectors over the whole set, and reports
the patterns -- each with the evidence an operator (or a synthesis model) needs
to decide what to change upstream.

Nothing here edits the book or deletes a finding.
"""

from __future__ import annotations

from pathlib import Path


def review_dir(project_dir: Path) -> Path:
    """Working files for this stage: ``<project>/.harness/book_review/``."""
    return Path(project_dir) / ".harness" / "book_review"


def reports_dir(project_dir: Path) -> Path:
    """Where the human-readable report lands: ``<project>/reports/``."""
    return Path(project_dir) / "reports"
