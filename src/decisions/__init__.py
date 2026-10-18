"""
The decisions stage: find the book-wide translation calls before translation.

Phase 2 of the decision-taxonomy plan. The scan (:mod:`src.decisions.scan`)
runs one headless job per English chapter, read against the taxonomy in
``prompts/decisions_taxonomy.json``, producing candidate decisions. The
consolidate step (:mod:`src.decisions.consolidate`) runs one job per category,
folding those candidates into ranked gate questions, routed items and house-rule
proposals. The rulings ledger and the harness wiring come later.
"""

from pathlib import Path

#: Name of this stage's working directory under ``<project>/.harness/``.
DIRNAME = "decisions"


def decisions_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / DIRNAME
