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
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    if i == pos or i == 0 or text[i - 1] != ']':
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


def get_chapter_pattern(pattern_type: str = "roman", custom_regex: Optional[str] = None) -> re.Pattern:
    """
    Get compiled regex pattern for chapter detection.

    Patterns are loaded from split_patterns.json. The special type "custom"
    accepts an arbitrary user-provided regex.

    Args:
        pattern_type: Named pattern from split_patterns.json, or "custom"
        custom_regex: Custom regex pattern (required if pattern_type is "custom")

    Returns:
        Compiled regex pattern that matches chapter headers

    Raises:
        ValueError: If pattern_type is invalid or custom_regex missing for "custom" type
    """
    if pattern_type == "custom":
        if not custom_regex:
            raise ValueError("custom_regex is required when pattern_type is 'custom'")
        try:
            return re.compile(custom_regex, re.IGNORECASE | re.MULTILINE)
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
) -> List[DetectedChapter]:
    """
    Split a full book text into individual chapters and front/back matter.

    Detects chapter boundaries using the specified pattern. Additionally
    scans for front matter (preface, foreword, etc.) before the first
    chapter and back matter (epilogue, appendix, etc.) after the last.

    Args:
        book_text: Full text of the book to split
        pattern_type: Type of chapter pattern - "roman", "numeric", or "custom"
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
            of each stripped section ({"label": <heading>, "reason":
            "boilerplate"}) for transparency. Left untouched when None.

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

    # Get chapter detection pattern
    pattern = get_chapter_pattern(pattern_type, custom_regex)

    # Find all chapter headers
    matches = list(pattern.finditer(book_text))

    # User-supplied front/back-matter titles take precedence over the chapter
    # regex. Without this, a generic pattern like "allcaps_heading" would
    # claim a heading such as "TO THE CHILDREN" as the first chapter and the
    # downstream front-matter scan (which only looks BEFORE the first chapter)
    # would never see the user's declared title. Drop any chapter match whose
    # heading line normalizes to a user-declared front- or back-matter title;
    # _find_matter_sections will then re-tag those lines with the correct
    # kind on its second pass.
    user_matter_titles = [*front_matter_titles, *back_matter_titles]
    if user_matter_titles and matches:
        matches = [
            m for m in matches
            if _matches_user_title(m.group(0), user_matter_titles) is None
        ]

    # Compile matter patterns
    front_patterns = _compile_matter_patterns("front_matter_patterns") if auto_detect_front_matter else []
    back_patterns = _compile_matter_patterns("back_matter_patterns") if auto_detect_back_matter else []
    drop_patterns = _compile_matter_patterns("drop_matter_patterns") if auto_strip_boilerplate else []

    # Determine numbering strategy from pattern definition
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
        raise ValueError(
            f"No chapters detected with pattern type '{pattern_type}'. "
            f"Check that your book uses the expected chapter format."
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
                     body_override: Optional[str] = None) -> None:
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
        # Prepend the chapter-header image (if any) so it appears at the
        # start of the chapter body in the saved file, where every
        # downstream consumer (chunker, translator, EPUB builder) already
        # knows how to handle [IMAGE:...] placeholders.
        if header_image:
            content = f"{header_image}\n\n{content}" if content else header_image
        if len(content) < min_chapter_size and kind == "chapter":
            return  # filter false-positive chapters by size
        if not content:
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
                continue
            heading = f"Chapter {ident.upper()}"
            if subtitle:
                heading = f"{heading}\n{subtitle}"
        elif numbering == "numeric":
            try:
                num = int(ident)
            except (TypeError, ValueError):
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
) -> List[str]:
    """
    Save detected chapters/sections to individual text files.

    Files are written in reading order using ``position_index`` regardless
    of section kind (front matter, chapter, back matter). When
    ``write_manifest`` is True (default), a ``chapter_manifest`` is also
    merged into the parent project's ``project.json`` so downstream tools
    can render proper labels.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

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
