"""
English Bible references: parse them, find them in prose, write them in Spanish.

Ported from ``projects/short-sermons-to-little-children/helper_scripts/add_nbla.py``
(``BOOK_NUMS`` / ``parse_ref``) and widened to the forms that book's source
actually writes: abbreviations with and without a period ("2 Pet. 3:8",
"Ps 95:3", "Prov. 8:17"), comma lists that change chapter
("Job 26:6, 38:17", "Rev. 3:5, 13:8, 20:12,15"), semicolon lists, and ranges
with a hyphen, en dash or em dash ("Rom. 3:10–12").

A :class:`Ref` keeps the text it was parsed from. The Spanish form
(:meth:`Ref.spanish`) swaps the book name for the standard abbreviation in
``bible_book_abbreviations_es.json`` (no trailing period: ``Jn 10:5``) and keeps
the author's numbers as written, so "Ezek. 36:22, 26–27" becomes
"Ez 36:22, 26–27" rather than a re-derived list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

ABBREVIATIONS_FILE = Path(__file__).with_name("bible_book_abbreviations_es.json")

#: (number, English name, Spanish name), in canonical order. The Spanish names
#: are the keys of ``bible_book_abbreviations_es.json``.
BOOKS: tuple[tuple[int, str, str], ...] = (
    (1, "Genesis", "Génesis"), (2, "Exodus", "Éxodo"), (3, "Leviticus", "Levítico"),
    (4, "Numbers", "Números"), (5, "Deuteronomy", "Deuteronomio"), (6, "Joshua", "Josué"),
    (7, "Judges", "Jueces"), (8, "Ruth", "Rut"), (9, "1 Samuel", "1 Samuel"),
    (10, "2 Samuel", "2 Samuel"), (11, "1 Kings", "1 Reyes"), (12, "2 Kings", "2 Reyes"),
    (13, "1 Chronicles", "1 Crónicas"), (14, "2 Chronicles", "2 Crónicas"),
    (15, "Ezra", "Esdras"), (16, "Nehemiah", "Nehemías"), (17, "Esther", "Ester"),
    (18, "Job", "Job"), (19, "Psalms", "Salmos"), (20, "Proverbs", "Proverbios"),
    (21, "Ecclesiastes", "Eclesiastés"), (22, "Song of Solomon", "Cantares"),
    (23, "Isaiah", "Isaías"), (24, "Jeremiah", "Jeremías"), (25, "Lamentations", "Lamentaciones"),
    (26, "Ezekiel", "Ezequiel"), (27, "Daniel", "Daniel"), (28, "Hosea", "Oseas"),
    (29, "Joel", "Joel"), (30, "Amos", "Amós"), (31, "Obadiah", "Abdías"),
    (32, "Jonah", "Jonás"), (33, "Micah", "Miqueas"), (34, "Nahum", "Nahúm"),
    (35, "Habakkuk", "Habacuc"), (36, "Zephaniah", "Sofonías"), (37, "Haggai", "Hageo"),
    (38, "Zechariah", "Zacarías"), (39, "Malachi", "Malaquías"),
    (40, "Matthew", "Mateo"), (41, "Mark", "Marcos"), (42, "Luke", "Lucas"),
    (43, "John", "Juan"), (44, "Acts", "Hechos"), (45, "Romans", "Romanos"),
    (46, "1 Corinthians", "1 Corintios"), (47, "2 Corinthians", "2 Corintios"),
    (48, "Galatians", "Gálatas"), (49, "Ephesians", "Efesios"), (50, "Philippians", "Filipenses"),
    (51, "Colossians", "Colosenses"), (52, "1 Thessalonians", "1 Tesalonicenses"),
    (53, "2 Thessalonians", "2 Tesalonicenses"), (54, "1 Timothy", "1 Timoteo"),
    (55, "2 Timothy", "2 Timoteo"), (56, "Titus", "Tito"), (57, "Philemon", "Filemón"),
    (58, "Hebrews", "Hebreos"), (59, "James", "Santiago"), (60, "1 Peter", "1 Pedro"),
    (61, "2 Peter", "2 Pedro"), (62, "1 John", "1 Juan"), (63, "2 John", "2 Juan"),
    (64, "3 John", "3 Juan"), (65, "Jude", "Judas"), (66, "Revelation", "Apocalipsis"),
)

#: Single-chapter books, where "Jude 3" means verse 3.
SINGLE_CHAPTER = frozenset({31, 57, 63, 64, 65})

# Short forms per unnumbered book. Matched case-insensitively with an optional
# trailing period. Two-letter forms that are common English words ("Is", "Am",
# "So") are left out on purpose: in running prose they would match far more
# often than they cite anything.
_ALIASES: dict[int, tuple[str, ...]] = {
    1: ("gen", "ge", "gn"), 2: ("exod", "exo", "ex"), 3: ("lev", "lv"),
    4: ("num", "nu", "nm"), 5: ("deut", "deu", "dt"), 6: ("josh", "jos"),
    7: ("judg", "jdg"), 8: ("rth",), 15: ("ezr",), 16: ("neh",), 17: ("esth", "est"),
    19: ("psalm", "ps", "psa", "pss"), 20: ("prov", "pro", "prv", "pr"),
    21: ("eccl", "ecc", "eccles", "qoh"), 22: ("song", "song of songs", "canticles", "cant"),
    23: ("isa",), 24: ("jer",), 25: ("lam",), 26: ("ezek", "eze", "ezk"),
    27: ("dan", "dn"), 28: ("hos",), 29: ("jl",), 30: ("amo",), 31: ("obad", "ob"),
    32: ("jon", "jnh"), 33: ("mic",), 34: ("nah",), 35: ("hab",), 36: ("zeph", "zep"),
    37: ("hag",), 38: ("zech", "zec"), 39: ("mal",),
    40: ("matt", "mat", "mt"), 41: ("mk", "mrk"), 42: ("lk", "luk"), 43: ("jn", "joh"),
    44: ("act",), 45: ("rom", "ro"), 48: ("gal",), 49: ("eph",), 50: ("phil", "php"),
    51: ("col",), 56: ("tit",), 57: ("philem", "phlm", "phm"), 58: ("heb",),
    59: ("jas", "jam", "jm"), 65: ("jud", "jde"), 66: ("rev", "apoc", "re"),
}

# Numbered books share a stem; "1 Cor.", "I Corinthians", "1st John".
_NUMBERED_STEMS: dict[str, tuple[str, ...]] = {
    "Samuel": ("sam", "sa", "sm"),
    "Kings": ("kgs", "kin", "ki", "kg"),
    "Chronicles": ("chron", "chr", "ch"),
    "Corinthians": ("cor", "co"),
    "Thessalonians": ("thess", "thes", "th"),
    "Timothy": ("tim", "ti"),
    "Peter": ("pet", "pt", "pe"),
    "John": ("jn", "jno", "joh"),
}

_ORDINALS = {"1": ("1", "i", "1st", "first"), "2": ("2", "ii", "2nd", "second"),
             "3": ("3", "iii", "3rd", "third")}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().rstrip(".").lower())


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, int]:
    table: dict[str, int] = {}
    for num, en, _es in BOOKS:
        table[_norm(en)] = num
        match = re.match(r"^([123]) (.+)$", en)
        if match:
            digit, stem = match.groups()
            forms = (stem.lower(),) + _NUMBERED_STEMS.get(stem, ())
            for ordinal in _ORDINALS[digit]:
                for form in forms:
                    table[f"{ordinal} {form}"] = num
        for alias in _ALIASES.get(num, ()):
            table[alias] = num
    table["psalms"] = 19
    table["song of solomon"] = 22
    table["revelations"] = 66
    return table


def book_number(name: str) -> Optional[int]:
    """The canonical number for an English book name or abbreviation, else ``None``."""
    key = _norm(name)
    key = re.sub(r"^([123])(?=[a-z])", r"\1 ", key)  # "1John" -> "1 john"
    return _alias_table().get(key)


@lru_cache(maxsize=1)
def spanish_abbreviations() -> dict[str, str]:
    return json.loads(ABBREVIATIONS_FILE.read_text(encoding="utf-8"))


def spanish_name(book: int) -> str:
    return BOOKS[book - 1][2]


def english_name(book: int) -> str:
    return BOOKS[book - 1][1]


def spanish_abbreviation(book: int) -> str:
    return spanish_abbreviations()[spanish_name(book)]


# ── parsing ──────────────────────────────────────────────────────────────────

DASHES = "-‐‑‒–—"
_DASH = f"[{DASHES}]"
_ROMAN = r"[ivxlc]+"

#: The numbers after a book name: "3:16", "3:16-18", "26:6, 38:17",
#: "20:12,15", "1:1; 2:3", "xxiii. 1". Group ``nums``.
_NUMS = (
    rf"(?P<nums>(?:\d+|{_ROMAN})\s*[:.]\s*\d+(?:\s*{_DASH}\s*\d+(?:\s*[:.]\s*\d+)?)?"
    rf"(?:\s*[,;]\s*(?:\d+\s*[:.]\s*)?\d+(?:\s*{_DASH}\s*\d+(?:\s*[:.]\s*\d+)?)?(?![\d:]))*)"
)


@lru_cache(maxsize=1)
def _book_pattern() -> str:
    names = sorted(_alias_table(), key=len, reverse=True)
    alts = []
    for name in names:
        parts = [re.escape(p) for p in name.split(" ")]
        alts.append(r"\s+".join(parts))
    return "(?:" + "|".join(alts) + ")"


@lru_cache(maxsize=1)
def _find_re() -> re.Pattern[str]:
    # Capitalised book name (an abbreviation in prose always is), optional
    # period, then chapter and verse. Case-insensitive alternation would let
    # "see 3:16" match "Se"; the capital is checked in ``find_refs``.
    return re.compile(
        rf"(?<![\w])(?P<book>{_book_pattern()})\.?\s*{_NUMS}",
        re.IGNORECASE,
    )


@dataclass
class Ref:
    """One parsed reference: a book and the verses it names, in order."""

    book: int
    text: str                       # as written, e.g. "Ezek. 36:22, 26–27"
    book_text: str                  # e.g. "Ezek."
    nums: str                       # e.g. "36:22, 26–27"
    verses: list[tuple[int, int]] = field(default_factory=list)
    whole_chapters: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        """A stable label independent of how the reference was written.

        ``"43 10:5"``, ``"45 3:10,3:11,3:12"``; ``"19 23"`` for a whole chapter.
        """
        if self.whole_chapters and not self.verses:
            return f"{self.book} " + ",".join(str(c) for c in self.whole_chapters)
        return f"{self.book} " + ",".join(f"{c}:{v}" for c, v in self.verses)

    @property
    def english(self) -> str:
        """A canonical English form, "John 10:5", for prompts and the board."""
        if self.whole_chapters and not self.verses:
            return f"{english_name(self.book)} " + ",".join(str(c) for c in self.whole_chapters)
        return f"{english_name(self.book)} {self.nums_canonical()}"

    def nums_canonical(self) -> str:
        """The verses as "10:5", "3:10-12" or "26:6; 38:17", ranges collapsed."""
        groups: list[tuple[int, list[list[int]]]] = []
        for chapter, verse in self.verses:
            if groups and groups[-1][0] == chapter:
                runs = groups[-1][1]
                if verse == runs[-1][-1] + 1:
                    runs[-1].append(verse)
                else:
                    runs.append([verse])
            else:
                groups.append((chapter, [[verse]]))
        parts = []
        for chapter, runs in groups:
            spans = ",".join(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs)
            parts.append(f"{chapter}:{spans}")
        return "; ".join(parts)

    def verse_keys(self) -> list[str]:
        """Index keys ``"BB:CCC:VVV"`` for every named verse, in order."""
        return [verse_key(self.book, c, v) for c, v in self.verses]

    def spanish(self, range_sep: Optional[str] = None) -> str:
        """``Jn 10:5``: the Spanish abbreviation and the numbers as written.

        ``range_sep`` replaces every dash in a range (the book's own style);
        ``None`` keeps the author's.
        """
        nums = re.sub(r"\s*:\s*", ":", self.nums.strip())
        if re.match(rf"^{_ROMAN}\s*\.", nums, re.IGNORECASE):
            chapter, _, rest = nums.partition(".")
            nums = f"{_roman_to_int(chapter.strip())}:{rest.strip()}"
        if range_sep:
            nums = re.sub(rf"\s*{_DASH}\s*", range_sep, nums)
        return f"{spanish_abbreviation(self.book)} {nums}"


def verse_key(book: int, chapter: int, verse: int) -> str:
    return f"{book:02d}:{chapter:03d}:{verse:03d}"


def _roman_to_int(text: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
    total, prev = 0, 0
    for ch in reversed(text.lower()):
        val = values[ch]
        total = total - val if val < prev else total + val
        prev = max(prev, val)
    return total


def _chapter_number(token: str) -> int:
    token = token.strip()
    return int(token) if token.isdigit() else _roman_to_int(token)


def _parse_nums(book: int, nums: str) -> tuple[list[tuple[int, int]], list[int]]:
    """``(verses, whole_chapters)`` for the number tail of a reference."""
    verses: list[tuple[int, int]] = []
    chapters: list[int] = []
    chapter: Optional[int] = None
    for part in re.split(r"\s*[,;]\s*", nums.strip()):
        if not part:
            continue
        match = re.match(
            rf"^(?:(?P<ch>\d+|{_ROMAN})\s*[:.]\s*)?(?P<v1>\d+)"
            rf"(?:\s*{_DASH}\s*(?:(?P<ch2>\d+)\s*[:.]\s*)?(?P<v2>\d+))?$",
            part,
            re.IGNORECASE,
        )
        if not match:
            # Bare chapter ("Psalm 23") or chapter range ("Psalm 1-2").
            bare = re.match(rf"^(\d+)(?:\s*{_DASH}\s*(\d+))?$", part)
            if bare and chapter is None:
                lo = int(bare.group(1))
                hi = int(bare.group(2) or lo)
                chapters.extend(range(lo, hi + 1))
                continue
            raise ValueError(f"cannot read {part!r}")
        if match.group("ch"):
            chapter = _chapter_number(match.group("ch"))
        elif chapter is None:
            if book in SINGLE_CHAPTER:
                chapter = 1
            else:
                # A lone number with no chapter yet is a whole chapter.
                chapters.append(int(match.group("v1")))
                if match.group("v2"):
                    chapters.extend(range(int(match.group("v1")) + 1, int(match.group("v2")) + 1))
                continue
        v1 = int(match.group("v1"))
        if match.group("v2"):
            v2 = int(match.group("v2"))
            if match.group("ch2"):
                # Cross-chapter range "3:20-4:2": the end verse is unknown to
                # the parser, so take v1..(end of chapter) lazily as v1 only and
                # the second chapter from 1..v2.
                verses.append((chapter, v1))
                chapter = int(match.group("ch2"))
                verses.extend((chapter, v) for v in range(1, v2 + 1))
                continue
            if v2 < v1:
                raise ValueError(f"backwards range in {part!r}")
            verses.extend((chapter, v) for v in range(v1, v2 + 1))
        else:
            verses.append((chapter, v1))
    return verses, chapters


def parse_ref(text: str) -> Optional[Ref]:
    """Parse one reference ("John 10:5", "2 Pet. 3:8", "Psalm 23"), else ``None``.

    Lenient about what surrounds it: a leading "cf." / "see", parentheses and a
    trailing note are dropped. Only the first reference of a "/"- or
    "+"-joined list is read; :func:`find_refs` is the tool for prose.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    raw = re.sub(r"^\(?\s*(?:cf\.|see|compare|paraphrase(?: of)?)\s+", "", raw, flags=re.IGNORECASE)
    raw = re.split(r"\s*[/+]\s*", raw)[0]
    raw = raw.strip("() ")
    raw = re.sub(r"\s*\(.*$", "", raw).strip()
    match = re.match(
        rf"^(?P<book>(?:[123]|i{{1,3}}|first|second|third)?\s*[A-Za-z][A-Za-z ]*?)\.?\s*"
        # A roman chapter needs a space before it, or "Rev." reads as "Re" + "v.".
        rf"(?P<nums>(?:\d+|(?<=\s){_ROMAN}\.\s*\d).*)$",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return None
    book_text = match.group("book").strip()
    book = book_number(book_text)
    if book is None:
        return None
    nums = match.group("nums").strip().rstrip(".")
    try:
        verses, chapters = _parse_nums(book, nums)
    except ValueError:
        return None
    if not verses and not chapters:
        return None
    period = "." if raw[len(match.group("book")) :].lstrip().startswith(".") else ""
    return Ref(
        book=book,
        text=raw,
        book_text=book_text + period,
        nums=nums,
        verses=verses,
        whole_chapters=chapters,
    )


@dataclass
class FoundRef:
    """A reference located in prose: where it sits and what it says."""

    start: int
    end: int
    ref: Ref


def find_refs(text: str) -> list[FoundRef]:
    """Every written chapter-and-verse reference in ``text``, in order.

    Needs a verse ("John 10:5", "Ps. xxiii. 1"); a bare "Psalm 23" in prose is
    too often just a mention to count as a citation. The book name must start
    with a capital letter or a digit.
    """
    out: list[FoundRef] = []
    for match in _find_re().finditer(text or ""):
        book_text = match.group("book")
        first_alpha = next((c for c in book_text if c.isalpha()), "")
        if not first_alpha or (not book_text[0].isdigit() and not first_alpha.isupper()):
            continue
        # A roman ordinal must be upper case ("II Cor."), never "i cor".
        book = book_number(book_text)
        if book is None:
            continue
        nums = match.group("nums").rstrip()
        try:
            verses, chapters = _parse_nums(book, nums)
        except ValueError:
            continue
        if not verses:
            continue
        written = text[match.start() : match.start("nums") + len(nums)]
        between = text[match.end("book") : match.start("nums")]
        out.append(
            FoundRef(
                start=match.start(),
                end=match.start("nums") + len(nums),
                ref=Ref(
                    book=book,
                    text=written,
                    book_text=book_text + ("." if between.lstrip().startswith(".") else ""),
                    nums=nums,
                    verses=verses,
                    whole_chapters=chapters,
                ),
            )
        )
    return out


@lru_cache(maxsize=1)
def _es_find_re() -> re.Pattern[str]:
    """Spanish (and leftover English) book names followed by chapter:verse."""
    names: set[str] = set()
    for num, en, es in BOOKS:
        names.add(es)
        names.add(spanish_abbreviations()[es])
        names.add(en)
    for alias in _alias_table():
        if len(alias) > 2 or alias[0].isdigit():
            names.add(alias)
    alts = sorted(names, key=len, reverse=True)
    pattern = "|".join(r"\s+".join(re.escape(p) for p in n.split(" ")) for n in alts)
    return re.compile(
        rf"(?<![\w])(?P<book>{pattern})\.?\s*{_NUMS}", re.IGNORECASE
    )


def _es_book_number(name: str) -> Optional[int]:
    key = _norm(name)
    for num, _en, es in BOOKS:
        if key in (_norm(es), _norm(spanish_abbreviations()[es])):
            return num
    return book_number(name)


def find_es_refs(text: str) -> list[FoundRef]:
    """Written references in Spanish prose ("Jn 10:5", "Juan 10:5", "Heb. 11:6")."""
    out: list[FoundRef] = []
    for match in _es_find_re().finditer(text or ""):
        book_text = match.group("book")
        first_alpha = next((c for c in book_text if c.isalpha()), "")
        if not first_alpha or (not book_text[0].isdigit() and not first_alpha.isupper()):
            continue
        book = _es_book_number(book_text)
        if book is None:
            continue
        nums = match.group("nums").rstrip()
        try:
            verses, chapters = _parse_nums(book, nums)
        except ValueError:
            continue
        if not verses:
            continue
        between = text[match.end("book") : match.start("nums")]
        out.append(
            FoundRef(
                start=match.start(),
                end=match.start("nums") + len(nums),
                ref=Ref(
                    book=book,
                    text=text[match.start() : match.start("nums") + len(nums)],
                    book_text=book_text + ("." if between.lstrip().startswith(".") else ""),
                    nums=nums,
                    verses=verses,
                    whole_chapters=chapters,
                ),
            )
        )
    return out


def range_separator(text: str) -> Optional[str]:
    """The dash a book uses inside verse ranges ("21:15–17"), if it uses one."""
    match = re.search(rf"\d\s*[:.]\s*\d+\s*({_DASH})\s*\d", text or "")
    return match.group(1) if match else None
