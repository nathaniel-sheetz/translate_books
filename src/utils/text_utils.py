"""
Text utility functions for paragraph detection and word counting.

This module provides utilities for processing chapter text, including:
- Normalizing newlines across different platforms
- Detecting and extracting paragraphs
- Counting words and paragraphs consistently with evaluators
- Detecting and stripping [IMAGE:...] placeholders embedded by source ingestion
- Detecting [CAPTION] block markers that tag a paragraph as an image caption
- Accent/case-folded substring search + KWIC windowing (reader concordance and
  the annotation-review book-wide term search share these)

All functions handle edge cases like empty text, single paragraphs,
and mixed newline conventions.
"""

import re
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple


# Matches [IMAGE:filename.ext] or [IMAGE:filename.ext:description].
# Group 1 = filename (everything up to the first ':' or ']').
# Group 2 = description (everything after the second ':'), or None if absent.
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE:([^:\]]+)(?::([^\]]*))?\]")


def normalize_newlines(text: str) -> str:
    """
    Convert all newline styles to Unix format (\\n).

    Handles Windows (\\r\\n), old Mac (\\r), and Unix (\\n) formats.
    Multiple consecutive newlines are preserved for paragraph detection.

    Args:
        text: Input text with potentially mixed newline styles

    Returns:
        Text with all newlines normalized to \\n

    Example:
        >>> normalize_newlines("Hello\\r\\nWorld")
        'Hello\\nWorld'
        >>> normalize_newlines("Line1\\r\\rLine2")
        'Line1\\n\\nLine2'
    """
    # Replace Windows CRLF first (before individual CR)
    text = text.replace('\r\n', '\n')
    # Replace old Mac CR
    text = text.replace('\r', '\n')
    return text


def count_words(text: str) -> int:
    """
    Count words in text using whitespace splitting.

    Matches the word counting behavior used by Chunk.word_count and evaluators.
    Handles multi-language text including Spanish accents.

    Args:
        text: Input text to count words in

    Returns:
        Number of words (0 for empty or whitespace-only text)

    Example:
        >>> count_words("The quick brown fox")
        4
        >>> count_words("El niño comió pan")
        4
        >>> count_words("")
        0
        >>> count_words("   ")
        0
    """
    if not text or not text.strip():
        return 0
    return len(text.split())


def extract_paragraphs(text: str) -> List[str]:
    """
    Split text into paragraphs separated by double newlines.

    A paragraph boundary is defined as two or more consecutive newlines.
    Leading/trailing whitespace is stripped from each paragraph.
    Empty paragraphs are filtered out.

    Args:
        text: Input text with paragraph breaks (\\n\\n)

    Returns:
        List of paragraph strings, stripped and non-empty

    Example:
        >>> extract_paragraphs("Para 1\\n\\nPara 2\\n\\nPara 3")
        ['Para 1', 'Para 2', 'Para 3']
        >>> extract_paragraphs("Single paragraph")
        ['Single paragraph']
        >>> extract_paragraphs("")
        []
    """
    # Normalize newlines first
    text = normalize_newlines(text)

    # Split on double newline (or more)
    # Pattern matches 2 or more consecutive newlines
    paragraphs = re.split(r'\n\n+', text)

    # Strip each paragraph and filter empty ones
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    return paragraphs


def count_paragraphs(text: str) -> int:
    """
    Count number of paragraphs in text.

    Paragraphs are separated by double newlines (\\n\\n).

    Args:
        text: Input text to count paragraphs in

    Returns:
        Number of paragraphs (0 for empty text)

    Example:
        >>> count_paragraphs("Para 1\\n\\nPara 2\\n\\nPara 3")
        3
        >>> count_paragraphs("Single paragraph")
        1
        >>> count_paragraphs("")
        0
    """
    return len(extract_paragraphs(text))


def detect_paragraph_boundaries(text: str) -> List[int]:
    """
    Find character positions where each paragraph starts.

    Returns 0-indexed positions in the original (normalized) text where
    paragraphs begin. The first paragraph always starts at position 0.

    Args:
        text: Input text with paragraph breaks

    Returns:
        List of starting character positions for each paragraph
        Empty list if text is empty

    Example:
        >>> detect_paragraph_boundaries("Para 1\\n\\nPara 2")
        [0, 8]
        >>> detect_paragraph_boundaries("Single paragraph")
        [0]
        >>> detect_paragraph_boundaries("")
        []
    """
    # Normalize newlines
    text = normalize_newlines(text)

    if not text.strip():
        return []

    boundaries = [0]  # First paragraph always starts at position 0

    # Find all double-newline positions
    for match in re.finditer(r'\n\n+', text):
        # Next paragraph starts after the blank lines
        start_pos = match.end()

        # Skip any leading whitespace (spaces/tabs) after blank lines
        while start_pos < len(text) and text[start_pos] in ' \t':
            start_pos += 1

        # Only add if there's actual content after the boundary
        if start_pos < len(text):
            boundaries.append(start_pos)

    return boundaries


def image_placeholder_ranges(text: str) -> list[tuple[int, int]]:
    """
    Return the (start, end) character ranges of all [IMAGE:...] placeholders.

    Useful for filtering out tool matches that land inside a replaced
    placeholder region (e.g. LanguageTool whitespace warnings).

    Args:
        text: Text potentially containing image placeholders.

    Returns:
        List of (start, end) tuples (end is exclusive, matching slice semantics).
    """
    if not text:
        return []
    return [(m.start(), m.end()) for m in _IMAGE_PLACEHOLDER_RE.finditer(text)]


def image_filenames(text: str) -> set[str]:
    """Return the set of ``[IMAGE:filename]`` filenames (group 1) in *text*.

    Used to check that a translation preserves exactly the source's image
    placeholders. The description (group 2) is meant to be translated, so the
    filename is the stable identity of the token: a filename present in the
    source but missing from the translation is a dropped image; a filename in
    the translation but not the source is a hallucinated one.

    Example:
        >>> sorted(image_filenames("a [IMAGE:img/p7.jpg:a dog] b [IMAGE:fig1.png]"))
        ['fig1.png', 'img/p7.jpg']
        >>> image_filenames("no images here")
        set()
    """
    if not text:
        return set()
    return {m.group(1).strip() for m in _IMAGE_PLACEHOLDER_RE.finditer(text)}


def image_filename_counts(text: str) -> "Counter[str]":
    """Return a Counter of ``[IMAGE:filename]`` occurrences in *text*.

    Unlike :func:`image_filenames` (which deduplicates), this preserves the
    count of each filename so that a worker that emits a token twice is caught
    by a mismatch against the source's single occurrence.
    """
    from collections import Counter
    if not text:
        return Counter()
    return Counter(m.group(1).strip() for m in _IMAGE_PLACEHOLDER_RE.finditer(text))


def strip_image_placeholders(text: str) -> str:
    """
    Replace [IMAGE:...] tokens with equal-length whitespace.

    Character offsets are preserved so downstream tools (LanguageTool,
    word tokenizers) that report positions stay accurate against the
    original text.

    Both supported placeholder formats are handled:
    - ``[IMAGE:filename.ext]``
    - ``[IMAGE:filename.ext:description]``

    Args:
        text: Text potentially containing image placeholders.

    Returns:
        Text with each placeholder replaced by a run of spaces of the same length.

    Example:
        >>> strip_image_placeholders("a [IMAGE:images/i01.jpg] b")
        'a                        b'
    """
    if not text:
        return text
    return _IMAGE_PLACEHOLDER_RE.sub(lambda m: " " * len(m.group()), text)


# The with-description bullet is a safe superset of the filename-only wording
# (it covers both formats), so it is what a book-level "always include" uses.
_IMAGE_INSTRUCTION_WITH_DESCRIPTION = (
    "   - If the source contains image placeholders in the format "
    "[IMAGE:filename.ext:image description], copy\n"
    "     them into the translation exactly as-is at the same position "
    "in the text, translating only the image description."
)
_IMAGE_INSTRUCTION_FILENAME_ONLY = (
    "   - If the source contains image placeholders in the format "
    "[IMAGE:filename.ext], copy\n"
    "     them into the translation exactly as-is at the same position "
    "in the text."
)


def image_placeholder_instruction(source_text: str, *, always_include: bool = False) -> str:
    """
    Build the translation-prompt sub-bullet describing how to handle image placeholders.

    Inspects ``source_text`` and returns one of:

    - ``""`` — the source contains no ``[IMAGE:...]`` placeholders.
    - filename-only bullet — every placeholder is of the form
      ``[IMAGE:filename.ext]``.
    - with-description bullet — at least one placeholder includes a description
      (``[IMAGE:filename.ext:description]``). Mixed-format chunks also resolve
      here because the with-description wording is a safe superset.

    When ``always_include`` is true, the constant with-description (superset)
    bullet is returned regardless of this chunk's own placeholders. Books that
    contain image placeholders anywhere pass ``always_include=True`` for every
    chunk so this bullet is byte-identical across the book — keeping the fixed
    prompt prefix cacheable rather than fragmenting it on per-chunk image
    presence. The bullet's "If the source contains..." wording is a correct
    no-op on chunks that happen to have no images.

    Returned bullets include leading ``   - `` so they slot directly into the
    STRUCTURE PRESERVATION section of the translation prompt.

    Args:
        source_text: The chunk's source text.
        always_include: Emit the constant superset bullet regardless of
            ``source_text`` (book-level constant for cache stability).

    Returns:
        A bullet line (no trailing newline) or an empty string.
    """
    if always_include:
        return _IMAGE_INSTRUCTION_WITH_DESCRIPTION

    if not source_text:
        return ""

    matches = list(_IMAGE_PLACEHOLDER_RE.finditer(source_text))
    if not matches:
        return ""

    any_with_description = any(
        m.group(2) is not None and m.group(2).strip() != "" for m in matches
    )

    if any_with_description:
        return _IMAGE_INSTRUCTION_WITH_DESCRIPTION

    return _IMAGE_INSTRUCTION_FILENAME_ONLY


# ---------------------------------------------------------------------------
# [CAPTION] block marker
#
# Image captions arrive from source ingestion as ordinary paragraphs sitting
# directly under their image, which renders them as body prose. The marker tags
# such a paragraph so the EPUB builder and the reader can style it as a caption.
#
# It is a *leading atom*, deliberately not a wrapping delimiter like _italics_:
# a wrapper's closing half can be lost in an LLM round trip, silently degrading
# the paragraph back to body prose. A leading atom is countable, which is what
# lets harness_guard enforce exact parity the way it does for [IMAGE:...] and
# [FOOTNOTE:N].
#
# Only meaningful at the start of a block, so the literal string elsewhere in
# prose is left alone.
# ---------------------------------------------------------------------------

CAPTION_MARKER = "[CAPTION]"

# Anchored at block start; trailing horizontal whitespace is part of the marker
# so stripping it does not leave a leading space on the caption text.
_CAPTION_BLOCK_RE = re.compile(r"^\[CAPTION\][ \t]*")

# Blank-line block split, matching epub_builder._render_body_blocks.
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")


def is_caption_block(block: str) -> bool:
    """
    Return True when *block* is a caption paragraph.

    Only a block-leading marker counts; the literal text elsewhere in a
    paragraph is ordinary prose.

    Example:
        >>> is_caption_block("[CAPTION] The lamb with the longest tail.")
        True
        >>> is_caption_block("  [CAPTION] leading whitespace is tolerated")
        True
        >>> is_caption_block("He wrote [CAPTION] on the board.")
        False
    """
    if not block:
        return False
    return bool(_CAPTION_BLOCK_RE.match(block.lstrip()))


def strip_caption_marker(block: str) -> str:
    """
    Return *block* with a leading ``[CAPTION]`` marker removed.

    Non-caption blocks are returned unchanged.

    Example:
        >>> strip_caption_marker("[CAPTION] El cordero de la cola larga.")
        'El cordero de la cola larga.'
        >>> strip_caption_marker("Ordinary paragraph.")
        'Ordinary paragraph.'
    """
    if not block:
        return block
    return _CAPTION_BLOCK_RE.sub("", block.lstrip(), count=1)


def caption_block_count(text: str) -> int:
    """
    Count blank-line-separated blocks in *text* that carry the caption marker.

    This is the parity signal: a translation must contain exactly as many
    caption blocks as its source. A plain count is sufficient because the
    marker has no payload to compare (unlike image filenames or footnote
    numbers).

    Example:
        >>> caption_block_count("Body.\\n\\n[CAPTION] A dog.\\n\\nMore body.")
        1
        >>> caption_block_count("Nothing here.")
        0
    """
    if not text:
        return 0
    return sum(1 for block in _BLOCK_SPLIT_RE.split(text) if is_caption_block(block))


#: Block-leading marker, matched per line. A caption marker always opens its
#: block, so a line anchor is equivalent here and far cheaper than re-splitting.
_CAPTION_MARKER_LINE_RE = re.compile(r"^\[CAPTION\][ \t]*", re.MULTILINE)


def blank_caption_markers(text: str) -> str:
    """
    Replace block-leading ``[CAPTION]`` markers with equal-length whitespace.

    Character offsets are preserved, so evaluators that report positions
    (LanguageTool, word tokenizers) stay aligned with the original text. Same
    contract as :func:`strip_image_placeholders`.

    The caption's *text* is left intact — it is real prose and should still be
    spell- and grammar-checked. Only the marker is blanked, so "CAPTION" is not
    reported as a misspelling in every captioned paragraph.

    Example:
        >>> blank_caption_markers("[CAPTION] El cordero.")
        '          El cordero.'
    """
    if not text:
        return text
    return _CAPTION_MARKER_LINE_RE.sub(lambda m: " " * len(m.group()), text)


_CAPTION_INSTRUCTION = (
    "   - A paragraph beginning with the [CAPTION] marker is an image caption. "
    "Keep [CAPTION]\n"
    "     at the very start of the corresponding paragraph in your translation, "
    "followed by a\n"
    "     space, then translate the caption text normally. Never add a [CAPTION] "
    "marker to a\n"
    "     paragraph that does not carry one, and never drop one."
)


def caption_instruction(source_text: str, *, always_include: bool = False) -> str:
    """
    Build the translation-prompt sub-bullet describing how to handle captions.

    Returns ``""`` when *source_text* carries no caption blocks, otherwise the
    constant bullet.

    When ``always_include`` is true the bullet is returned regardless of this
    chunk's own content. Books containing captions anywhere pass
    ``always_include=True`` for every chunk so the bullet is byte-identical
    across the book — keeping the fixed prompt prefix cacheable rather than
    fragmenting it on per-chunk caption presence. This mirrors
    :func:`image_placeholder_instruction`, and the bullet's wording is a correct
    no-op on chunks that happen to have no captions.

    The returned bullet includes the leading ``   - `` so it slots directly into
    the STRUCTURE PRESERVATION section of the translation prompt.

    Args:
        source_text: The chunk's source text.
        always_include: Emit the bullet regardless of *source_text*
            (book-level constant for cache stability).

    Returns:
        A bullet line (no trailing newline) or an empty string.
    """
    if always_include:
        return _CAPTION_INSTRUCTION
    if not source_text:
        return ""
    return _CAPTION_INSTRUCTION if caption_block_count(source_text) else ""


# ---------------------------------------------------------------------------
# Accent/case-folded substring search
#
# Moved here from web_ui/app.py so non-web callers can reuse it: the annotation
# concordance runs from a CLI and must not import web_ui (same circular-dependency
# constraint src/endnotes.py documents). web_ui/app.py imports these back under
# its old private names, so the reader's "Find in book" behavior is unchanged.
# ---------------------------------------------------------------------------

# Words of context on each side of a KWIC snippet.
KWIC_WORDS = 7


def fold(text: str) -> str:
    """Accent/case-fold for substring matching: NFD, drop combining marks, casefold."""
    decomposed = unicodedata.normalize("NFD", text)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return no_marks.casefold()


def fold_with_map(text: str) -> Tuple[str, List[int]]:
    """Fold ``text`` and return ``(folded, orig_index)`` where ``orig_index[i]``
    is the index in the ORIGINAL ``text`` that produced folded char ``i``.

    Folding can change string length (combining marks dropped, ``casefold`` can
    expand a char to several), so match offsets in folded space cannot be reused
    against the original. This map carries them back to the original.
    """
    folded_chars: List[str] = []
    orig_index: List[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFD", ch):
            if unicodedata.combining(c):
                continue
            for fc in c.casefold():
                folded_chars.append(fc)
                orig_index.append(i)
    return "".join(folded_chars), orig_index


def find_folded(haystack: str, folded_query: str) -> Optional[Tuple[int, int]]:
    """First folded-substring match of ``folded_query`` in ``haystack``.

    Returns ``(start, end)`` offsets into the ORIGINAL ``haystack``, or ``None``
    if there is no match. ``folded_query`` must be pre-folded and non-empty
    (callers guard on min length).
    """
    if not folded_query:
        return None
    folded, orig_index = fold_with_map(haystack)
    pos = folded.find(folded_query)
    if pos == -1:
        return None
    start = orig_index[pos]
    end = orig_index[pos + len(folded_query) - 1] + 1
    return start, end


def _at_word_boundary(folded: str, start: int, end: int) -> bool:
    """True when ``folded[start:end]`` is not glued to an adjacent word character.

    Applied in folded space, where combining marks are already gone, so
    ``isalnum`` is a sound test for "part of the same word" in Latin scripts.
    """
    before = folded[start - 1] if start > 0 else ""
    after = folded[end] if end < len(folded) else ""
    return not (before.isalnum() or before == "_") and not (
        after.isalnum() or after == "_"
    )


def count_folded(folded_haystack: str, folded_query: str, *, whole_word: bool = False) -> int:
    """Count matches of an already-folded query in already-folded text.

    Takes pre-folded input so a caller searching many terms against the same
    corpus folds each sentence once rather than once per term.
    """
    if not folded_query or not folded_haystack:
        return 0
    if not whole_word:
        return folded_haystack.count(folded_query)
    total = 0
    pos = folded_haystack.find(folded_query)
    while pos != -1:
        if _at_word_boundary(folded_haystack, pos, pos + len(folded_query)):
            total += 1
        pos = folded_haystack.find(folded_query, pos + len(folded_query))
    return total


def iter_folded(haystack: str, folded_query: str, *, whole_word: bool = False):
    """Yield every non-overlapping ``(start, end)`` folded match in ``haystack``.

    Offsets index the ORIGINAL ``haystack``. :func:`find_folded` returns only the
    first match; the concordance needs all of them to count per-chapter usage.

    ``whole_word`` rejects matches glued to an adjacent word character — without
    it, searching "test" also hits *protestaba* and *detestable*, which is right
    for search-as-you-type but useless as evidence about a word's usage.
    """
    if not folded_query:
        return
    folded, orig_index = fold_with_map(haystack)
    search_from = 0
    while True:
        pos = folded.find(folded_query, search_from)
        if pos == -1:
            return
        end = pos + len(folded_query)
        if not whole_word or _at_word_boundary(folded, pos, end):
            yield orig_index[pos], orig_index[end - 1] + 1
        search_from = end


def kwic_window(
    text: str, start: int, end: int, words_each_side: int = KWIC_WORDS
) -> Tuple[str, int, int]:
    """Slice a word-window snippet around ``[start, end)`` in ``text``.

    Returns ``(snippet, match_start, match_end)`` where the offsets index into
    ``snippet``. Trimmed to word boundaries; whitespace is collapsed, and an
    ellipsis marks truncation on either side. No sentence segmentation.
    """
    match = text[start:end]
    before_words = text[:start].split()
    after_words = text[end:].split()
    left = " ".join(before_words[-words_each_side:]) if before_words else ""
    right = " ".join(after_words[:words_each_side]) if after_words else ""

    prefix = (left + " ") if left else ""
    if len(before_words) > words_each_side:
        prefix = "… " + prefix
    snippet = prefix + match
    if right:
        snippet += " " + right
    if len(after_words) > words_each_side:
        snippet += " …"

    ms = len(prefix)
    return snippet, ms, ms + len(match)


# ---------------------------------------------------------------------------
# Footnote reference tokens
# ---------------------------------------------------------------------------

# Matches [FOOTNOTE:N] where N is a 1-based, book-global index. Inserted by
# ``src/footnote_import`` at ingest to mark the exact position of an imported
# Gutenberg footnote reference, and carried verbatim through translation (the
# same survivable-token strategy as [IMAGE:...]) so the marker lands at the
# right spot in the target text.
FOOTNOTE_TOKEN_RE = re.compile(r"\[FOOTNOTE:(\d+)\]")


def footnote_token_numbers(text: str) -> list[int]:
    """Return the footnote numbers of every ``[FOOTNOTE:N]`` token, in order."""
    if not text:
        return []
    return [int(m.group(1)) for m in FOOTNOTE_TOKEN_RE.finditer(text)]


def footnote_token_counts(text: str) -> "Counter[int]":
    """Return a Counter of ``[FOOTNOTE:N]`` numbers in *text*.

    Used to check that a translation preserved exactly the source's footnote
    tokens: a number present in the source but missing from the translation is a
    dropped footnote marker; a number in the translation but not the source is a
    hallucinated one; a count mismatch catches a token emitted twice.
    """
    from collections import Counter
    if not text:
        return Counter()
    return Counter(int(m.group(1)) for m in FOOTNOTE_TOKEN_RE.finditer(text))


def footnote_tokens_preserved(source_text: str, translated_text: str) -> bool:
    """True when *translated_text* carries exactly the source's footnote tokens."""
    return footnote_token_counts(source_text) == footnote_token_counts(translated_text)


def strip_footnote_tokens(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Remove every ``[FOOTNOTE:N]`` token; return ``(clean_text, placements)``.

    ``placements`` is ``[(number, position_in_clean_text), ...]`` in document
    order, where ``position_in_clean_text`` is the character offset in the
    returned token-free text at which the token sat. This is what the
    post-translation conversion uses to re-anchor each imported footnote.
    """
    placements: list[tuple[int, int]] = []
    out: list[str] = []
    last = 0
    clean_len = 0
    for m in FOOTNOTE_TOKEN_RE.finditer(text):
        segment = text[last:m.start()]
        out.append(segment)
        clean_len += len(segment)
        placements.append((int(m.group(1)), clean_len))
        last = m.end()
    out.append(text[last:])
    return "".join(out), placements


_FOOTNOTE_INSTRUCTION = (
    "   - If the source contains footnote markers in the format [FOOTNOTE:N] "
    "(N is a number),\n"
    "     copy them into the translation exactly as-is at the same position in "
    "the text. Never\n"
    "     translate, renumber, move, or drop them."
)


def footnote_placeholder_instruction(source_text: str, *, always_include: bool = False) -> str:
    """Build the translation-prompt sub-bullet for footnote markers.

    Returns the bullet only when the book/chunk actually has footnote tokens, so
    a book without imported footnotes never sees it. ``always_include`` forces
    the (constant) bullet regardless of this chunk's own tokens — a book-level
    knob for cache-prefix stability, mirroring ``image_placeholder_instruction``.
    The bullet includes leading ``   - `` so it slots into the STRUCTURE
    PRESERVATION section next to the image bullet.
    """
    if always_include:
        return _FOOTNOTE_INSTRUCTION
    if not source_text:
        return ""
    return _FOOTNOTE_INSTRUCTION if FOOTNOTE_TOKEN_RE.search(source_text) else ""


# Dialogue-handling instructions live in a standalone prompt file so the (long)
# house-style block can be maintained separately from the system prompt and
# injected only into chunks that actually contain dialogue — the same
# conditional-wildcard pattern as image_placeholder_instruction above.
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Inject the block when at least this many paragraphs in the chunk look like
# dialogue. 1 favors coverage over token-thrift: any dialogue in the chunk gets
# the rules. Bump this if incidental quotes trigger it too eagerly.
_DIALOGUE_MIN_PARAGRAPHS = 1


def _resolve_dialogue_path() -> Path:
    """Return the user's ``prompts/dialogue.txt`` if present, else the committed
    ``prompts/dialogue.example.txt`` fallback (mirrors the per-user prompt
    convention in ``style_guide_wizard._resolve_prompt_path``)."""
    user_path = _PROMPTS_DIR / "dialogue.txt"
    if user_path.exists():
        return user_path
    example_path = _PROMPTS_DIR / "dialogue.example.txt"
    if example_path.exists():
        return example_path
    raise FileNotFoundError(f"Neither {user_path} nor {example_path} found")


def _load_dialogue_block() -> str:
    """Load the dialogue instructions, framed as a self-contained prompt section.

    Framing the section here (rather than in the template) keeps the empty case
    clean: when a chunk has no dialogue the wildcard renders to "" with no orphan
    header. Read fresh each call (like ``load_prompt_template``) so edits to the
    file take effect without restarting a long-running process.
    """
    path = _resolve_dialogue_path()
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        raise FileNotFoundError(f"{path} exists but is empty")
    separator = "=" * 80
    return f"{separator}\nDIALOGUE FORMATTING\n{separator}\n\n{body}"


def _source_has_dialogue(source_text: str) -> bool:
    """True when the chunk contains at least ``_DIALOGUE_MIN_PARAGRAPHS`` dialogue
    paragraphs, using the chunker's own ``_is_dialogue`` rules."""
    # Lazy import: chunker imports this module, so a top-level import would cycle.
    try:
        from src.chunker import _is_dialogue
    except ImportError:
        return False

    hits = sum(1 for para in extract_paragraphs(source_text) if _is_dialogue(para))
    return hits >= _DIALOGUE_MIN_PARAGRAPHS


def source_has_dialogue(source_text: str, target_language: str = "Spanish") -> bool:
    """Public wrapper: True when ``source_text`` has dialogue for a Spanish target.

    Non-Spanish targets always return False (the dialogue block is Spanish-gated).
    Prefer this over ``_source_has_dialogue`` from outside ``text_utils``.
    """
    target = target_language.lower() if target_language else ""
    if not any(key in target for key in ("spanish", "español", "espanol")):
        return False
    return _source_has_dialogue(source_text)


def dialogue_instruction(
    source_text: str,
    target_language: str = "Spanish",
    *,
    always_include: bool = False,
) -> str:
    """Build the DIALOGUE FORMATTING prompt section for a single chunk.

    Returns the framed instructions block when ``source_text`` contains dialogue
    and the target is Spanish, otherwise ``""``. The instructions encode Spanish
    raya/guillemet house style, so they are gated to Spanish targets to avoid
    injecting Spanish-specific rules into other languages.

    When ``always_include`` is true, the block is returned for every Spanish-target
    chunk regardless of whether this chunk has dialogue — a per-book opt-in so the
    block sits in the byte-identical fixed prefix (cacheable across all chunks)
    instead of appearing only on dialogue-bearing chunks. The Spanish gate still
    applies: a non-Spanish target stays empty even with ``always_include``.

    Args:
        source_text: The chunk's source text.
        target_language: The translation target language.
        always_include: Emit the block on every Spanish-target chunk, not only
            chunks that contain dialogue (book-level constant for cache stability).

    Returns:
        The framed dialogue section (no trailing newline) or an empty string.
    """
    target = target_language.lower() if target_language else ""
    if not any(key in target for key in ("spanish", "español", "espanol")):
        return ""

    if always_include:
        return _load_dialogue_block()

    if not source_text:
        return ""

    if not _source_has_dialogue(source_text):
        return ""

    return _load_dialogue_block()
