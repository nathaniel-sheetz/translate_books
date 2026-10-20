"""
The last mile to a Kindle listing: book facts, Amazon evidence, the KDP form.

``scripts/kdp_publish.py`` is the one CLI. The agent does the judgment work
(positioning, keyword seeds, the description, picking categories); these
modules do everything deterministic and every network fetch, so no Amazon page
text ever reaches a model — only the fields extracted from it.

Per-book working files live in ``projects/<slug>/.harness/kdp/``; the approved
listing is kept beside ``project.json`` as ``kdp_listing.json``.

The per-user defaults (canonical translator name, imprint, title descriptors)
and the example descriptions are gitignored, with tracked ``.example`` twins a
fresh clone reads instead — see ``prompts/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

DEFAULTS_FILE = PROMPTS_DIR / "kdp_defaults.json"
DEFAULTS_EXAMPLE_FILE = PROMPTS_DIR / "kdp_defaults.example.json"
EXAMPLES_FILE = PROMPTS_DIR / "kdp_description_examples.md"
EXAMPLES_EXAMPLE_FILE = PROMPTS_DIR / "kdp_description_examples.example.md"

LISTING_FILE = "kdp_listing.json"


def work_dir(project_dir: Path) -> Path:
    """``projects/<slug>/.harness/kdp/`` (not created here)."""
    return Path(project_dir) / ".harness" / "kdp"


def load_defaults() -> dict[str, Any]:
    """The per-user defaults, else the tracked example. ``{}`` if both are unusable."""
    for path in (DEFAULTS_FILE, DEFAULTS_EXAMPLE_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_source"] = path.name
            return data
    return {}


def examples_path() -> Path:
    """The description examples the drafting step reads: the user's own, else the example."""
    return EXAMPLES_FILE if EXAMPLES_FILE.exists() else EXAMPLES_EXAMPLE_FILE
