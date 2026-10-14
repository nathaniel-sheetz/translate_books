"""
Load the decision taxonomy and render it for the scan prompt.

Per-user like the house rules (:func:`src.judges.context._house_rules_path`): the
operator's ``prompts/decisions_taxonomy.json`` is gitignored because its examples
name real books, and the ``.example`` twin is what a fresh clone has.

Every example in the operator's file carries the ``project`` it was mined from.
:func:`load_taxonomy` drops a book's own examples when asked to, so scanning a
finished book never shows the scanner the answer it is being measured on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
TAXONOMY_FILE = _PROMPTS_DIR / "decisions_taxonomy.json"
TAXONOMY_EXAMPLE_FILE = _PROMPTS_DIR / "decisions_taxonomy.example.json"

#: The category a scan may use when nothing in the taxonomy fits.
OTHER = "other"


def taxonomy_path() -> Path:
    """The operator's taxonomy if present, else the checked-in example."""
    if TAXONOMY_FILE.exists():
        return TAXONOMY_FILE
    return TAXONOMY_EXAMPLE_FILE


def load_taxonomy(
    *, exclude_project: Optional[str] = None, path: Optional[Path] = None
) -> dict[str, Any]:
    """The taxonomy document, with ``exclude_project``'s examples removed.

    Raises ``FileNotFoundError`` / ``ValueError`` rather than degrading to an
    empty list: a scan with no taxonomy has nothing to scan for.
    """
    path = Path(path) if path else taxonomy_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    categories = doc.get("categories") if isinstance(doc, dict) else None
    if not isinstance(categories, list) or not categories:
        raise ValueError(f"{path} has no 'categories' list")
    kept: list[dict[str, Any]] = []
    held_out = 0
    for cat in categories:
        if not isinstance(cat, dict) or not cat.get("id"):
            continue
        cat = dict(cat)
        examples = [e for e in (cat.get("examples") or []) if isinstance(e, dict)]
        if exclude_project:
            mine = [e for e in examples if e.get("project") == exclude_project]
            held_out += len(mine)
            examples = [e for e in examples if e.get("project") != exclude_project]
        cat["examples"] = examples
        kept.append(cat)
    return {**doc, "categories": kept, "path": str(path), "held_out_examples": held_out}


def category_ids(taxonomy: dict[str, Any]) -> list[str]:
    return [c["id"] for c in taxonomy.get("categories") or []]


def format_taxonomy(taxonomy: dict[str, Any]) -> str:
    """The categories as prompt text: id, description, cues, default, examples.

    Examples go in without their ``project`` tag. The scanner needs the shape of
    the decision, not which book it came from.
    """
    blocks: list[str] = []
    for cat in taxonomy.get("categories") or []:
        lines = [f"### {cat['id']} — {cat.get('name') or cat['id']}"]
        if cat.get("description"):
            lines.append(str(cat["description"]).strip())
        cues = [str(c).strip() for c in cat.get("detection_cues") or [] if str(c).strip()]
        if cues:
            lines.append("detection_cues:")
            lines += [f"- {c}" for c in cues]
        if cat.get("house_default"):
            lines.append(f"house_default: {str(cat['house_default']).strip()}")
        examples = [
            str(e.get("text") or "").strip()
            for e in cat.get("examples") or []
            if str(e.get("text") or "").strip()
        ]
        if examples:
            lines.append("examples from earlier books:")
            lines += [f"- {e}" for e in examples]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
