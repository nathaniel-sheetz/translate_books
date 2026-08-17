"""
Book splitting functionality for automatic chapter detection.

This module provides utilities to detect chapter boundaries in full book files
and split them into individual chapter files. Patterns are defined in
split_patterns.json and can be extended without code changes.

Front matter (preface, foreword, etc.) and back matter (epilogue, appendix,
etc.) are first-class non-numbered sections. Detected sections are recorded
in a "chapter_manifest" written to project.json so the reader and EPUB
builder can render them with proper labels.
"""

import json
import re
import statistics
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.utils.text_utils import is_caption_block


SectionKind = Literal["front_matter", "chapter", "back_matter"]


class DetectedChapter(BaseModel):
    """
    Represents a detected chapter or front/back matter section.

    Example:
        DetectedChapter(
            position_index=1,
            kind="chapter",
            number=1,
            chapter_title="Chapter I",
            content="It was the best of times...",
            start_line=1,
            end_line=145,
        )
    """
    model_config = ConfigDict(populate_by_name=True)

    position_index: int = Field(
        ge=1, description="Sequential position in reading order (1-based)"
    )
    chapter_title: str = Field(description="Heading text as it appears in the book")
    content: str = Field(min_length=1, description="Section text content")
    start_line: int = Field(ge=0, description="Starting line number in source file")
    end_line: int = Field(ge=0, description="Ending line number in source file")
    kind: SectionKind = Field(
        default="chapter",
        description="Kind of section: front_matter, chapter, or back_matter",
    )
    label: str = Field(
        default="",
        description="Display label for non-chapter sections (e.g. 'Preface').",
    )
    number: Optional[int] = Field(
        default=None,
        description="Display chapter number (only set when kind='chapter').",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_chapter_number(cls, data):
        """Accept legacy 'chapter_number' kwarg as 'position_index'."""
        if isinstance(data, dict) and "chapter_number" in data and "position_index" not in data:
            data = dict(data)
            data["position_index"] = data.pop("chapter_number")
        return data

    @property
    def chapter_number(self) -> int:
        """Backward-compatible alias for position_index."""
        return self.position_index


# Roman numeral conversion tables
ROMAN_NUMERALS = {
    'I': 1, 'IV': 4, 'V': 5, 'IX': 9,
    'X': 10, 'XL': 40, 'L': 50, 'XC': 90,
    'C': 100, 'CD': 400, 'D': 500, 'CM': 900,
    'M': 1000
}


_PATTERNS_CACHE = None

_RE_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}

# A line that contains nothing but a single [IMAGE:images/...] placeholder.
# Used to detect chapter-header decoration images that sit immediately
# above an all-caps chapter heading and would otherwise be glued to the
# end of the previous section by the splitter.
_HEADER_IMAGE_LINE_RE = re.compile(r'\[IMAGE:images/[^\]]+\]')


def _find_preceding_header_image(text: str, pos: int) -> Optional[tuple[int, str]]:
    """
    If a single ``[IMAGE:images/...]`` line lies immediately before ``pos``
    (separated from it only by blank lines), return ``(line_start, image_text)``.

    ``line_start`` is the offset of the first character of the image line —
    suitable for use as the new section boundary so the previous section's
    ``end_pos`` can be shrunk to it. The image is *not* required to be
    preceded by anything in particular (start-of-file is fine), but the
    image line must be the only content on its line.

    Returns ``None`` if no such image exists, so callers can use the
    plain heading position unchanged.
    """
    # Walk back over the blank gap between `pos` and whatever precedes it.
    i = pos
    while i > 0 and text[i - 1] in ' \t\r\n':
        i -= 1
    if i == pos or i == 0:
        return None

    # A [CAPTION] block belonging to the ornament may sit between the image and
    # the heading. Step back over it so the ornament is still found; otherwise
    # the image and its caption stay glued to the end of the previous section.
    if text[i - 1] != ']':
        cap_start = text.rfind('\n', 0, i) + 1
        if not is_caption_block(text[cap_start:i]):
            return None
        i = cap_start
        while i > 0 and text[i - 1] in ' \t\r\n':
            i -= 1
        if i == 0 or text[i - 1] != ']':
            return None

    line_start = text.rfind('\n', 0, i) + 1  # 0 if no preceding newline
    candidate = text[line_start:i]
    if not _HEADER_IMAGE_LINE_RE.fullmatch(candidate):
        return None
    return (line_start, candidate)


def _looks_like_subtitle(candidate: str) -> bool:
    """
    True if a stripped line reads like a short, standalone chapter subtitle
    rather than prose or an image placeholder.

    A subtitle is non-empty, not itself an ``[IMAGE:...]`` line, short in both
    characters (<=100) and words (<=12), and contains at least one letter.
    """
    return bool(
        candidate
        and not _HEADER_IMAGE_LINE_RE.fullmatch(candidate)
        and len(candidate) <= 100
        and len(candidate.split()) <= 12
        and re.search(r'[A-Za-z]', candidate)
    )


def _take_standalone_subtitle(
    lines: List[str], i: int,
) -> Optional[Tuple[str, str]]:
    """
    Treat ``lines[i]`` as a subtitle candidate. If it looks like a subtitle
    and stands alone (the next non-blank content is a blank-line break, not
    more prose on the very next line), return ``(title, cleaned_body)`` with
    that one line removed. Otherwise return ``None``.
    """
    candidate = lines[i].strip()
    if not _looks_like_subtitle(candidate):
        return None
    # Title must be a standalone line; a following non-blank line means prose.
    if i + 1 < len(lines) and lines[i + 1].strip():
        return None
    cleaned_body = '\n'.join(lines[:i] + lines[i + 1:]).strip()
    return (candidate, cleaned_body)


def _extract_header_image_title(body: str) -> Optional[Tuple[str, str]]:
    """
    If ``body`` begins with a standalone ``[IMAGE:...]`` line followed by a
    short title on its own line, return ``(title, cleaned_body)`` with the
    title line removed. The image and remaining body are preserved.

    Returns ``None`` when the pattern does not match (e.g. no image, or the
    line after the image is a paragraph rather than a standalone title).
    """
    if not body:
        return None

    lines = body.split('\n')
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None

    if not _HEADER_IMAGE_LINE_RE.fullmatch(lines[i].strip()):
        return None
    i += 1

    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None

    return _take_standalone_subtitle(lines, i)


def _extract_leading_subtitle(body: str) -> Optional[Tuple[str, str]]:
    """
    If ``body`` begins with a short standalone title line (no image), return
    ``(title, cleaned_body)`` with the title removed.
    """
    if not body:
        return None

    lines = body.split('\n')
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None

    return _take_standalone_subtitle(lines, i)


def _extract_chapter_subtitle(
    body: str,
    header_image: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Find a chapter subtitle after the heading line.

    Handles both layouts:
    - Image before heading in source (``header_image`` set; title follows heading)
    - Image between heading and title in the body slice
    - Title immediately after heading with no image
    """
    if header_image:
        effective = f"{header_image}\n\n{body}"
        result = _extract_header_image_title(effective)
        if result:
            title, cleaned = result
            if cleaned.startswith(header_image):
                cleaned = cleaned[len(header_image):].lstrip('\n')
            return (title, cleaned)

    result = _extract_header_image_title(body)
    if result:
        return result
    return _extract_leading_subtitle(body)


def load_split_patterns() -> dict:
    """Load split patterns from split_patterns.json. Caches after first load."""
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is not None:
        return _PATTERNS_CACHE

    patterns_file = Path(__file__).parent / "split_patterns.json"
    if not patterns_file.exists():
        raise FileNotFoundError(f"Split patterns file not found: {patterns_file}")

    with open(patterns_file, "r", encoding="utf-8") as f:
        _PATTERNS_CACHE = json.load(f)
    return _PATTERNS_CACHE


def get_pattern_names() -> list[str]:
    """Return list of available pattern names (excluding 'custom')."""
    data = load_split_patterns()
    return list(data["patterns"].keys())


def get_pattern_definitions() -> dict:
    """Return pattern definitions for API/UI consumption."""
    data = load_split_patterns()
    result = {}
    for name, defn in data["patterns"].items():
        result[name] = {
            "label": defn["label"],
            "numbering": defn["numbering"],
        }
    return result


def roman_to_int(roman: str) -> Optional[int]:
    """
    Convert Roman numeral to integer.

    Args:
        roman: Roman numeral string (e.g., 'I', 'IV', 'XII', 'C')

    Returns:
        Integer value, or None if invalid Roman numeral

    Example:
        >>> roman_to_int('IV')
        4
        >>> roman_to_int('XLII')
        42
        >>> roman_to_int('C')
        100
    """
    roman = roman.upper().strip()

    if not roman:
        return None

    # Validate characters
    if not all(c in 'IVXLCDM' for c in roman):
        return None

    result = 0
    i = 0

    while i < len(roman):
        # Check for two-character numerals first
        if i + 1 < len(roman):
            two_char = roman[i:i+2]
            if two_char in ROMAN_NUMERALS:
                result += ROMAN_NUMERALS[two_char]
                i += 2
                continue

        # Single character numeral
        one_char = roman[i]
        if one_char in ROMAN_NUMERALS:
            result += ROMAN_NUMERALS[one_char]
            i += 1
        else:
            return None  # Invalid character

    return result


def int_to_roman(num: int) -> str:
    """
    Convert integer to Roman numeral.

    Args:
        num: Integer to convert (1-3999)

    Returns:
        Roman numeral string

    Example:
        >>> int_to_roman(4)
        'IV'
        >>> int_to_roman(42)
        'XLII'
        >>> int_to_roman(100)
        'C'
    """
    if num < 1 or num > 3999:
        raise ValueError("Number must be between 1 and 3999")

    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syms = [
        'M', 'CM', 'D', 'CD',
        'C', 'XC', 'L', 'XL',
        'X', 'IX', 'V', 'IV',
        'I'
    ]

    result = ''
    for i, v in enumerate(val):
        count = num // v
        if count:
            result += syms[i] * count
            num -= v * count

    return result


def get_chapter_pattern(
    pattern_type: str = "roman",
    custom_regex: Optional[str] = None,
    *,
    case_sensitive: bool = False,
) -> re.Pattern:
    """
    Get compiled regex pattern for chapter detection.

    Patterns are loaded from split_patterns.json. The special type "custom"
    accepts an arbitrary user-provided regex.

    Args:
        pattern_type: Named pattern from split_patterns.json, or "custom"
        custom_regex: Custom regex pattern (required if pattern_type is "custom")
        case_sensitive: Drop the forced re.IGNORECASE on a "custom" regex.
            Ignored for named patterns, which carry their own flags.

    Returns:
        Compiled regex pattern that matches chapter headers

    Raises:
        ValueError: If pattern_type is invalid or custom_regex missing for "custom" type
    """
    if pattern_type == "custom":
        if not custom_regex:
            raise ValueError("custom_regex is required when pattern_type is 'custom'")
        # IGNORECASE has always been forced here, and silently changes what a
        # character class means: under it `[A-Z][A-Z ...]` matches lowercase
        # too, degrading an all-caps heading matcher into "any run of letters
        # and spaces" -- i.e. almost every English paragraph. Callers that want
        # the literal reading pass case_sensitive=True.
        flags = re.MULTILINE if case_sensitive else re.IGNORECASE | re.MULTILINE
        try:
            return re.compile(custom_regex, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")

    data = load_split_patterns()
    patterns = data.get("patterns", {})

    if pattern_type not in patterns:
        available = ", ".join(list(patterns.keys()) + ["custom"])
        raise ValueError(f"Invalid pattern_type: {pattern_type}. Available: {available}")

    defn = patterns[pattern_type]
    flags = 0
    for flag_name in defn.get("flags", []):
        flags |= _RE_FLAGS.get(flag_name, 0)

    try:
        return re.compile(defn["regex"], flags)
    except re.error as e:
        raise ValueError(f"Invalid regex in pattern '{pattern_type}': {e}")


def detect_pattern_from_text(book_text: str) -> Optional[str]:
    """Detect the best-fit chapter pattern for raw book text.

    The URL ingest path derives a ``suggested_pattern`` from the HTML headings;
    this is the local ``source.txt`` analog. Each candidate pattern's *real*
    splitting regex is run over the text in ``detection_order`` priority, and
    the first pattern that matches confidently wins. Note ``detection_order``
    deliberately omits the plain ``roman`` / ``numeric`` patterns: the titled
    variants (``chapter_roman_titled`` / ``chapter_numeric_titled``) make their
    title optional, so they subsume the plain ones and always match first — the
    plain patterns stay selectable only as explicit user choices. The specific
    ``chapter …`` patterns need >= 2 hits; the greedy fallbacks that declare
    ``detect_min_ratio`` (``allcaps_heading``, ``bare_roman``) need a higher
    floor so they don't win on stray all-caps or lone-numeral lines. Returns
    ``None`` when nothing matches confidently, so the caller can fall back to a
    default.
    """
    if not book_text or not book_text.strip():
        return None

    data = load_split_patterns()
    patterns = data.get("patterns", {})
    detection_order = data.get("detection_order", list(patterns.keys()))

    for name in detection_order:
        defn = patterns.get(name)
        if not defn:
            continue
        try:
            compiled = get_chapter_pattern(name)
        except ValueError:
            continue
        hits = len(compiled.findall(book_text))
        # Greedy patterns (those carrying detect_min_ratio) are prone to false
        # positives on raw text, so demand a higher floor; they sit last in
        # detection_order and are only reached when the specific patterns miss.
        floor = 3 if defn.get("detect_min_ratio") is not None else 2
        if hits >= floor:
            return name
    return None


# ---------------------------------------------------------------------------
# Heading-outline splitting
#
# The HTML importer already knows where every chapter starts -- it sees the
# <h1>..<h6> tags -- and writes that outline to headings.json next to
# source.txt. Anchoring the split on it replaces regex archaeology over
# flattened prose with the structure the markup declared. When there is no
# sidecar, or the outline fails the confidence gate below, every caller falls
# back to the regex patterns unchanged.
# ---------------------------------------------------------------------------

HEADING_OUTLINE_FILENAME = "headings.json"

# Tuned on the 20-book local corpus. See the module tests for the cases each
# threshold is holding down.
_HEADING_MIN_SECTIONS = 5       # fewer than this is a book without an outline
_HEADING_MIN_MEDIAN = 400       # chars; below this the level is title fragments
_HEADING_TINY_SPAN = 400        # chars; a section this short is a stub
_HEADING_MAX_TINY_FRACTION = 0.34
_HEADING_MERGE_SPAN = 200       # chars of prose under a bare numeral heading
_HEADING_SKEW_ADVISORY = 4.0    # max/median above this earns a warning, not a veto

# "Chapter I." / "II" / "Part 3:" -- a heading that numbers a chapter without
# naming it. When one of these has no prose under it, it is a super-title for
# the heading that follows, not a section of its own.
_BARE_NUMERAL_HEADING_RE = re.compile(
    r"^(chapter|book|part|story)?\s*[IVXLCDM\d]+\s*[.:]?\s*$", re.IGNORECASE
)


class _HeadingMatch:
    """A regex-match lookalike for a located heading.

    ``split_book_into_chapters`` consumes ``re.Match`` objects (``.start()``,
    ``.end()``, ``.group()``, ``.lastindex``). Wrapping heading anchors in the
    same shape lets the heading path reuse the entire downstream pipeline --
    front/back-matter tagging, boilerplate stripping, ``min_chapter_size``,
    header-image pull-back, the manifest -- instead of forking it.
    """

    __slots__ = ("_start", "_end", "_text", "leads")

    def __init__(self, start: int, end: int, text: str, leads=None):
        self._start = start
        self._end = end
        self._text = text
        # Character ranges between the headings of a merged group, which belong
        # to the body (see _merge_bare_numeral_anchors). Not part of the re.Match
        # protocol -- only the headings path reads it.
        self.leads = leads or []

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end

    def group(self, n: int = 0) -> str:
        if n in (0, 1):
            return self._text
        raise IndexError("no such group")

    @property
    def lastindex(self) -> int:
        return 1


def load_heading_outline(
    project_dir, *, collect_error: Optional[List[str]] = None
) -> Optional[List[dict]]:
    """
    Read ``headings.json`` from a project directory.

    Returns the ordered ``[{level, text}, ...]`` list, or ``None`` when the
    sidecar is absent or unreadable — which is the signal for every caller to
    use the regex patterns instead. Projects ingested before the sidecar
    existed take this path and behave exactly as they did before.

    ``None`` alone cannot distinguish "no sidecar" (normal, silent) from "the
    sidecar is there but broken" (a truncated or hand-mangled write, which
    should be said out loud — otherwise it reads as a pre-sidecar project and
    the regex fallback looks intentional). Pass ``collect_error`` to receive a
    human-readable reason in the broken case; callers that asked for the
    outline by name (``--chapter-pattern headings``) should fail on it, and
    ``auto`` callers should surface it as a warning.
    """
    path = Path(project_dir) / HEADING_OUTLINE_FILENAME
    if not path.exists():
        return None

    def _broken(detail: str) -> None:
        if collect_error is not None:
            collect_error.append(
                f"{HEADING_OUTLINE_FILENAME} exists but could not be used — "
                f"{detail} — so the heading-outline split is unavailable until "
                f"this project is re-ingested from the source URL."
            )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _broken(f"invalid JSON: {exc}")
        return None
    except (OSError, UnicodeDecodeError) as exc:
        _broken(f"unreadable: {exc}")
        return None
    headings = data.get("headings") if isinstance(data, dict) else None
    if not isinstance(headings, list):
        _broken("no 'headings' list at the top level")
        return None
    out = []
    for h in headings:
        if not isinstance(h, dict):
            continue
        text = (h.get("text") or "").strip()
        if not text:
            continue
        try:
            level = int(h.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        out.append({"level": level, "text": text})
    if not out:
        _broken(f"{len(headings)} heading entries present, none with usable text")
        return None
    return out


def locate_headings(
    book_text: str, outline: List[dict]
) -> Tuple[List[dict], List[str]]:
    """
    Find each outline heading as a standalone line in ``book_text``.

    Returns ``(anchors, unlocated)`` where each anchor is
    ``{level, text, start, end}`` with ``start`` at the first character of the
    heading line and ``end`` just past it.

    The scan is sequential — each heading is searched for at or after the
    previous match — so a title that appears twice (a story that is also an
    illustration caption, say) anchors on the right occurrence. The importer
    emits every heading as ``\\n\\n{text}\\n\\n`` and post-processing only
    collapses runs of blank lines, so the standalone-line invariant holds.
    A heading that cannot be found (``source.txt`` was hand-edited, most
    likely) is reported rather than silently shifting every boundary after it.
    """
    anchors: List[dict] = []
    unlocated: List[str] = []
    cursor = 0
    for h in outline:
        text = h["text"]
        found = _find_standalone_line(book_text, text, cursor)
        if found is None:
            unlocated.append(text)
            continue
        start, end = found
        anchors.append({"level": h.get("level", 0), "text": text,
                        "start": start, "end": end})
        cursor = end
    return anchors, unlocated


def _find_standalone_line(
    text: str, target: str, from_pos: int
) -> Optional[Tuple[int, int]]:
    """Locate ``target`` as a whole line at or after ``from_pos``."""
    pos = text.find(target, from_pos)
    while pos != -1:
        line_start = text.rfind("\n", 0, pos) + 1
        line_end = text.find("\n", pos)
        if line_end == -1:
            line_end = len(text)
        if text[line_start:line_end].strip() == target:
            return (line_start, line_end)
        pos = text.find(target, pos + 1)

    # Fall back to a whitespace-insensitive line scan: the sidecar collapses
    # runs of whitespace, and a hand-edited source may not have.
    needle = re.sub(r"\s+", " ", target).strip()
    scan = from_pos
    while scan < len(text):
        line_end = text.find("\n", scan)
        if line_end == -1:
            line_end = len(text)
        if re.sub(r"\s+", " ", text[scan:line_end]).strip() == needle:
            return (scan, line_end)
        scan = line_end + 1
    return None


def _merge_bare_numeral_anchors(
    anchors: List[dict], total: int
) -> List[dict]:
    """
    Collapse ``Chapter I.`` + ``THE HORSE AND HIS RIDER.`` sibling headings
    into one section.

    Returns one entry per section: ``{start, end, span, title}`` where ``start``
    opens the first heading line of the group, ``end`` closes the last, and
    ``span`` runs to the next section.

    Some books number a chapter in one heading and title it in the next. The
    merge is deliberately narrow — the leading heading must be a bare numeral
    *and* have essentially no prose under it — because a span-only rule also
    eats short standalone pieces (a book of one-page poems collapses from 52
    correct sections to 37).

    ``leads`` carries the character ranges *between* the group's headings, one
    per gap. The gap is measured start-to-start, so it includes the heading line
    itself and up to ~190 characters of prose can sit inside a group that still
    merges. Since ``end`` closes the last heading, the body starts after it and
    anything in those gaps — an epigraph under ``Chapter I.`` before the title
    heading — is in neither the title nor the body, and so was written nowhere
    and reported nowhere. The caller prepends ``leads`` to the body instead.
    Ranges are per-gap rather than one span across the group so the intervening
    heading lines are excluded rather than folded in as prose.
    """
    bounds = [a["start"] for a in anchors] + [total]
    out: List[dict] = []
    i = 0
    while i < len(anchors):
        j = i
        while (
            j + 1 < len(anchors)
            and (bounds[j + 1] - bounds[j]) < _HEADING_MERGE_SPAN
            and _BARE_NUMERAL_HEADING_RE.match(anchors[j]["text"])
        ):
            j += 1
        out.append({
            "start": anchors[i]["start"],
            "end": anchors[j]["end"],
            "span": bounds[j + 1] - anchors[i]["start"],
            "title": "\n".join(a["text"] for a in anchors[i:j + 1]),
            "leads": [(anchors[k]["end"], anchors[k + 1]["start"])
                      for k in range(i, j)],
        })
        i = j + 1
    return out


def select_heading_level(
    anchors: List[dict], book_text: str
) -> dict:
    """
    Decide which h-level holds the book's chapters.

    Scores each level by how well its headings partition the text and returns
    ``{"selected": <level|None>, "reason": str, "levels": {...}}``. ``selected``
    is ``None`` when no level is convincing — a book with almost no markup
    structure, or a landing page — and the caller then uses the regex patterns.

    A level qualifies when it has at least ``_HEADING_MIN_SECTIONS`` sections,
    a median section of at least ``_HEADING_MIN_MEDIAN`` chars, and at most
    ``_HEADING_MAX_TINY_FRACTION`` of its sections are stubs. Among qualifying
    levels the densest wins — the deepest level that still describes chapters.

    Section-length *skew* is deliberately not a gate: an anthology's stories
    legitimately vary 13x, and vetoing on that rejects exactly the books this
    path helps most. It comes back as an advisory instead.
    """
    drop_patterns = _compile_matter_patterns("drop_matter_patterns")
    total = len(book_text)
    levels: dict = {}

    for level in sorted({a["level"] for a in anchors}):
        at_level = [
            a for a in anchors
            if a["level"] == level
            and _matches_builtin_pattern(a["text"], drop_patterns) is None
        ]
        if len(at_level) < 2:
            continue
        sections = _merge_bare_numeral_anchors(at_level, total)
        spans = [s["span"] for s in sections]
        median = statistics.median(spans)
        tiny = sum(1 for s in spans if s < _HEADING_TINY_SPAN)
        levels[f"h{level}"] = {
            "n": len(sections),
            "median_chars": int(median),
            "tiny": tiny,
            "skew": round(max(spans) / median, 1) if median else None,
        }

    # Densest qualifying level wins. On a tie the deeper level wins, because a
    # book that numbers chapters at h2 and titles them at h3 should split on the
    # more specific one.
    qualifying = [
        (stats["n"], int(name[1:]), name)
        for name, stats in levels.items()
        if stats["n"] >= _HEADING_MIN_SECTIONS
        and stats["median_chars"] >= _HEADING_MIN_MEDIAN
        and stats["tiny"] / stats["n"] <= _HEADING_MAX_TINY_FRACTION
    ]
    if not qualifying:
        return {
            "selected": None,
            "reason": "no heading level partitions the text convincingly",
            "levels": levels,
        }

    *_, selected = max(qualifying)
    s = levels[selected]
    return {
        "selected": selected,
        "reason": f"n={s['n']} median={s['median_chars']} tiny={s['tiny']}",
        "levels": levels,
    }


def _normalize_heading_level(level) -> Optional[int]:
    """Accept ``2``, ``"2"``, or ``"h2"`` as a heading level."""
    if level is None:
        return None
    if isinstance(level, int):
        return level
    s = str(level).strip().lower().lstrip("h")
    try:
        return int(s)
    except ValueError:
        return None


def _heading_matches(
    book_text: str, anchors: List[dict], level: int
) -> List[_HeadingMatch]:
    """Build regex-match lookalikes for one heading level."""
    at_level = [a for a in anchors if a["level"] == level]
    if not at_level:
        return []
    return [
        _HeadingMatch(sec["start"], sec["end"], sec["title"], sec.get("leads"))
        for sec in _merge_bare_numeral_anchors(at_level, len(book_text))
    ]


def resolve_pattern_type(
    requested: Optional[str],
    book_text: str,
    *,
    outline_report: Optional[dict] = None,
    heading_level=None,
) -> str:
    """Resolve an ``auto`` request to the pattern that will actually run.

    Prefers the document's own heading outline when it is convincing, else
    detects the best-fit regex pattern from the text (the local-source analog
    of the URL path's ``suggested_pattern``), else falls back to ``roman`` so
    behavior stays defined. A concrete request passes through untouched.

    An explicit ``heading_level`` also selects the outline path, even when
    ``select_heading_level`` found no level convincing. That is precisely the
    case the flag exists for: the caller read the ``levels`` table, saw a level
    the confidence gates rejected, and asked for it by name. Without this the
    flag was discarded here and the split ran on a regex pattern instead —
    reporting ``levels.h2.n=4``, accepting ``--heading-level h2``, and then
    failing with "No chapters detected with pattern type 'roman'".

    ``heading_level`` needs a located outline to act on, so it only redirects
    when ``outline_report`` is present (i.e. the project has a headings.json).
    With no sidecar the regex patterns still run and
    :func:`split_sanity_warnings` says the flag had nothing to bite on.

    Shared by the splitter and by the harness's reporting so ``pattern_used``
    can never disagree with what the split did — which is why the reporting
    call sites have to pass ``heading_level`` too.
    """
    if requested not in (None, "auto"):
        return requested
    if outline_report and outline_report.get("selected"):
        return "headings"
    if heading_level is not None and outline_report:
        return "headings"
    return detect_pattern_from_text(book_text) or "roman"


def shape_outline_report(
    outline_report: Optional[dict], pattern_used: str
) -> Optional[dict]:
    """Shape the heading-outline report for output.

    ``applied`` says whether the split actually anchored on the outline. It
    matters because the report is still computed when the caller forces a regex
    pattern on a book that has a sidecar — without the flag, ``selected: "h2"``
    reads as "this is how the book was split" when it isn't. The level table is
    kept either way; it is exactly what you need to decide whether to switch.
    """
    if not outline_report:
        return None
    return {**outline_report, "applied": pattern_used == "headings"}


def split_ledger(
    outline_report: Optional[dict],
    heading_outline: Optional[List[dict]],
    sections: list,
    dropped: list,
) -> dict:
    """Summarize what became of everything the splitter saw.

    Sections used to disappear between detection and the written files with no
    report line at all — a book's dedication, two title-page fragments — so
    "not written AND not in ``dropped``" was a blind spot nothing could surface.
    The real fix is that ``dropped`` now carries a reason for every section it
    filters (``too_short``, ``empty``, ``unparsable_number``, not just
    ``boilerplate``); this is the at-a-glance view over it.

    ``chapter_level_headings`` counts raw headings at the chosen level, so it
    can exceed ``sections`` legitimately: a numeral heading merged into the
    title that follows it consumes two. Treat a gap as a prompt to read
    ``dropped``, not as an error.
    """
    unlocated = (outline_report or {}).get("unlocated") or []
    # Only claim a chapter level when the outline is what the split ran on.
    selected = ((outline_report or {}).get("selected")
                if (outline_report or {}).get("applied") else None)
    at_level = None
    if selected and heading_outline:
        at_level = sum(
            1 for h in heading_outline if f"h{h.get('level')}" == selected
        )
    return {
        "outline_headings": len(heading_outline) if heading_outline else None,
        "chapter_level": selected,
        "chapter_level_headings": at_level,
        "sections": len(sections),
        "dropped": len(dropped),
        "unlocated": len(unlocated),
    }


def split_sanity_warnings(
    chapters: List["DetectedChapter"],
    book_text: str,
    *,
    pattern_used: str,
    detected: Optional[str] = None,
    outline_report: Optional[dict] = None,
    heading_level=None,
    outline_errors: Optional[List[str]] = None,
) -> List[str]:
    """Cheap post-split guardrail: flag results that look mis-split.

    Returns human-readable advisories (empty when the split looks fine) so the
    setup/split beats can surface a "this split looks wrong" signal instead of
    silently carrying a 1-chapter book all the way to EPUB.
    """
    warnings: List[str] = []
    chapter_sections = [c for c in chapters if c.kind == "chapter"]
    n = len(chapter_sections)
    size = len(book_text or "")

    # An unreadable headings.json used to be indistinguishable from no sidecar
    # at all: the loader returned None, the split quietly regexed, and
    # `heading_outline` came back null. Say so instead.
    warnings.extend(outline_errors or [])

    if heading_level is not None and not outline_report:
        warnings.append(
            f"--heading-level {heading_level} had no effect: this project has no "
            f"usable heading outline (headings.json) to anchor on, so the regex "
            f"patterns ran instead. Re-ingest from the source URL to capture the "
            f"outline, or drop the flag."
        )

    if outline_report:
        unlocated = outline_report.get("unlocated") or []
        if unlocated:
            shown = ", ".join(repr(t) for t in unlocated[:3])
            more = f" (+{len(unlocated) - 3} more)" if len(unlocated) > 3 else ""
            warnings.append(
                f"{len(unlocated)} heading(s) from headings.json were not found "
                f"in source.txt and were skipped: {shown}{more}. Was source.txt "
                f"hand-edited after ingest?"
            )
        selected = outline_report.get("selected")
        stats = (outline_report.get("levels") or {}).get(selected or "")
        if stats and (stats.get("skew") or 0) > _HEADING_SKEW_ADVISORY:
            warnings.append(
                f"Largest section is {stats['skew']}x the median — a heading may "
                f"have been missed, or the book's sections are just uneven. "
                f"Check the longest section, or try another --heading-level."
            )

    if pattern_used == "roman" and detected is None and size > 20_000:
        # auto found no confident pattern and fell back to 'roman'. This is the
        # more actionable message, so it wins over the generic under-split warning
        # below (they'd otherwise both fire for this one situation).
        warnings.append(
            "No chapter pattern matched the text confidently; fell back to "
            "'roman'. Try --chapter-pattern auto or a --custom-regex."
        )
    elif n <= 1 and size > 20_000:
        suffix = ""
        if detected and detected != pattern_used:
            suffix = f" — text detection suggests '{detected}'"
        warnings.append(
            f"Only {n} chapter detected for a {size:,}-char source; the "
            f"'{pattern_used}' pattern may be wrong{suffix}."
        )
    return warnings


# ---------------------------------------------------------------------------
# Front / back matter detection
# ---------------------------------------------------------------------------

_TITLE_PUNCT = ".:!?,;\u2014-"


def _normalize_title(line: str) -> str:
    """Lowercase a heading line, strip whitespace and trailing punctuation."""
    s = line.strip()
    s = s.strip(_TITLE_PUNCT).strip()
    return s.casefold()


def _matches_user_title(line: str, titles: List[str]) -> Optional[str]:
    """Return the original (user-supplied) title if the line matches one."""
    if not titles:
        return None
    norm_line = _normalize_title(line)
    for t in titles:
        if not t or not t.strip():
            continue
        if _normalize_title(t) == norm_line:
            return t.strip()
    return None


def _matches_builtin_pattern(line: str, compiled_patterns: List[re.Pattern]) -> Optional[str]:
    """Return the matched heading (canonical title-cased) if any built-in pattern matches."""
    s = line.strip().strip(_TITLE_PUNCT).strip()
    if not s:
        return None
    for pat in compiled_patterns:
        if pat.match(s):
            # Title-case for display purposes (Preface, Foreword, etc.)
            return s.title()
    return None


def _compile_matter_patterns(key: str) -> List[re.Pattern]:
    """Compile front_matter_patterns or back_matter_patterns from JSON."""
    data = load_split_patterns()
    raw = data.get(key, []) or []
    out = []
    for r in raw:
        try:
            out.append(re.compile(r, re.IGNORECASE))
        except re.error:
            continue
    return out


def _find_matter_sections(
    text: str,
    *,
    region_start: int,
    region_end: int,
    user_titles: List[str],
    builtin_patterns: List[re.Pattern],
    kind: SectionKind,
) -> List[dict]:
    """
    Scan a substring of `text` (region_start..region_end) for front- or
    back-matter headings. Returns ordered sections within that region.
    Each entry: {"start_pos", "end_pos", "label", "heading_line", "kind"}
    """
    if region_start >= region_end:
        return []

    matches = []  # list of (line_start_pos, heading_end_pos, label, raw_heading)

    pos = region_start
    while pos < region_end:
        line_end = text.find("\n", pos, region_end)
        if line_end == -1:
            line_end = region_end
        line = text[pos:line_end]

        label = _matches_user_title(line, user_titles)
        if label is None:
            label = _matches_builtin_pattern(line, builtin_patterns)

        if label is not None:
            matches.append((pos, line_end, label, line.strip()))

        pos = line_end + 1

    if not matches:
        return []

    sections = []
    for i, (start, heading_end, label, raw_heading) in enumerate(matches):
        if i + 1 < len(matches):
            section_end = matches[i + 1][0]
        else:
            section_end = region_end
        sections.append({
            "start_pos": start,
            "end_pos": section_end,
            "heading_end": heading_end,
            "label": label,
            "heading_line": raw_heading,
            "kind": kind,
        })
    return sections


def split_book_into_chapters(
    book_text: str,
    pattern_type: str = "roman",
    custom_regex: Optional[str] = None,
    min_chapter_size: int = 100,
    front_matter_titles: Optional[List[str]] = None,
    back_matter_titles: Optional[List[str]] = None,
    auto_detect_front_matter: bool = True,
    auto_detect_back_matter: bool = True,
    auto_strip_boilerplate: bool = True,
    collect_dropped: Optional[List[dict]] = None,
    heading_outline: Optional[List[dict]] = None,
    heading_level=None,
    case_sensitive_custom: bool = False,
    collect_outline_report: Optional[dict] = None,
    outline_error: Optional[str] = None,
) -> List[DetectedChapter]:
    """
    Split a full book text into individual chapters and front/back matter.

    Detects chapter boundaries using the specified pattern. Additionally
    scans for front matter (preface, foreword, etc.) before the first
    chapter and back matter (epilogue, appendix, etc.) after the last.

    Args:
        book_text: Full text of the book to split
        pattern_type: Type of chapter pattern. Any named pattern from
            split_patterns.json ("roman", "numeric", "chapter_roman_titled",
            "chapter_numeric_titled", "allcaps_heading", "bare_roman"),
            "custom" (with custom_regex), "headings" (split on heading_outline
            at heading_level), or "auto"/None to detect the best fit — preferring
            the outline when it is convincing, else the regex patterns
            (see detect_pattern_from_text).
        custom_regex: Custom regex pattern (required if pattern_type is "custom")
        min_chapter_size: Minimum characters for valid chapter (filters false matches)
        front_matter_titles: Literal heading strings the user has declared as
            front matter for this book (e.g. ["To the Teacher"]). Always
            match, regardless of the built-in keyword list.
        back_matter_titles: Literal heading strings declared as back matter.
        auto_detect_front_matter: If True (default), also match the built-in
            keyword list (preface, foreword, prologue, ...).
        auto_detect_back_matter: If True (default), also match the built-in
            back-matter keyword list (epilogue, afterword, appendix, ...).
        auto_strip_boilerplate: If True (default), drop navigation/boilerplate
            sections (Contents, Title Page, List of Illustrations, Copyright,
            ...) entirely so they are never written, numbered, or translated —
            even when a chapter pattern matched them or the user declared them
            as front matter. Set False to keep a section whose heading happens
            to be one of those words.
        collect_dropped: Optional list the caller supplies to receive a record
            of each section that was detected but not written ({"label":
            <heading>, "reason": ...}) for transparency. Reasons are
            "boilerplate" (navigation stripped by drop_matter_patterns),
            "too_short" (below min_chapter_size), "empty", and
            "unparsable_number" (a roman/numeric pattern matched a heading whose
            numeral won't parse). Left untouched when None.
        heading_outline: The document's own ``[{level, text}, ...]`` heading
            outline, as written to headings.json by the HTML importer (see
            load_heading_outline). Enables pattern_type "headings", and lets
            "auto" prefer it over the regex patterns when it is convincing.
        heading_level: Which h-level holds chapters ("h2", 2, ...). Defaults to
            whatever select_heading_level picks.
        case_sensitive_custom: Compile a "custom" regex without re.IGNORECASE.
            Default False preserves the long-standing behavior.
        collect_outline_report: Optional dict the caller supplies to receive the
            heading-outline report ({selected, reason, levels, unlocated}) even
            when the split ends up on the regex path. Left untouched when None.
        outline_error: Why headings.json could not be used, from
            load_heading_outline's ``collect_error``. Folded into the "no
            chapters detected" message so a broken sidecar isn't diagnosed as a
            wrong regex pattern.

    Returns:
        List of DetectedChapter objects in reading order. position_index is
        1..N, kind is one of front_matter / chapter / back_matter, and
        ``number`` is set sequentially (starting at 1) only for chapters.

    Raises:
        ValueError: If no sections detected at all.
    """
    if not book_text or not book_text.strip():
        raise ValueError("Book text cannot be empty")

    front_matter_titles = list(front_matter_titles or [])
    back_matter_titles = list(back_matter_titles or [])

    # Anchor on the document's own heading outline when we have one. This is
    # ground truth from the markup, so it sidesteps every way flattened prose
    # can fool a regex: image captions shaped like titles, hand-typeset
    # multi-line headings, a book that mixes "CHAPTER 1" with "CHAPTER II".
    anchors: List[dict] = []
    unlocated: List[str] = []
    outline_report: Optional[dict] = None
    if heading_outline:
        anchors, unlocated = locate_headings(book_text, heading_outline)
        outline_report = select_heading_level(anchors, book_text)
        outline_report["unlocated"] = unlocated
        if collect_outline_report is not None:
            collect_outline_report.update(outline_report)

    if pattern_type in (None, "auto"):
        pattern_type = resolve_pattern_type(
            pattern_type, book_text, outline_report=outline_report,
            heading_level=heading_level)

    if pattern_type == "headings":
        if not anchors:
            if heading_outline:
                raise ValueError(
                    "Chapter pattern 'headings' loaded a heading outline, but "
                    "none of its titles were found in source.txt. Restore "
                    "source.txt to match the outline, or re-ingest from the "
                    "source URL."
                )
            raise ValueError(
                "Chapter pattern 'headings' needs a heading outline "
                "(headings.json), but none was found for this project. "
                "Re-ingest from the source URL, or pick a regex pattern."
            )
        if heading_level is not None:
            level = _normalize_heading_level(heading_level)
            if level is None or not (1 <= level <= 6):
                raise ValueError(
                    f"invalid heading level: {heading_level!r} (expected h1..h6)"
                )
        else:
            level = None
        if level is None:
            selected = (outline_report or {}).get("selected")
            level = _normalize_heading_level(selected)
        elif outline_report is not None:
            # An explicit level overrides the selector, so the report has to say
            # so — otherwise the output names one level while the split used
            # another, and the level table becomes untrustworthy.
            outline_report["selected"] = f"h{level}"
            outline_report["reason"] = "explicitly requested via heading_level"
            if collect_outline_report is not None:
                collect_outline_report.update(outline_report)
        if level is None:
            raise ValueError(
                "No heading level holds this book's chapters. Pass "
                "--heading-level explicitly, or pick a regex pattern."
            )
        matches = _heading_matches(book_text, anchors, level)
        if not matches:
            available = sorted({f"h{a['level']}" for a in anchors})
            raise ValueError(
                f"No headings at level h{level}. Available: {', '.join(available)}"
            )
        numbering = "sequential"
    else:
        # Get chapter detection pattern
        pattern = get_chapter_pattern(
            pattern_type, custom_regex, case_sensitive=case_sensitive_custom
        )

        # Find all chapter headers
        matches = list(pattern.finditer(book_text))
        numbering = None  # resolved from the pattern definition below

    # User-supplied front/back-matter titles take precedence over the chapter
    # regex. Without this, a generic pattern like "allcaps_heading" would
    # claim a heading such as "TO THE CHILDREN" as the first chapter and the
    # downstream front-matter scan (which only looks BEFORE the first chapter)
    # would never see the user's declared title. Drop any chapter match whose
    # heading line normalizes to a user-declared front- or back-matter title;
    # _find_matter_sections will then re-tag those lines with the correct
    # kind on its second pass.
    # The headings path tags matter in place instead (see kind_overrides below),
    # so it must keep these matches as boundaries rather than dropping them.
    user_matter_titles = [*front_matter_titles, *back_matter_titles]
    if user_matter_titles and matches and pattern_type != "headings":
        matches = [
            m for m in matches
            if _matches_user_title(m.group(0), user_matter_titles) is None
        ]

    # Compile matter patterns
    front_patterns = _compile_matter_patterns("front_matter_patterns") if auto_detect_front_matter else []
    back_patterns = _compile_matter_patterns("back_matter_patterns") if auto_detect_back_matter else []
    drop_patterns = _compile_matter_patterns("drop_matter_patterns") if auto_strip_boilerplate else []

    # On the headings path every heading stays a section boundary and is tagged
    # in place instead. The regex paths find matter by *position* — everything
    # before the first chapter is front matter, everything after the last is
    # back matter — which cannot express matter interleaved with chapters. The
    # outline routinely is: a half-title sits between a book's dedication and
    # its prologue, so a positional scan stops at the half-title and the
    # prologue silently becomes part of its body.
    #
    # Tagging by heading also means "Foreword" no longer has to be declared by
    # hand just because the markup put it at the chapter level (which is what
    # fused Bambi's foreword into Chapter I). Regex patterns keep their existing
    # behavior; only this path is affected.
    kind_overrides: dict[int, Tuple[SectionKind, str]] = {}
    if pattern_type == "headings":
        for m in matches:
            heading = m.group(0)
            user_label = _matches_user_title(heading, front_matter_titles)
            if user_label is not None:
                kind_overrides[m.start()] = ("front_matter", user_label)
                continue
            user_label = _matches_user_title(heading, back_matter_titles)
            if user_label is not None:
                kind_overrides[m.start()] = ("back_matter", user_label)
                continue
            label = _matches_builtin_pattern(heading, front_patterns)
            if label is not None:
                kind_overrides[m.start()] = ("front_matter", label)
                continue
            label = _matches_builtin_pattern(heading, back_patterns)
            if label is not None:
                kind_overrides[m.start()] = ("back_matter", label)

    # Determine numbering strategy from pattern definition ("headings" already
    # set its own above).
    if numbering is None:
        if pattern_type == "custom":
            numbering = "sequential"
        else:
            data = load_split_patterns()
            defn = data["patterns"].get(pattern_type, {})
            numbering = defn.get("numbering", "sequential")

    # ------------------------------------------------------------------
    # Build the list of sections (front matter + chapters + back matter)
    # ------------------------------------------------------------------

    if matches:
        # If the first chapter heading has a preceding [IMAGE:...] line,
        # the front-matter region must end at the image (not the heading)
        # so the image isn't claimed by the last front-matter section.
        first_header_image = _find_preceding_header_image(book_text, matches[0].start())
        first_chapter_start = first_header_image[0] if first_header_image else matches[0].start()
        last_chapter_end = len(book_text)  # back matter is anything after last chapter heading
    else:
        first_chapter_start = len(book_text)
        last_chapter_end = len(book_text)

    # Detect drop_matter_patterns (Contents, Title Page, ...) alongside real
    # front matter so standalone boilerplate headings that sit before the first
    # chapter become sections _add_section can record + strip. Without this they
    # were discarded silently by position and never reached collect_dropped, so
    # `setup` reported `dropped: []` despite the strip (friction log #28).
    front_sections = _find_matter_sections(
        book_text,
        region_start=0,
        region_end=first_chapter_start,
        user_titles=front_matter_titles,
        builtin_patterns=front_patterns + drop_patterns,
        kind="front_matter",
    )

    # Build raw chapter sections (start, end, identifier, raw_heading)
    chapter_sections = []
    for i, m in enumerate(matches):
        chapter_identifier = m.group(1) if m.lastindex else m.group(0)
        # Optional second capture group holds an inline subtitle (e.g.
        # "CHAPTER I EARLY BOYHOOD" -> subtitle = "EARLY BOYHOOD").
        chapter_subtitle: Optional[str] = None
        if m.lastindex and m.lastindex >= 2:
            sub = m.group(2)
            if sub:
                chapter_subtitle = sub.strip() or None
        start_pos = m.end()
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(book_text)

        # If a single [IMAGE:images/...] line sits immediately before this
        # heading, treat it as a chapter-header decoration and pull the
        # section boundary back so the image belongs to *this* chapter
        # instead of the preceding one.
        header_image = _find_preceding_header_image(book_text, m.start())
        section_start = header_image[0] if header_image else m.start()

        chapter_sections.append({
            "match_start": section_start,
            "start_pos": start_pos,
            "end_pos": end_pos,
            "identifier": chapter_identifier,
            "subtitle": chapter_subtitle,
            "raw_heading": m.group(0).strip(),
            "header_image": header_image[1] if header_image else None,
            "kind_override": kind_overrides.get(m.start()),
            # Prose between the headings of a merged bare-numeral group. Empty
            # for every regex pattern and for unmerged headings.
            "lead_text": "\n\n".join(
                t for s, e in getattr(m, "leads", ())
                if (t := book_text[s:e].strip())
            ),
        })

    # Sections may now overlap with their predecessor (we pulled match_start
    # back). Shrink each section's end_pos to its successor's match_start so
    # the chapter-header image isn't double-counted.
    for i in range(len(chapter_sections) - 1):
        chapter_sections[i]["end_pos"] = chapter_sections[i + 1]["match_start"]

    # Identify back matter inside the LAST chapter's body. Back matter is the
    # first matter heading found after the last chapter heading.
    back_sections: List[dict] = []
    if chapter_sections:
        last = chapter_sections[-1]
        candidate_back = _find_matter_sections(
            book_text,
            region_start=last["start_pos"],
            region_end=len(book_text),
            user_titles=back_matter_titles,
            builtin_patterns=back_patterns + drop_patterns,
            kind="back_matter",
        )
        if candidate_back:
            # Trim the last chapter to end where back matter begins.
            last["end_pos"] = candidate_back[0]["start_pos"]
            back_sections = candidate_back
    else:
        # No chapters detected: scan the entire text for back matter too.
        back_sections = _find_matter_sections(
            book_text,
            region_start=0,
            region_end=len(book_text),
            user_titles=back_matter_titles,
            builtin_patterns=back_patterns + drop_patterns,
            kind="back_matter",
        )

    if not matches and not front_sections and not back_sections:
        # When a broken sidecar is why we are on the regex path at all, say so
        # here. Otherwise the run dies on "no chapters with pattern 'roman'"
        # and the caller never sees the warning that names the real cause.
        because = f" {outline_error}" if outline_error else ""
        raise ValueError(
            f"No chapters detected with pattern type '{pattern_type}'. "
            f"Check that your book uses the expected chapter format.{because}"
        )

    # ------------------------------------------------------------------
    # Materialize DetectedChapter list in reading order.
    # ------------------------------------------------------------------

    detected: List[DetectedChapter] = []
    chapter_seq = 0  # display number for kind=="chapter"

    def _add_section(*, section_start: int, content_start: int, content_end: int,
                     kind: SectionKind, raw_heading: str, label: str,
                     number: Optional[int],
                     header_image: Optional[str] = None,
                     body_override: Optional[str] = None,
                     lead_text: Optional[str] = None) -> None:
        nonlocal detected
        # Drop navigation/boilerplate (Contents, Title Page, ...) outright so it
        # is never written, numbered, or translated — whether it arrived as a
        # matched "chapter", an auto-detected matter heading, or a user-declared
        # title. Match against the section's first heading line and its label.
        if drop_patterns:
            heading_line = (raw_heading or label or "").splitlines()[0:1]
            drop_hit = (
                _matches_builtin_pattern(heading_line[0], drop_patterns)
                if heading_line else None
            )
            if drop_hit is None and label:
                drop_hit = _matches_builtin_pattern(label, drop_patterns)
            if drop_hit is not None:
                if collect_dropped is not None:
                    collect_dropped.append({"label": drop_hit, "reason": "boilerplate"})
                return
        if body_override is not None:
            content = body_override.strip()
        else:
            content = book_text[content_start:content_end].strip()
        # An epigraph that sat between 'Chapter I.' and the title heading it was
        # merged with opens the chapter body, where it appears in the book.
        if lead_text:
            content = f"{lead_text}\n\n{content}" if content else lead_text
        # Prepend the chapter-header image (if any) so it appears at the
        # start of the chapter body in the saved file, where every
        # downstream consumer (chunker, translator, EPUB builder) already
        # knows how to handle [IMAGE:...] placeholders.
        if header_image:
            content = f"{header_image}\n\n{content}" if content else header_image
        # Sections filtered from here on used to vanish with no trace: not
        # written, not reported, invisible to anyone reading the split output.
        # That is how a book's dedication disappeared between the chapter count
        # and the `dropped` list. Record them instead.
        first_line = ((raw_heading or label or "").splitlines() or [""])[0]
        if len(content) < min_chapter_size and kind == "chapter":
            if collect_dropped is not None:
                collect_dropped.append({
                    "label": first_line,
                    "reason": "too_short",
                    "chars": len(content),
                })
            return  # filter false-positive chapters by size
        if not content:
            if collect_dropped is not None:
                collect_dropped.append({"label": first_line, "reason": "empty"})
            return
        start_line = book_text[:section_start].count("\n")
        end_line = book_text[:content_end].count("\n")
        position = len(detected) + 1

        if kind == "chapter":
            title = raw_heading or (f"Chapter {number}" if number else "")
        else:
            title = raw_heading or label

        detected.append(DetectedChapter(
            position_index=position,
            chapter_title=title,
            content=content,
            start_line=start_line,
            end_line=end_line,
            kind=kind,
            label=label,
            number=number,
        ))

    # Front matter
    for fs in front_sections:
        _add_section(
            section_start=fs["start_pos"],
            content_start=fs["heading_end"] + 1,
            content_end=fs["end_pos"],
            kind="front_matter",
            raw_heading=fs["heading_line"],
            label=fs["label"],
            number=None,
        )

    # Chapters
    for cs in chapter_sections:
        ident = cs["identifier"]
        subtitle = cs.get("subtitle")
        if numbering == "roman":
            num = roman_to_int(ident)
            if num is None:
                # Defensive: unreachable through the shipped patterns, whose
                # capture groups are [IVXLCDM]+ / \d+ and so always parse. Kept
                # so a future pattern can't reintroduce a silent `continue`.
                if collect_dropped is not None:
                    collect_dropped.append({
                        "label": cs["raw_heading"], "reason": "unparsable_number",
                    })
                continue
            heading = f"Chapter {ident.upper()}"
            if subtitle:
                heading = f"{heading}\n{subtitle}"
        elif numbering == "numeric":
            try:
                num = int(ident)
            except (TypeError, ValueError):
                if collect_dropped is not None:
                    collect_dropped.append({
                        "label": cs["raw_heading"], "reason": "unparsable_number",
                    })
                continue
            heading = f"Chapter {num}"
        else:  # sequential
            num = chapter_seq + 1
            heading = cs["raw_heading"]

        body_override: Optional[str] = None
        if numbering in ("roman", "numeric") and not subtitle:
            extracted = _extract_chapter_subtitle(
                book_text[cs["start_pos"]:cs["end_pos"]],
                header_image=cs.get("header_image"),
            )
            if extracted:
                subtitle, body_override = extracted
                heading = f"{heading}\n{subtitle}"

        # A heading tagged as front/back matter keeps its position in reading
        # order but is emitted with that kind and left unnumbered, so the
        # chapter sequence skips it (headings path only; see kind_overrides).
        override = cs.get("kind_override")
        if override is not None:
            override_kind, override_label = override
            _add_section(
                section_start=cs["match_start"],
                content_start=cs["start_pos"],
                content_end=cs["end_pos"],
                kind=override_kind,
                raw_heading=cs["raw_heading"],
                label=override_label,
                number=None,
                header_image=cs.get("header_image"),
                lead_text=cs.get("lead_text"),
            )
            continue

        # Try to add; only bump display sequence if the section was actually added
        before = len(detected)
        chapter_seq += 1
        _add_section(
            section_start=cs["match_start"],
            content_start=cs["start_pos"],
            content_end=cs["end_pos"],
            kind="chapter",
            raw_heading=heading,
            label="",
            number=chapter_seq,
            header_image=cs.get("header_image"),
            body_override=body_override,
            lead_text=cs.get("lead_text"),
        )
        if len(detected) == before:
            # Section was filtered (too short); roll back the display counter.
            chapter_seq -= 1

    # Back matter
    for bs in back_sections:
        _add_section(
            section_start=bs["start_pos"],
            content_start=bs["heading_end"] + 1,
            content_end=bs["end_pos"],
            kind="back_matter",
            raw_heading=bs["heading_line"],
            label=bs["label"],
            number=None,
        )

    if not detected:
        raise ValueError(
            "No valid chapters found. Chapters may be too short or pattern may not match."
        )

    return detected


def validate_chapter_sequence(chapters: List[DetectedChapter]) -> tuple[bool, List[str]]:
    """
    Validate that detected chapters form a proper sequence.

    Checks numbering of kind=='chapter' entries; front/back matter are
    informational and do not affect validation.

    Returns:
        Tuple of (is_valid, list_of_warnings)
    """
    if not chapters:
        return (False, ["No chapters provided"])

    warnings = []

    chapter_only = [c for c in chapters if c.kind == "chapter"]
    if not chapter_only:
        return (False, ["No numbered chapters detected (only front/back matter)"])

    # Sort by display number
    sorted_chapters = sorted(chapter_only, key=lambda c: c.number or 0)

    # Check for duplicates
    chapter_nums = [c.number for c in sorted_chapters]
    duplicates = [num for num in set(chapter_nums) if chapter_nums.count(num) > 1]

    if duplicates:
        for num in duplicates:
            warnings.append(f"Duplicate chapter number: {num}")

    # Check for gaps in sequence
    expected_next = 1
    for chapter in sorted_chapters:
        if chapter.number is None:
            continue
        if chapter.number > expected_next:
            missing = list(range(expected_next, chapter.number))
            warnings.append(f"Gap in sequence: Missing chapter(s) {missing}")
        expected_next = chapter.number + 1

    # Check if first chapter is 1
    if sorted_chapters[0].number != 1:
        warnings.append(
            f"First chapter is {sorted_chapters[0].number}, not 1. "
            f"Book may have prologue or preface."
        )

    # Check for very short chapters (potential false positives)
    for chapter in sorted_chapters:
        if len(chapter.content) < 500:
            warnings.append(
                f"Chapter {chapter.number} is very short ({len(chapter.content)} chars). "
                f"May be a false positive."
            )

    is_valid = len(warnings) == 0
    return (is_valid, warnings)


def build_chapter_manifest(
    chapters: List[DetectedChapter],
    *,
    filename_prefix: str = "chapter",
) -> List[dict]:
    """
    Build a serializable chapter_manifest from a list of detected sections.

    Each entry has the shape:
        {"id": "chapter_03", "kind": "chapter", "number": 1}
        {"id": "chapter_01", "kind": "front_matter", "label": "Preface"}
    """
    out = []
    for ch in chapters:
        chapter_id = f"{filename_prefix}_{ch.position_index:02d}"
        entry = {"id": chapter_id, "kind": ch.kind}
        if ch.kind == "chapter":
            if ch.number is not None:
                entry["number"] = ch.number
        else:
            if ch.label:
                entry["label"] = ch.label
        out.append(entry)
    return out


def _write_chapter_manifest(project_dir: Path, manifest: List[dict]) -> None:
    """Merge a chapter_manifest into project.json without clobbering other keys."""
    project_json = project_dir / "project.json"
    data: dict = {}
    if project_json.exists():
        try:
            data = json.loads(project_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    data["chapter_manifest"] = manifest
    project_json.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def save_chapters_to_files(
    chapters: List[DetectedChapter],
    output_dir: str,
    filename_prefix: str = "chapter",
    filename_suffix: str = ".txt",
    *,
    write_manifest: bool = True,
    clear_existing: bool = True,
) -> List[str]:
    """
    Save detected chapters/sections to individual text files.

    Files are written in reading order using ``position_index`` regardless
    of section kind (front matter, chapter, back matter). When
    ``write_manifest`` is True (default), a ``chapter_manifest`` is also
    merged into the parent project's ``project.json`` so downstream tools
    can render proper labels.

    When ``clear_existing`` is True (default), unlink existing
    ``{prefix}_*{suffix}`` files in ``output_dir`` before writing so a
    smaller re-split does not leave orphaned higher-numbered files.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if clear_existing:
        for stale in output_path.glob(f"{filename_prefix}_*{filename_suffix}"):
            stale.unlink()

    created_files = []

    for chapter in chapters:
        # Filename uses position_index so files are always in reading order.
        filename = f"{filename_prefix}_{chapter.position_index:02d}{filename_suffix}"
        filepath = output_path / filename

        # Write chapter content with the original heading text on top.
        heading = chapter.chapter_title or chapter.label or ""
        if heading:
            filepath.write_text(f"{heading}\n\n{chapter.content}", encoding="utf-8")
        else:
            filepath.write_text(chapter.content, encoding="utf-8")

        created_files.append(str(filepath))

    if write_manifest:
        manifest = build_chapter_manifest(chapters, filename_prefix=filename_prefix)
        # Project root is the parent of the chapters/ directory.
        project_dir = output_path.parent
        try:
            _write_chapter_manifest(project_dir, manifest)
        except OSError:
            # Don't fail the write if manifest persistence fails.
            pass

    return created_files
