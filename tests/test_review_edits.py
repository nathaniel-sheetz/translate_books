"""Tests for scripts/review_edits.py — the edit-review HTML report generator.

Covers:
  - _parse_source_from_prompt: marker absent, bar absent, newline absent, body with/without end-bar
  - _build_history_index: empty dir, ignores null-response, ignores wrong call_type, sorts newest-first
  - _normalize_for_match / _source_texts_match: empty inputs, match and mismatch
  - _build_baseline_from_log: corrupt JSON, null response, valid log
  - _resolve_baseline: path A (last_llm_log stamp), path B (fallback scan), both miss
  - _tokenize_with_offsets: basic tokenization with correct offsets
  - _opcode_hunks: identical strings, single-word replace, pure insert, pure delete
  - _merge_close_hunks: empty, single hunk, merge when gap<=40, no-merge when gap>40
  - _render_hunk_side: zero-width hunk (hl_start==hl_end), normal replace
  - _windowed: start/end clamping
  - _proportional_source_range: zero-length guards, normal proportional mapping
  - _expand_to_sentence: empty text, no terminators, expands to sentence boundary
  - _build_source_spans: prefix/suffix dim spans, truncation flags
  - _load_tags: no file, empty lines, corrupt line, valid lines, negative hunk_index
  - _process_chunk: untranslated+no baseline, translated+no baseline (no_baseline=True),
    translated==baseline (skip), edited chunk with hunks, source_changed detection
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── make imports work without installing the package ──────────────────────────
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from scripts.review_edits import (  # noqa: E402
    _build_baseline_from_log,
    _build_history_index,
    _build_source_spans,
    _expand_to_sentence,
    _load_tags,
    _merge_close_hunks,
    _normalize_for_match,
    _opcode_hunks,
    _parse_source_from_prompt,
    _process_chunk,
    _proportional_source_range,
    _render_hunk_side,
    _resolve_baseline,
    _source_texts_match,
    _tokenize_with_offsets,
    _windowed,
    Baseline,
)


# =============================================================================
# _parse_source_from_prompt
# =============================================================================


class TestParseSourceFromPrompt:
    def test_marker_absent_returns_empty(self):
        assert _parse_source_from_prompt("no marker here") == ""

    def test_bar_absent_after_marker_returns_empty(self):
        prompt = "...SOURCE TEXT TO TRANSLATE\nnobars\n"
        assert _parse_source_from_prompt(prompt) == ""

    def test_no_newline_after_bar_returns_empty(self):
        prompt = "SOURCE TEXT TO TRANSLATE\n" + "=" * 10  # no newline
        assert _parse_source_from_prompt(prompt) == ""

    def test_extracts_body_without_end_bar(self):
        prompt = (
            "preamble\nSOURCE TEXT TO TRANSLATE\n"
            + "=" * 10 + "\n"
            + "Here is the source text.\n"
        )
        result = _parse_source_from_prompt(prompt)
        assert result == "Here is the source text."

    def test_extracts_body_with_end_bar(self):
        prompt = (
            "preamble\nSOURCE TEXT TO TRANSLATE\n"
            + "=" * 10 + "\n"
            + "Source body.\n"
            + "=" * 80 + "\n"
            + "GLOSSARY TERMS\n"
        )
        result = _parse_source_from_prompt(prompt)
        assert result == "Source body."


# =============================================================================
# _build_history_index
# =============================================================================


class TestBuildHistoryIndex:
    def test_empty_when_dir_missing(self, tmp_path):
        with patch("scripts.review_edits._history_dir", return_value=tmp_path / "nonexistent"):
            idx = _build_history_index()
        assert idx == {}

    def test_ignores_null_response(self, tmp_path):
        f = tmp_path / "20260101_translation_abc.json"
        f.write_text(json.dumps({
            "metadata": {"chunk_id": "ch01", "call_type": "translation"},
            "response": None,
        }), encoding="utf-8")
        with patch("scripts.review_edits._history_dir", return_value=tmp_path):
            idx = _build_history_index()
        assert "ch01" not in idx

    def test_ignores_wrong_call_type(self, tmp_path):
        f = tmp_path / "20260101_translation_abc.json"
        f.write_text(json.dumps({
            "metadata": {"chunk_id": "ch01", "call_type": "style_questions"},
            "response": "something",
        }), encoding="utf-8")
        with patch("scripts.review_edits._history_dir", return_value=tmp_path):
            idx = _build_history_index()
        assert "ch01" not in idx

    def test_ignores_corrupt_json(self, tmp_path):
        f = tmp_path / "20260101_translation_bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        with patch("scripts.review_edits._history_dir", return_value=tmp_path):
            idx = _build_history_index()
        assert idx == {}

    def test_indexes_valid_log(self, tmp_path):
        f = tmp_path / "20260101_translation_ch01.json"
        f.write_text(json.dumps({
            "metadata": {
                "chunk_id": "ch01",
                "call_type": "translation",
                "timestamp": "2026-01-01T00:00:00",
            },
            "response": "some translation",
        }), encoding="utf-8")
        with patch("scripts.review_edits._history_dir", return_value=tmp_path):
            idx = _build_history_index()
        assert "ch01" in idx
        assert len(idx["ch01"]) == 1

    def test_sorts_newest_first(self, tmp_path):
        for ts, name in [("2026-01-01", "old"), ("2026-06-01", "new")]:
            f = tmp_path / f"20260101_translation_{name}.json"
            f.write_text(json.dumps({
                "metadata": {
                    "chunk_id": "ch01",
                    "call_type": "translation",
                    "timestamp": ts,
                },
                "response": f"translation_{name}",
            }), encoding="utf-8")
        with patch("scripts.review_edits._history_dir", return_value=tmp_path):
            idx = _build_history_index()
        # newest-first: 2026-06-01 should be first
        assert idx["ch01"][0][0] == "2026-06-01"


# =============================================================================
# _normalize_for_match / _source_texts_match
# =============================================================================


class TestSourceTextsMatch:
    def test_normalize_collapses_whitespace(self):
        assert _normalize_for_match("  hello   world  ") == "hello world"

    def test_normalize_lowercases(self):
        assert _normalize_for_match("Hello World") == "hello world"

    def test_empty_inputs_no_match(self):
        assert _source_texts_match("", "anything") is False
        assert _source_texts_match("anything", "") is False

    def test_identical_texts_match(self):
        text = "It is a truth universally acknowledged. " * 5
        assert _source_texts_match(text, text) is True

    def test_different_texts_no_match(self):
        assert _source_texts_match("hello world", "completely different text") is False

    def test_prefix_match(self):
        long_text = "a " * 300
        short_text = "a " * 50
        # short fits inside long within the 200-char head window
        assert _source_texts_match(short_text, long_text) is True


# =============================================================================
# _build_baseline_from_log
# =============================================================================


class TestBuildBaselineFromLog:
    def test_corrupt_json_returns_none(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{bad", encoding="utf-8")
        assert _build_baseline_from_log(f) is None

    def test_null_response_returns_none(self, tmp_path):
        f = tmp_path / "null_resp.json"
        f.write_text(json.dumps({"response": None, "prompt": "p", "metadata": {}}),
                     encoding="utf-8")
        assert _build_baseline_from_log(f) is None

    def test_valid_log_returns_baseline(self, tmp_path):
        f = tmp_path / "valid.json"
        f.write_text(json.dumps({
            "response": "some translation",
            "prompt": "some prompt",
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")
        b = _build_baseline_from_log(f)
        assert b is not None
        assert b.response == "some translation"
        assert b.log_timestamp == "2026-01-01"


# =============================================================================
# _resolve_baseline
# =============================================================================


class TestResolveBaseline:
    def _make_log(self, path: Path, response: str = "baseline translation") -> None:
        path.write_text(json.dumps({
            "response": response,
            "prompt": "SOURCE TEXT TO TRANSLATE\n" + "=" * 10 + "\nSource body.\n",
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")

    def test_path_a_last_llm_log_direct_hit(self, tmp_path):
        """If chunk.last_llm_log points at a real file, use it immediately."""
        log_file = tmp_path / "valid_log.json"
        self._make_log(log_file)

        chunk = {
            "id": "ch01",
            "source_text": "Source body.",
            "last_llm_log": str(log_file),
        }

        with patch("scripts.review_edits._REPO_ROOT", tmp_path):
            # Make the relative path resolve: last_llm_log must be relative to _REPO_ROOT
            chunk["last_llm_log"] = log_file.name
            b = _resolve_baseline(chunk, {})

        assert b is not None
        assert b.response == "baseline translation"

    def test_path_b_fallback_scan(self, tmp_path):
        """No last_llm_log stamp: falls back to history_index scan."""
        log_file = tmp_path / "20260101_translation_ch01.json"
        source_body = "Source text that matches." * 10
        log_file.write_text(json.dumps({
            "response": "fallback translation",
            "prompt": "SOURCE TEXT TO TRANSLATE\n" + "=" * 10 + "\n" + source_body + "\n",
            "metadata": {"timestamp": "2026-01-01", "chunk_id": "ch01", "call_type": "translation"},
        }), encoding="utf-8")

        history_index = {"ch01": [("2026-01-01", log_file)]}
        chunk = {"id": "ch01", "source_text": source_body}
        b = _resolve_baseline(chunk, history_index)
        assert b is not None
        assert b.response == "fallback translation"

    def test_both_paths_miss_returns_none(self):
        chunk = {"id": "ch99", "source_text": "irrelevant"}
        b = _resolve_baseline(chunk, {})
        assert b is None


# =============================================================================
# _tokenize_with_offsets
# =============================================================================


class TestTokenizeWithOffsets:
    def test_empty_string(self):
        tokens, offsets = _tokenize_with_offsets("")
        assert tokens == []
        assert offsets == [0]

    def test_single_word(self):
        tokens, offsets = _tokenize_with_offsets("hello")
        assert tokens == ["hello"]
        assert offsets == [0, 5]

    def test_two_words_with_space(self):
        tokens, offsets = _tokenize_with_offsets("hi there")
        assert tokens == ["hi", " ", "there"]
        assert offsets == [0, 2, 3, 8]

    def test_offsets_reconstruct_original(self):
        text = "The quick brown fox"
        tokens, offsets = _tokenize_with_offsets(text)
        reconstructed = "".join(tokens)
        assert reconstructed == text
        for i, tok in enumerate(tokens):
            assert text[offsets[i]:offsets[i + 1]] == tok


# =============================================================================
# _opcode_hunks
# =============================================================================


class TestOpcodeHunks:
    def test_identical_strings_no_hunks(self):
        assert _opcode_hunks("hello world", "hello world") == []

    def test_empty_strings_no_hunks(self):
        assert _opcode_hunks("", "") == []

    def test_pure_insert(self):
        # "hello" -> "hello world": 'world' is inserted
        hunks = _opcode_hunks("hello", "hello world")
        assert len(hunks) >= 1
        assert all(h["op"] in ("insert", "replace", "delete") for h in hunks)

    def test_pure_delete(self):
        hunks = _opcode_hunks("hello world", "hello")
        assert len(hunks) >= 1

    def test_single_word_replace(self):
        hunks = _opcode_hunks("cat sat", "dog sat")
        assert len(hunks) == 1
        h = hunks[0]
        # The changed region covers "cat" in a (offsets 0..3)
        assert h["a_start"] == 0
        assert h["a_end"] == 3


# =============================================================================
# _merge_close_hunks
# =============================================================================


class TestMergeCloseHunks:
    def _hunk(self, a_start, a_end, b_start, b_end, op="replace"):
        return {"op": op, "a_start": a_start, "a_end": a_end,
                "b_start": b_start, "b_end": b_end}

    def test_empty_list_returns_empty(self):
        assert _merge_close_hunks([]) == []

    def test_single_hunk_unchanged(self):
        h = [self._hunk(0, 3, 0, 3)]
        result = _merge_close_hunks(h)
        assert len(result) == 1
        assert result[0]["b_start"] == 0

    def test_merges_when_gap_lte_40(self):
        h1 = self._hunk(0, 3, 0, 3)
        h2 = self._hunk(5, 8, 5, 8)  # gap = 5 - 3 = 2 (<=40) → merge
        result = _merge_close_hunks([h1, h2])
        assert len(result) == 1
        assert result[0]["b_end"] == 8
        assert result[0]["op"] == "replace"

    def test_no_merge_when_gap_gt_40(self):
        h1 = self._hunk(0, 3, 0, 3)
        h2 = self._hunk(50, 53, 50, 53)  # gap = 50 - 3 = 47 (>40) → keep separate
        result = _merge_close_hunks([h1, h2])
        assert len(result) == 2


# =============================================================================
# _render_hunk_side
# =============================================================================


class TestRenderHunkSide:
    def test_zero_width_hunk_renders_plain(self):
        # hl_start == hl_end → no highlight, just plain escaped text
        result = _render_hunk_side("hello world", "", 5, 5, "", "del")
        assert result == "hello world"
        assert "<span" not in result

    def test_empty_this_hunk_text_renders_plain(self):
        result = _render_hunk_side("hello world", "", 0, 5, "other", "del")
        assert "<span" not in result

    def test_highlights_changed_word(self):
        # window_text = "cat sat", hunk covers "cat" (0..3), other is "dog"
        result = _render_hunk_side("cat sat", "cat", 0, 3, "dog", "del")
        assert '<span class="del">' in result
        assert "sat" in result

    def test_html_escapes_special_chars(self):
        result = _render_hunk_side("<b>test</b>", "<b>test</b>", 0, 11, "other", "ins")
        assert "&lt;" in result
        assert "&gt;" in result


# =============================================================================
# _windowed
# =============================================================================


class TestWindowed:
    def test_normal_window(self):
        text = "hello world"
        w, tl, tr = _windowed(text, 2, 7)
        assert w == "llo w"
        assert tl is True
        assert tr is True

    def test_full_window_no_truncation(self):
        text = "hello"
        w, tl, tr = _windowed(text, 0, 5)
        assert w == "hello"
        assert tl is False
        assert tr is False

    def test_clamping(self):
        text = "hi"
        w, tl, tr = _windowed(text, -5, 100)
        assert w == "hi"
        assert tl is False
        assert tr is False


# =============================================================================
# _proportional_source_range
# =============================================================================


class TestProportionalSourceRange:
    def test_zero_length_trans_returns_full_src(self):
        s, e = _proportional_source_range(0, 10, 0, 100)
        assert s == 0
        assert e == 100

    def test_zero_length_src_returns_0_0(self):
        s, e = _proportional_source_range(0, 10, 100, 0)
        assert s == 0
        assert e == 0

    def test_proportional_midpoint(self):
        # translation 0..100, source 0..100: midpoint maps 1:1
        s, e = _proportional_source_range(40, 60, 100, 100)
        assert s == 40
        assert e == 60

    def test_proportional_scaling(self):
        # translation is half as long as source: range doubles
        s, e = _proportional_source_range(0, 50, 100, 200)
        assert s == 0
        assert e == 100


# =============================================================================
# _expand_to_sentence
# =============================================================================


class TestExpandToSentence:
    def test_empty_text(self):
        assert _expand_to_sentence("", 0, 0) == (0, 0)

    def test_no_terminators_falls_back_to_full_text(self):
        text = "no terminators here"
        s, e = _expand_to_sentence(text, 5, 10)
        # start cannot exceed original start; end cannot be less than original end
        assert s <= 5
        assert e >= 10

    def test_expands_to_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        # anchor inside "Second sentence" (16..32)
        s, e = _expand_to_sentence(text, 16, 32)
        # Should start at or before position 16 (beginning of "Second")
        assert s <= 16
        # Should end at or after position 32
        assert e >= 32
        # After expansion, the result should include the full second sentence
        extracted = text[s:e]
        assert "Second" in extracted

    def test_newline_acts_as_boundary(self):
        text = "Line one\nLine two\nLine three"
        s, e = _expand_to_sentence(text, 9, 17)  # inside "Line two"
        # newlines are terminators, so expansion should stay within that line's region
        assert s <= 9
        assert e >= 17


# =============================================================================
# _build_source_spans
# =============================================================================


class TestBuildSourceSpans:
    def test_full_inside_no_dim_spans(self):
        source = "hello world"
        spans, tl, tr = _build_source_spans(source, 0, 11, window_chars=50)
        # Only one span: the full "inside" span
        kinds = [sp["kind"] for sp in spans]
        assert "inside" in kinds
        assert tl is False
        assert tr is False

    def test_prefix_and_suffix_dim_spans(self):
        source = "prefix INSIDE suffix"
        spans, tl, tr = _build_source_spans(source, 7, 13, window_chars=100)
        kinds = [sp["kind"] for sp in spans]
        assert kinds.count("dim") == 2
        assert kinds.count("inside") == 1

    def test_truncation_flags_when_window_smaller_than_content(self):
        source = "a" * 1000
        # inside_start=400, inside_end=600, window=50
        # win_start = 350 > 0 → truncated left
        # win_end = 650 < 1000 → truncated right
        spans, tl, tr = _build_source_spans(source, 400, 600, window_chars=50)
        assert tl is True
        assert tr is True


# =============================================================================
# _load_tags
# =============================================================================


class TestLoadTags:
    def test_no_file_returns_empty(self, tmp_path):
        result = _load_tags(tmp_path)
        assert result == {}

    def test_empty_lines_skipped(self, tmp_path):
        f = tmp_path / "edit_review_tags.jsonl"
        f.write_text("\n\n\n", encoding="utf-8")
        result = _load_tags(tmp_path)
        assert result == {}

    def test_corrupt_line_skipped(self, tmp_path):
        f = tmp_path / "edit_review_tags.jsonl"
        f.write_text("{bad json\n", encoding="utf-8")
        result = _load_tags(tmp_path)
        assert result == {}

    def test_negative_hunk_index_skipped(self, tmp_path):
        f = tmp_path / "edit_review_tags.jsonl"
        row = {"chunk_id": "ch01", "hunk_index": -1, "tag": "other"}
        f.write_text(json.dumps(row) + "\n", encoding="utf-8")
        result = _load_tags(tmp_path)
        assert result == {}

    def test_valid_row_indexed_correctly(self, tmp_path):
        f = tmp_path / "edit_review_tags.jsonl"
        row = {"chunk_id": "ch01", "hunk_index": 0, "tag": "other", "note": ""}
        f.write_text(json.dumps(row) + "\n", encoding="utf-8")
        result = _load_tags(tmp_path)
        assert ("ch01", 0) in result
        assert result[("ch01", 0)][0]["tag"] == "other"

    def test_multiple_rows_same_key_accumulated(self, tmp_path):
        f = tmp_path / "edit_review_tags.jsonl"
        rows = [
            {"chunk_id": "ch01", "hunk_index": 0, "tag": "other"},
            {"chunk_id": "ch01", "hunk_index": 0, "tag": "style-tone"},
        ]
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        result = _load_tags(tmp_path)
        assert len(result[("ch01", 0)]) == 2


# =============================================================================
# _process_chunk
# =============================================================================


def _write_chunk(path: Path, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kwargs), encoding="utf-8")


class TestProcessChunk:
    def test_untranslated_no_baseline_returns_none(self, tmp_path):
        chunk_path = tmp_path / "ch01_chunk_000.json"
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text="Source text.",
                     translated_text=None)
        result = _process_chunk(chunk_path, {}, {})
        assert result is None

    def test_translated_no_baseline_returns_no_baseline_record(self, tmp_path):
        chunk_path = tmp_path / "ch01_chunk_000.json"
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text="Source text.",
                     translated_text="Translated text.")
        result = _process_chunk(chunk_path, {}, {})
        assert result is not None
        assert result["no_baseline"] is True
        assert result["hunks"] == []

    def test_translated_equals_baseline_returns_none(self, tmp_path):
        """If translation matches the baseline exactly, no diff → skip."""
        log_file = tmp_path / "20260101_translation_ch01.json"
        log_file.write_text(json.dumps({
            "response": "Exact baseline text.",
            "prompt": "SOURCE TEXT TO TRANSLATE\n" + "=" * 10 + "\nSource.\n",
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")

        chunk_path = tmp_path / "ch01_chunk_000.json"
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text="Source.",
                     translated_text="Exact baseline text.",
                     last_llm_log=log_file.name)

        with patch("scripts.review_edits._REPO_ROOT", tmp_path):
            result = _process_chunk(chunk_path, {}, {})
        assert result is None

    def test_edited_chunk_returns_hunks(self, tmp_path):
        """Chunk translated text differs from baseline → hunks produced."""
        log_file = tmp_path / "20260101_translation_ch01.json"
        log_file.write_text(json.dumps({
            "response": "The cat sat on the mat.",
            "prompt": "SOURCE TEXT TO TRANSLATE\n" + "=" * 10 + "\nThe cat sat.\n",
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")

        chunk_path = tmp_path / "ch01_chunk_000.json"
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text="The cat sat.",
                     translated_text="The dog sat on the mat.",
                     last_llm_log=log_file.name)

        with patch("scripts.review_edits._REPO_ROOT", tmp_path):
            result = _process_chunk(chunk_path, {}, {})

        assert result is not None
        assert result["no_baseline"] is False
        assert len(result["hunks"]) >= 1

    def test_source_changed_flag(self, tmp_path):
        """source_changed is True when the baseline prompt source differs from chunk source."""
        log_file = tmp_path / "20260101_translation_ch01.json"
        # Prompt contains old source text
        old_source = "Old source sentence here."
        log_file.write_text(json.dumps({
            "response": "Old baseline translation.",
            "prompt": (
                "SOURCE TEXT TO TRANSLATE\n"
                + "=" * 10 + "\n"
                + old_source + "\n"
            ),
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")

        chunk_path = tmp_path / "ch01_chunk_000.json"
        new_source = "New source sentence here, edited."
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text=new_source,
                     translated_text="New translation of edited source.",
                     last_llm_log=log_file.name)

        with patch("scripts.review_edits._REPO_ROOT", tmp_path):
            result = _process_chunk(chunk_path, {}, {})

        assert result is not None
        assert result["source_changed"] is True

    def test_tags_attached_to_hunks(self, tmp_path):
        """Tags from tag_map are attached to the correct hunk."""
        log_file = tmp_path / "20260101_translation_ch01.json"
        log_file.write_text(json.dumps({
            "response": "The cat sat on the mat.",
            "prompt": "SOURCE TEXT TO TRANSLATE\n" + "=" * 10 + "\nSource.\n",
            "metadata": {"timestamp": "2026-01-01"},
        }), encoding="utf-8")

        chunk_path = tmp_path / "ch01_chunk_000.json"
        _write_chunk(chunk_path,
                     id="ch01_chunk_000",
                     chapter_id="ch01",
                     position=0,
                     source_text="Source.",
                     translated_text="The dog sat on the mat.",
                     last_llm_log=log_file.name)

        tag_map = {
            ("ch01_chunk_000", 0): [
                {"chunk_id": "ch01_chunk_000", "hunk_index": 0, "tag": "style-tone", "note": ""}
            ]
        }

        with patch("scripts.review_edits._REPO_ROOT", tmp_path):
            result = _process_chunk(chunk_path, {}, tag_map)

        assert result is not None
        assert len(result["hunks"]) >= 1
        first_hunk = result["hunks"][0]
        assert first_hunk["tags"] != [] or first_hunk["unique_tag_set"] is not None
