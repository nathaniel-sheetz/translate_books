"""
The house rules, and the book's passage-scoped rules, as the translator reads them.

Two blocks, placed for the prompt cache:

- **HOUSE RULES** carries the global house rules (no ``triggers``). It sits in
  the fixed prefix, so it is byte-identical across every chunk of a book and
  cached after the first.
- **NOTES FOR THIS PASSAGE** carries what applies to this chunk only: the house
  rules its English triggers, the book's own rules whose chapter window or
  triggers cover it, and the forms of address that change partway through the
  book (an address-map direction with a ``since`` / ``until`` window). It sits
  after the glossary, in the per-chunk suffix, and is empty (no header at all)
  in a chunk nothing applies to.

The translator is never told which chapter it is in; a windowed rule simply
reaches only the chunks of the chapters it covers (:mod:`src.house.scope`).

Mechanical and draft-only rules never reach either block
(:func:`src.judges.context.rules_for` with the translator audience).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SEPARATOR = "=" * 80


def _line(rule: dict[str, Any]) -> str:
    text = str(rule.get("rule") or rule.get("text") or "").strip()
    note = str(rule.get("note") or "").strip()
    return f"- {text}" + (f" {note}" if note else "")


@dataclass(frozen=True)
class AddressDirection:
    """One address-map direction whose form changes partway through the book.

    ``speaker`` / ``addressee`` are the map's (target-language) names;
    ``speaker_forms`` / ``addressee_forms`` are what to look for in the English
    (the glossary's English for that name, and the name itself).
    """

    speaker: str
    addressee: str
    speaker_forms: tuple[str, ...]
    addressee_forms: tuple[str, ...]
    rules: tuple[Any, ...]  # AddressRule, in the map's first-match order

    def line_for(self, source_text: str, chapter_id: Optional[str]) -> str:
        """The direction as one line, or ``""`` when it does not apply here."""
        from src.house.match import any_trigger
        from src.house.scope import rule_in_scope

        if not chapter_id:
            return ""
        if not (any_trigger(list(self.speaker_forms), source_text)
                or any_trigger(list(self.addressee_forms), source_text)):
            return ""
        live = [r for r in self.rules if rule_in_scope(r, chapter_id)]
        if not live:
            return ""
        # Form and condition only: a rule's ``notes`` are the map's rationale,
        # for the judge and the human, and would double every line.
        parts = [rule.form if rule.when == "default" else f"{rule.form} {rule.when.strip()}"
                 for rule in live]
        if len(parts) > 1 and live[-1].when == "default":
            parts[-1] = "otherwise " + parts[-1]
        return f"- {_with_english(self.speaker, self.speaker_forms)} → " \
               f"{_with_english(self.addressee, self.addressee_forms)}: " + "; ".join(parts)


def _with_english(name: str, forms: tuple[str, ...]) -> str:
    english = [f for f in forms if f.lower() != name.lower()]
    return f"{name} ({english[0]})" if english else name


@dataclass(frozen=True)
class TranslatorHouse:
    """The house rules and passage-scoped book rules one book's translator prompts carry."""

    global_rules: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    triggered: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    book_scoped: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    address: tuple[AddressDirection, ...] = field(default_factory=tuple)

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

    def notes_for(self, source_text: str, chapter_id: Optional[str] = None) -> list[dict[str, Any]]:
        """The house and book rules that apply to this passage, house first."""
        from src.judges.context import rule_triggers
        from src.house.match import any_trigger
        from src.house.scope import rule_in_scope

        house = [r for r in self.triggered if any_trigger(rule_triggers(r), source_text)]
        book = [
            r for r in self.book_scoped
            if rule_in_scope(r, chapter_id)
            and (not rule_triggers(r) or any_trigger(rule_triggers(r), source_text))
        ]
        return house + book

    def address_lines(self, source_text: str, chapter_id: Optional[str] = None) -> list[str]:
        return [line for d in self.address if (line := d.line_for(source_text, chapter_id))]

    def notes_block(self, source_text: str, chapter_id: Optional[str] = None) -> str:
        """The framed NOTES FOR THIS PASSAGE section, or ``""`` when nothing applies.

        ``chapter_id`` is the chunk's chapter; without it (a sentence
        retranslation) only the triggered rules can apply.
        """
        lines = [_line(r) for r in self.notes_for(source_text, chapter_id)]
        address = self.address_lines(source_text, chapter_id)
        if not lines and not address:
            return ""
        body = "These apply to this passage in particular, alongside the style guide:"
        if lines:
            body += "\n\n" + "\n".join(lines)
        if address:
            body += "\n\nForms of address at this point in the book:\n" + "\n".join(address)
        return f"{_SEPARATOR}\nNOTES FOR THIS PASSAGE\n{_SEPARATOR}\n\n{body}"


def load_translator_house(project_dir: Optional[Path]) -> TranslatorHouse:
    """The translator-facing rules for this book: house global/triggered, book scoped, address."""
    from src.judges.context import (
        AUDIENCE_TRANSLATOR,
        load_book_rules,
        rule_triggers,
        rules_for,
        split_global_triggered,
    )
    from src.house.scope import has_window

    global_rules, triggered = split_global_triggered(
        rules_for(project_dir, None, audience=AUDIENCE_TRANSLATOR)
    )
    # A book rule with neither a window nor triggers restates the style guide
    # (which the translator already has) for the judges, so only scoped ones ship.
    book_scoped = [
        r for r in (load_book_rules(project_dir) if project_dir is not None else [])
        if (has_window(r) or rule_triggers(r)) and str(r.get("rule") or r.get("text") or "").strip()
    ]
    return TranslatorHouse(
        tuple(global_rules), tuple(triggered), tuple(book_scoped),
        tuple(_address_directions(project_dir)),
    )


def _address_directions(project_dir: Optional[Path]) -> list[AddressDirection]:
    """The address-map directions with a chapter window, ready to match against a chunk."""
    if project_dir is None:
        return []
    map_path = Path(project_dir) / "address_map.json"
    if not map_path.exists():
        return []
    from src.house.scope import has_window
    from src.utils.file_io import load_address_map

    try:
        amap = load_address_map(map_path)
    except Exception as exc:  # noqa: BLE001 - an unusable map must not stop translation
        logger.warning("Ignoring unreadable %s for the translator notes: %s", map_path, exc)
        return []
    english = _english_names(Path(project_dir))
    out: list[AddressDirection] = []
    for pair in amap.pairs:
        for key, speaker, addressee in (("a_to_b", pair.a, pair.b), ("b_to_a", pair.b, pair.a)):
            rules = pair.directions.get(key) or []
            if not any(has_window(r) for r in rules):
                continue
            out.append(AddressDirection(
                speaker=speaker,
                addressee=addressee,
                speaker_forms=_name_forms(speaker, english),
                addressee_forms=_name_forms(addressee, english),
                rules=tuple(rules),
            ))
    return out


def _english_names(project_dir: Path) -> dict[str, list[str]]:
    """``{target-language name (lower): [English keys]}`` from the glossary."""
    path = project_dir / "glossary.json"
    if not path.exists():
        return {}
    from src.utils.file_io import load_glossary

    try:
        glossary = load_glossary(path)
    except Exception as exc:  # noqa: BLE001 - names fall back to the map's own
        logger.warning("Ignoring unreadable %s for address names: %s", path, exc)
        return {}
    names: dict[str, list[str]] = {}
    for term in glossary.terms:
        for spanish in (term.spanish, *term.alternatives):
            names.setdefault(spanish.strip().lower(), []).append(term.english)
    return names


def _name_forms(name: str, english: dict[str, list[str]]) -> tuple[str, ...]:
    forms = [name, *english.get(name.strip().lower(), [])]
    return tuple(dict.fromkeys(f for f in forms if f.strip()))
