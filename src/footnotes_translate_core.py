"""Backend-agnostic helpers for translating imported Gutenberg footnote bodies.

Both footnote-translation backends share this module:

- the metered **API** path (``scripts/translate_footnotes.py`` → ``call_llm``), and
- the no-API-key **subagent / headless** path (``src/harness/flow.py`` →
  ``run_headless_wave`` / Task workers).

Everything here is pure prompt/parse/batch logic plus two small project-file
readers — no network call lives in this module, so either backend can reuse it
without pulling in an API client. Keeping it out of ``footnote_import`` (which is
about *detecting* footnotes in HTML) avoids a dependency from that pure-text
module onto the glossary/style readers.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.utils.file_io import load_glossary, load_style_guide

# Keep each batch's source text under this many characters so the model's reply
# stays manageable and easy to reconcile.
_BATCH_CHAR_BUDGET = 6000

_LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.*)$")


def batch_notes(notes: list[dict], char_budget: int = _BATCH_CHAR_BUDGET) -> list[list[dict]]:
    """Group untranslated notes into batches under a character budget."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for n in notes:
        body = n.get("source_body") or ""
        if current and size + len(body) > char_budget:
            batches.append(current)
            current, size = [], 0
        current.append(n)
        size += len(body)
    if current:
        batches.append(current)
    return batches


def build_footnotes_prompt(
    notes: list[dict],
    *,
    source_language: str,
    target_language: str,
    title: str,
    glossary_text: str = "",
    style_text: str = "",
) -> str:
    """Render the batch translation prompt for a group of footnotes."""
    parts = [
        f'You are a professional literary translator. Translate these footnotes '
        f'from {source_language} to {target_language} for the book "{title}".',
        "",
        "Rules:",
        f"- Translate each numbered note into natural, fluent {target_language}.",
        "- Preserve _underscore italics_ exactly (they mark italic text).",
        "- Preserve any [IMAGE:...] tokens verbatim.",
        "- Do NOT add, drop, merge, or renumber notes.",
        "- Return EXACTLY one line per note, formatted as:  N| <translation>",
        "",
    ]
    if glossary_text.strip():
        parts += ["GLOSSARY (use these renderings):", glossary_text.strip(), ""]
    if style_text.strip():
        parts += ["STYLE GUIDE:", style_text.strip(), ""]
    parts.append("FOOTNOTES:")
    for n in notes:
        parts.append(f"{n['number']}| {n.get('source_body') or ''}")
    return "\n".join(parts)


def parse_numbered_translations(response: str) -> dict[int, str]:
    """Parse ``N| translation`` lines, joining wrapped continuation lines."""
    out: dict[int, str] = {}
    current: int | None = None
    for line in (response or "").splitlines():
        m = _LINE_RE.match(line)
        if m:
            current = int(m.group(1))
            out[current] = m.group(2).strip()
        elif current is not None and line.strip():
            out[current] = (out[current] + " " + line.strip()).strip()
    return out


def read_glossary_text(project_dir: Path) -> str:
    """Format ``glossary.json`` for the prompt (empty string when absent/unreadable)."""
    path = Path(project_dir) / "glossary.json"
    if not path.exists():
        return ""
    try:
        from src.api_translator import format_glossary_for_prompt  # lazy: avoid import cycle
        return format_glossary_for_prompt(load_glossary(path))
    except Exception:
        return ""


def read_style_text(project_dir: Path) -> str:
    """Read the style-guide body for the prompt (empty string when absent/unreadable)."""
    path = Path(project_dir) / "style.json"
    if not path.exists():
        return ""
    try:
        return load_style_guide(path).content
    except Exception:
        return ""
