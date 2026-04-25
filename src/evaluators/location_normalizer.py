"""
Location normalizer for evaluator Issue objects.

Evaluators emit free-form ``Issue.location`` strings whose format differs per
evaluator (e.g. ``"char 142"``, ``"Character positions: 15, 42, 89"``,
``"source positions: [0, 50]"``, ``"250 words -> 275 words"``). This module
parses those strings into a structured :class:`NormalizedLocation` that the UI
(and any future LLM consumer) can reason about: which side of the translation
it belongs to (source/target), the paragraph index, an optional char span, and
surrounding context snippets.

It also exposes :func:`fan_out_issues`, a pure view-layer helper that splits
multi-position issues (dictionary / glossary) into one entry per location so
the UI can render them individually. It never mutates the underlying
:class:`EvalResult` — existing CLI/report consumers stay untouched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from ..models import Chunk, EvalResult, Issue
from ..utils.text_utils import (
    detect_paragraph_boundaries,
    extract_paragraphs,
    normalize_newlines,
)

logger = logging.getLogger(__name__)

SnippetSide = Literal["source", "target", "none"]

# How many characters of context to include on either side of the match.
_SNIPPET_CONTEXT = 80


@dataclass
class NormalizedLocation:
    """Structured representation of an :class:`Issue.location` string.

    Fields are best-effort: evaluators that emit aggregate comparisons
    (``length``, ``paragraph``) produce locations with ``side="none"`` and no
    span; per-char evaluators (``grammar``, ``blacklist``, ``completeness``)
    produce a target-side span; ``dictionary`` / ``glossary`` emit multiple
    locations that :func:`fan_out_issues` expands into one entry each.
    """

    raw: str
    side: SnippetSide = "none"
    paragraph_index: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    snippet_before: str = ""
    match: str = ""
    snippet_after: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedIssue:
    """Flattened, UI-friendly view of an :class:`Issue`.

    One ``NormalizedIssue`` is produced per discrete location, so a single
    :class:`Issue` with multiple positions (dictionary/glossary) becomes
    multiple entries. ``issue_index`` refers to the index of the *original*
    ``Issue`` in ``EvalResult.issues`` so that feedback can reference back to
    it unambiguously.
    """

    eval_name: str
    eval_version: str
    issue_index: int
    severity: str
    message: str
    suggestion: Optional[str]
    location: Optional[NormalizedLocation]
    metadata_excerpt: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "eval_name": self.eval_name,
            "eval_version": self.eval_version,
            "issue_index": self.issue_index,
            "severity": self.severity,
            "message": self.message,
            "suggestion": self.suggestion,
            "location": self.location.to_dict() if self.location else None,
            "metadata_excerpt": self.metadata_excerpt,
        }
        return data


# ---------------------------------------------------------------------------
# Helpers


def _safe_text(chunk: Chunk, side: SnippetSide) -> str:
    """Return the normalized text for a given side (empty on unknown/none)."""
    if side == "source":
        return normalize_newlines(chunk.source_text or "")
    if side == "target":
        return normalize_newlines(chunk.translated_text or "")
    return ""


def _paragraph_slice(text: str, offset: int) -> tuple[Optional[int], int, int]:
    """Find the paragraph containing ``offset`` in ``text``.

    Returns ``(paragraph_index, paragraph_start, paragraph_end)`` — the
    half-open span of the containing paragraph in the normalized text. The
    paragraph_index is the index into :func:`extract_paragraphs` output. If
    the text is empty, returns ``(None, 0, 0)``.
    """
    if not text:
        return None, 0, 0

    boundaries = detect_paragraph_boundaries(text)
    if not boundaries:
        return None, 0, len(text)

    # Largest boundary <= offset
    para_index = 0
    for i, start in enumerate(boundaries):
        if start <= offset:
            para_index = i
        else:
            break

    para_start = boundaries[para_index]
    if para_index + 1 < len(boundaries):
        para_end_raw = boundaries[para_index + 1]
        # Trim any trailing paragraph break chars to keep snippet tidy.
        para_end = para_end_raw
        while para_end > para_start and text[para_end - 1] in "\n \t":
            para_end -= 1
    else:
        para_end = len(text)

    return para_index, para_start, para_end


def _build_snippet(
    text: str,
    char_start: int,
    char_end: int,
) -> tuple[Optional[int], str, str, str]:
    """Build paragraph-bounded ``(paragraph_index, before, match, after)``.

    The before/after snippets are clamped to the containing paragraph and
    truncated to ~``_SNIPPET_CONTEXT`` chars with an ellipsis when the window
    exceeds the paragraph bounds.
    """
    if not text or char_start < 0 or char_end < char_start or char_start >= len(text):
        return None, "", "", ""

    char_end = min(char_end, len(text))

    para_index, para_start, para_end = _paragraph_slice(text, char_start)

    window_start = max(para_start, char_start - _SNIPPET_CONTEXT)
    window_end = min(para_end, char_end + _SNIPPET_CONTEXT)

    before = text[window_start:char_start]
    match = text[char_start:char_end]
    after = text[char_end:window_end]

    if window_start > para_start:
        before = "…" + before
    if window_end < para_end:
        after = after + "…"

    return para_index, before, match, after


def _resolve_match_length(
    text: str,
    char_start: int,
    message: str,
    metadata: dict[str, Any],
) -> int:
    """Best-effort match length for a single-position location.

    Dictionary's location lists positions but not lengths; the offending word
    is embedded in the message as ``'word': ...``. Grammar/blacklist include
    the word length in their own char span. If nothing works we fall back to
    a single-char highlight so the UI still shows a caret.
    """
    # Try to pull the quoted token out of the message (dictionary/blacklist).
    m = re.match(r"^'([^']+)'", message or "")
    if m:
        candidate = m.group(1)
        if text[char_start:char_start + len(candidate)] == candidate:
            return len(candidate)

    # Some evaluators stash the offending term in metadata.
    for key in ("word", "term", "match", "flagged_word"):
        value = metadata.get(key) if metadata else None
        if isinstance(value, str) and value:
            if text[char_start:char_start + len(value)] == value:
                return len(value)

    return 1


def _metadata_excerpt(eval_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a compact per-evaluator metadata summary for the UI.

    We intentionally keep this small: users can expand the raw metadata via
    the "raw" toggle when they need everything.
    """
    if not metadata:
        return {}

    interesting: dict[str, list[str]] = {
        "length": ["ratio", "source_word_count", "target_word_count"],
        "paragraph": [
            "source_paragraphs",
            "translation_paragraphs",
            "difference",
        ],
        "dictionary": ["total_positions", "flagged_words", "word"],
        "glossary": ["term", "variants_used"],
        "completeness": ["missing_markers", "truncation_indicators"],
        "blacklist": ["category", "word"],
        "grammar": ["rule_id", "category", "replacements"],
        "llm_judge": ["fluency", "fidelity", "regional", "voice"],
    }

    keys = interesting.get(eval_name, [])
    excerpt = {k: metadata[k] for k in keys if k in metadata}
    return excerpt


# ---------------------------------------------------------------------------
# Location parsers
#
# Each helper returns either a single NormalizedLocation or a list of them
# when a single raw location encodes multiple positions.


def _parse_char_range(raw: str) -> Optional[tuple[int, int]]:
    """Parse ``"char N"`` or ``"char N-M"`` → ``(start, end)``."""
    m = re.match(r"^\s*char\s+(\d+)(?:\s*-\s*(\d+))?\s*$", raw, re.IGNORECASE)
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start + 1
    if end <= start:
        end = start + 1
    return start, end


def _parse_positions_list(raw: str) -> list[int]:
    """Extract any list of integers from a location string."""
    return [int(n) for n in re.findall(r"\d+", raw)]


def _parse_character_position(raw: str) -> list[int]:
    """Parse dictionary-style ``"Character position(s): ..."`` strings.

    Handles the truncated form ``"... (N total)"`` by stripping the trailing
    "(N total)" so the total count isn't mistaken for a position.
    """
    if "character position" not in raw.lower():
        return []
    # Strip any "(N total)" suffix — that integer describes the aggregate
    # count, not an additional position.
    cleaned = re.sub(r"\(\s*\d+\s*total\s*\)", "", raw, flags=re.IGNORECASE)
    return _parse_positions_list(cleaned)


def _parse_source_positions(raw: str) -> list[int]:
    """Parse glossary ``"source positions: [N, M]"`` style."""
    if "source position" not in raw.lower():
        return []
    return _parse_positions_list(raw)


def _make_aggregate_location(raw: str) -> NormalizedLocation:
    """No span — used for length/paragraph style aggregate locations."""
    return NormalizedLocation(raw=raw, side="none")


def _make_span_location(
    raw: str,
    side: SnippetSide,
    text: str,
    start: int,
    end: int,
) -> NormalizedLocation:
    """Build a NormalizedLocation from a concrete char span."""
    para_idx, before, match, after = _build_snippet(text, start, end)
    return NormalizedLocation(
        raw=raw,
        side=side,
        paragraph_index=para_idx,
        char_start=start if match or (0 <= start < len(text)) else None,
        char_end=end if match or (0 <= end <= len(text)) else None,
        snippet_before=before,
        match=match,
        snippet_after=after,
    )


def _make_side_only_location(
    raw: str,
    side: SnippetSide,
    text: str,
) -> NormalizedLocation:
    """Location with no span but whose side is known (e.g. ``"translation"``).

    Returns just the full first paragraph as a context hint so the UI can
    still show something.
    """
    paragraphs = extract_paragraphs(text)
    snippet = paragraphs[0] if paragraphs else text
    if len(snippet) > _SNIPPET_CONTEXT * 2:
        snippet = snippet[: _SNIPPET_CONTEXT * 2] + "…"
    return NormalizedLocation(
        raw=raw,
        side=side,
        paragraph_index=0 if paragraphs else None,
        snippet_before="",
        match="",
        snippet_after=snippet,
    )


# ---------------------------------------------------------------------------
# Public API


def normalize_issue_location(
    issue: Issue,
    chunk: Chunk,
    eval_name: str,
) -> list[NormalizedLocation]:
    """Parse ``issue.location`` into one or more :class:`NormalizedLocation`.

    Returns a list — most evaluators yield a single entry, but dictionary /
    glossary can emit multiple positions in one string and we fan those out.
    Returns ``[]`` when the location is missing or purely informational and
    no context can be recovered.
    """
    raw = (issue.location or "").strip()
    if not raw:
        return []

    source_text = _safe_text(chunk, "source")
    target_text = _safe_text(chunk, "target")

    # Aggregate comparisons: length / paragraph / completeness meta markers.
    if eval_name == "length":
        return [_make_aggregate_location(raw)]
    if eval_name == "paragraph":
        return [_make_aggregate_location(raw)]

    # "char N" / "char N-M" — used by grammar, blacklist, completeness.
    span = _parse_char_range(raw)
    if span is not None:
        start, end = span
        # If the range was implicit (just "char N"), try to expand using the
        # token embedded in the message so the UI highlights the full word.
        explicit_range = re.search(r"char\s+\d+\s*-\s*\d+", raw, re.IGNORECASE)
        if not explicit_range:
            length = _resolve_match_length(target_text, start, issue.message, {})
            if length > 1:
                end = start + length
        return [_make_span_location(raw, "target", target_text, start, end)]

    lower = raw.lower()

    # Dictionary: "Character position N" or "Character positions: ..."
    if "character position" in lower:
        positions = _parse_character_position(raw)
        if not positions:
            return [NormalizedLocation(raw=raw, side="target")]
        locs: list[NormalizedLocation] = []
        for pos in positions:
            length = _resolve_match_length(target_text, pos, issue.message, {})
            locs.append(
                _make_span_location(raw, "target", target_text, pos, pos + length)
            )
        return locs

    # Glossary: "source positions: [...]"
    if "source position" in lower:
        positions = _parse_source_positions(raw)
        if not positions:
            return [_make_side_only_location(raw, "source", source_text)]
        locs = []
        for pos in positions:
            length = _resolve_match_length(source_text, pos, issue.message, {})
            locs.append(
                _make_span_location(raw, "source", source_text, pos, pos + length)
            )
        return locs

    # Glossary mixed: "source: [...], translation: [...]" — surface source-side
    # positions when present, otherwise treat as side=source without a span.
    if lower.startswith("source:") and "translation:" in lower:
        # Pull the first [...] chunk (source positions).
        first = re.search(r"\[([^\]]*)\]", raw)
        if first:
            positions = _parse_positions_list(first.group(1))
            if positions:
                locs = []
                for pos in positions:
                    length = _resolve_match_length(
                        source_text, pos, issue.message, {}
                    )
                    locs.append(
                        _make_span_location(
                            raw, "source", source_text, pos, pos + length
                        )
                    )
                return locs
        return [_make_side_only_location(raw, "source", source_text)]

    # Glossary consistency: "variants used: [...]" — info, no span.
    if lower.startswith("variants used"):
        return [_make_side_only_location(raw, "target", target_text)]

    # Side-only labels.
    if lower in {"translation", "translated_text"}:
        return [_make_side_only_location(raw, "target", target_text)]
    if lower == "source":
        return [_make_side_only_location(raw, "source", source_text)]

    # Completeness informational markers — no span but target-side context.
    if lower.startswith("end of text") or lower == "special markers":
        return [_make_side_only_location(raw, "target", target_text)]

    # LLM judge / runtime errors — location is the chunk id or a status token.
    if raw == chunk.id or lower in {
        "evaluator_initialization",
        "evaluator_execution",
    }:
        return [NormalizedLocation(raw=raw, side="none")]

    # Fallback — unknown format, keep raw for debugging.
    logger.debug("Unknown evaluator location format: evaluator=%s raw=%r", eval_name, raw)
    return [NormalizedLocation(raw=raw, side="none")]


def fan_out_issues(result: EvalResult, chunk: Chunk) -> list[NormalizedIssue]:
    """Return one :class:`NormalizedIssue` per discrete location.

    Pure view-layer helper — it does not mutate ``result``.
    """
    out: list[NormalizedIssue] = []
    excerpt = _metadata_excerpt(result.eval_name, result.metadata or {})

    for issue_index, issue in enumerate(result.issues):
        locations = normalize_issue_location(issue, chunk, result.eval_name)
        if not locations:
            out.append(
                NormalizedIssue(
                    eval_name=result.eval_name,
                    eval_version=result.eval_version,
                    issue_index=issue_index,
                    severity=issue.severity.value
                    if hasattr(issue.severity, "value")
                    else str(issue.severity),
                    message=issue.message,
                    suggestion=issue.suggestion,
                    location=None,
                    metadata_excerpt=excerpt,
                )
            )
            continue

        for location in locations:
            out.append(
                NormalizedIssue(
                    eval_name=result.eval_name,
                    eval_version=result.eval_version,
                    issue_index=issue_index,
                    severity=issue.severity.value
                    if hasattr(issue.severity, "value")
                    else str(issue.severity),
                    message=issue.message,
                    suggestion=issue.suggestion,
                    location=location,
                    metadata_excerpt=excerpt,
                )
            )

    return out


__all__ = [
    "NormalizedLocation",
    "NormalizedIssue",
    "normalize_issue_location",
    "fan_out_issues",
]
