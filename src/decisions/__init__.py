"""
The decisions stage: find the book-wide translation calls before translation.

Phase 2 of the decision-taxonomy plan. Today only the scan exists
(:mod:`src.decisions.scan`): one headless job per English chapter, read against
the taxonomy in ``prompts/decisions_taxonomy.json``, producing candidate
decisions. Merging them into ranked cards, the rulings ledger and the harness
wiring come after the scan's recall has been checked against the mined gold set.
"""

from pathlib import Path

#: Name of this stage's working directory under ``<project>/.harness/``.
DIRNAME = "decisions"


def decisions_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / DIRNAME
