"""
The house rules as the translator reads them.

Two blocks, placed for the prompt cache:

- **HOUSE RULES** carries the global rules (no ``triggers``). It sits in the
  fixed prefix, so it is byte-identical across every chunk of a book and
  cached after the first.
- **HOUSE NOTES** carries the triggered rules whose triggers occur in this
  chunk's English. It sits after the glossary, in the per-chunk suffix, and is
  empty (no header at all) in a chunk that triggers nothing.

Mechanical and draft-only rules never reach either block
(:func:`src.judges.context.rules_for` with the translator audience).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_SEPARATOR = "=" * 80


def _line(rule: dict[str, Any]) -> str:
    text = str(rule.get("rule") or rule.get("text") or "").strip()
    note = str(rule.get("note") or "").strip()
    return f"- {text}" + (f" {note}" if note else "")


@dataclass(frozen=True)
class TranslatorHouse:
    """The house rules one book's translator prompts carry."""

    global_rules: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    triggered: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def rules_block(self) -> str:
        """The framed HOUSE RULES section, or ``""`` when there are none."""
        lines = [_line(r) for r in self.global_rules if str(r.get("rule") or r.get("text") or "").strip()]
        if not lines:
            return ""
        return (
            f"{_SEPARATOR}\nHOUSE RULES\n{_SEPARATOR}\n\n"
            "These hold in every book of this house, alongside the style guide:\n\n"
            + "\n".join(lines)
        )

    def notes_for(self, source_text: str) -> list[dict[str, Any]]:
        from src.judges.context import rule_triggers
        from src.house.match import any_trigger

        return [r for r in self.triggered if any_trigger(rule_triggers(r), source_text)]

    def notes_block(self, source_text: str) -> str:
        """The framed HOUSE NOTES section for one chunk, or ``""`` when nothing triggers."""
        lines = [_line(r) for r in self.notes_for(source_text)]
        if not lines:
            return ""
        return (
            f"{_SEPARATOR}\nHOUSE NOTES FOR THIS PASSAGE\n{_SEPARATOR}\n\n"
            "These house rules apply because the passage contains what they cover:\n\n"
            + "\n".join(lines)
        )


def load_translator_house(project_dir: Optional[Path]) -> TranslatorHouse:
    """The house rules for this book's translator prompts, split global/triggered."""
    from src.judges.context import AUDIENCE_TRANSLATOR, rules_for, split_global_triggered

    global_rules, triggered = split_global_triggered(
        rules_for(project_dir, None, audience=AUDIENCE_TRANSLATOR)
    )
    return TranslatorHouse(tuple(global_rules), tuple(triggered))
