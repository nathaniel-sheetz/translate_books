"""
Where in the whole book the speech and framing decisions are.

The style-guide follow-ups are drafted from the opening chapters, so a passage
that needs its own rule (two farmers in minstrel dialect, labelled "negro" and
"colored", in chapter 9 of Hunting for Hidden Gold) was never seen before the
guide was approved. This scans every chapter for two signals and names the
chapters, with a few lines each, for the follow-up and draft prompts:

- **period ethnic and racial framing**: a short list of terms such books use;
- **eye dialect** well above the book's own typical chapter: the difficulty
  scorer's markers plus the minstrel spellings it does not count (gwine, mah,
  heah). Relative to the book's median so a book written in dialect throughout,
  which the opening sample already shows, does not flag every chapter.
"""

from __future__ import annotations

import re
from pathlib import Path
from statistics import median
from typing import Any

#: Period terms for race and ethnicity that call for a rendering decision.
#: "colored" counts only before a person noun ("colored lights" is not one).
FRAMING_TERMS: dict[str, str] = {
    "negro": r"negro(?:es)?",
    "colored": r"colou?red\s+(?:man|men|woman|women|boy|boys|girl|girls|folks?|people|fellow|fellows|porter|servant)",
    "darky": r"dark(?:e?y|ies)",
    "pickaninny": r"pickaninn(?:y|ies)",
    "n-word": r"niggers?",
    "white trash": r"white\s+trash",
    "Injun": r"injuns?",
    "redskin": r"redskins?",
    "squaw": r"squaws?",
    "half-breed": r"half-?breeds?",
    "Chinaman": r"chinam[ae]n",
    "gypsy": r"g[iy]ps(?:y|ies)",
    "mammy": r"mammy",
}
_FRAMING_RE = {
    label: re.compile(r"(?<![^\W\d_])" + pattern + r"(?![^\W\d_])", re.IGNORECASE)
    for label, pattern in FRAMING_TERMS.items()
}

#: Minstrel eye-dialect spellings with no apostrophe, which
#: ``difficulty_scorer.dialect_marker_count`` does not count. Lowercase only:
#: capitalised "Ah" is mostly the interjection.
MINSTREL_SPELLINGS = ("mah", "gwine", "dat", "dem", "dis", "dey", "heah", "fum", "sah",
                      "dere", "wif", "nuffin", "massa", "lawdy")
_MINSTREL_RE = re.compile(r"(?<![\w'’])(?:" + "|".join(MINSTREL_SPELLINGS) + r")(?![\w])")

#: A chapter's dialect is a hotspot at this many markers per 1,000 words, or
#: three times the book's median chapter, whichever is higher.
_DIALECT_FLOOR_PER_1K = 5.0
_DIALECT_MEDIAN_FACTOR = 3.0
_MAX_CHAPTERS = 8
_LINES_PER_CHAPTER = 3
_LINE_CHARS = 200


def _dialect_markers(text: str) -> int:
    from src.difficulty_scorer import dialect_marker_count

    return dialect_marker_count(text) + len(_MINSTREL_RE.findall(text))


def _framing(text: str) -> dict[str, int]:
    return {label: n for label, rx in _FRAMING_RE.items() if (n := len(rx.findall(text)))}


def _clip(paragraph: str, hit: int) -> str:
    """At most ~200 characters of ``paragraph``, centred on offset ``hit``."""
    line = " ".join(paragraph.split())
    if len(line) <= _LINE_CHARS:
        return line
    lo = max(0, min(hit - _LINE_CHARS // 3, len(line) - _LINE_CHARS))
    return ("…" if lo else "") + line[lo:lo + _LINE_CHARS].strip() + ("…" if lo + _LINE_CHARS < len(line) else "")


def _sample_lines(text: str) -> list[str]:
    """Up to three paragraphs: framing terms first, then the densest dialect."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    framed, dialect = [], []
    for p in paragraphs:
        flat = " ".join(p.split())
        hit = next((m for rx in _FRAMING_RE.values() if (m := rx.search(flat))), None)
        if hit:
            framed.append(_clip(flat, hit.start()))
        elif (n := _dialect_markers(flat)) >= 2:
            dialect.append((n, _clip(flat, 0)))
    dialect.sort(key=lambda x: -x[0])
    return (framed + [line for _, line in dialect])[:_LINES_PER_CHAPTER]


def speech_hotspots(project_dir: Path) -> list[dict[str, Any]]:
    """``[{chapter_id, framing, dialect_markers, dialect_per_1k, lines}]``, framing first."""
    from src.harness.address_sample import _chapter_ids_in_order
    from src.utils.source_text import load_chapter_source_text

    project_dir = Path(project_dir)
    rows = []
    for chapter_id in _chapter_ids_in_order(project_dir):
        text, _mtime, _kind = load_chapter_source_text(project_dir, chapter_id)
        words = len(text.split())
        if not words:
            continue
        markers = _dialect_markers(text)
        rows.append({
            "chapter_id": chapter_id,
            "framing": _framing(text),
            "dialect_markers": markers,
            "dialect_per_1k": round(1000 * markers / words, 1),
            "_text": text,
        })
    if not rows:
        return []
    threshold = max(_DIALECT_FLOOR_PER_1K,
                    _DIALECT_MEDIAN_FACTOR * median(r["dialect_per_1k"] for r in rows))
    hot = [r for r in rows if r["framing"] or r["dialect_per_1k"] >= threshold]
    hot.sort(key=lambda r: (-sum(r["framing"].values()), -r["dialect_per_1k"]))
    out = []
    for r in hot[:_MAX_CHAPTERS]:
        r["lines"] = _sample_lines(r.pop("_text"))
        out.append(r)
    return out


def format_hotspots(rows: list[dict[str, Any]]) -> str:
    """The prompt block for ``rows``, or ``""`` when there are none."""
    if not rows:
        return ""
    parts = [
        "BOOK-WIDE SPEECH AND FRAMING HOTSPOTS\n"
        "The source sample above is the opening of the book. These chapters, from the whole "
        "book, carry period ethnic or racial framing or much more eye dialect than the book's "
        "typical chapter. Each needs a rule unless the house rules or the series guides "
        "already give one: name the speaker group and ask how to render it."
    ]
    for r in rows:
        signals = [f"{label} ×{n}" for label, n in r["framing"].items()]
        signals.append(f"dialect {r['dialect_per_1k']}/1k words")
        lines = "\n".join(f'  "{line}"' for line in r["lines"])
        parts.append(f"- {r['chapter_id']}: {', '.join(signals)}" + (f"\n{lines}" if lines else ""))
    return "\n".join(parts)


#: Words that show a guide (or a house rule) says something about framing.
_FRAMING_RULE_RE = re.compile(r"\b(?:racial|racialized|racialised|ethnic|race|slurs?)\b", re.IGNORECASE)


def framing_rule_warning(project_dir: Path, guide: str) -> str:
    """A ``REVIEW:`` line when the book has period framing and nothing gives a rule for it.

    The trim to the word target once cut a series' general framing rule down to
    one porter, with two farmers in minstrel dialect still ahead in chapter 9.
    """
    framed = [r for r in speech_hotspots(project_dir) if r["framing"]]
    if not framed or _FRAMING_RULE_RE.search(guide or ""):
        return ""
    from src.house.prompt import load_translator_house

    house = load_translator_house(project_dir)
    if any(_FRAMING_RULE_RE.search(str(r.get("rule") or "")) for r in (*house.global_rules, *house.triggered)):
        return ""
    where = "; ".join(
        f"{r['chapter_id']} ({', '.join(f'{t} ×{n}' for t, n in r['framing'].items())})" for r in framed
    )
    return (
        f"REVIEW: the book carries period ethnic/racial framing ({where}) and neither the "
        "guide nor a house rule says how to render it (no mention of racial/ethnic framing "
        "or slurs). Add a rule, or confirm with the user that none is needed."
    )
