"""Unit tests for src.retranslator (no live LLM calls)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import retranslator
from src.llm_mechanisms import Completion
from src.retranslator import (
    RetranslationError,
    _build_prompt,
    _load_style_guide_content,
    _strip_markdown_fences,
    retranslate_sentence,
)


class TestStripMarkdownFences:
    def test_plain_text_unchanged(self):
        assert _strip_markdown_fences("El gato.") == "El gato."

    def test_strips_fenced_block(self):
        wrapped = "```\nEl gato.\n```"
        assert _strip_markdown_fences(wrapped) == "El gato."

    def test_strips_language_tagged_fence(self):
        wrapped = "```spanish\nEl gato.\n```"
        assert _strip_markdown_fences(wrapped) == "El gato."

    def test_strips_double_quotes(self):
        assert _strip_markdown_fences('"El gato."') == "El gato."

    def test_does_not_strip_mismatched_curly_quotes(self):
        # Function strips only same-character pairs; “…” has different open/close.
        s = "“El gato.”"
        assert _strip_markdown_fences(s) == s

    def test_does_not_strip_unmatched_quotes(self):
        assert _strip_markdown_fences('"El gato.') == '"El gato.'

    def test_strips_outer_whitespace(self):
        assert _strip_markdown_fences("   El gato.   ") == "El gato."


class TestLoadStyleGuideContent:
    def test_none_returns_empty(self):
        assert _load_style_guide_content(None) == ""

    def test_missing_path_returns_empty(self, tmp_path):
        assert _load_style_guide_content(tmp_path / "missing.json") == ""

    def test_invalid_json_returns_empty(self, tmp_path):
        bad = tmp_path / "style.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert _load_style_guide_content(bad) == ""

    def test_prefers_light_content(self, tmp_path):
        path = tmp_path / "style.json"
        path.write_text(json.dumps({
            "content": "FULL guide text",
            "light_content": "LIGHT guide text",
        }), encoding="utf-8")
        assert _load_style_guide_content(path) == "LIGHT guide text"

    def test_falls_back_to_content_when_light_blank(self, tmp_path):
        path = tmp_path / "style.json"
        path.write_text(json.dumps({
            "content": "FULL guide text",
            "light_content": "   ",
        }), encoding="utf-8")
        assert _load_style_guide_content(path) == "FULL guide text"

    def test_falls_back_when_light_absent(self, tmp_path):
        path = tmp_path / "style.json"
        path.write_text(json.dumps({"content": "FULL guide text"}), encoding="utf-8")
        assert _load_style_guide_content(path) == "FULL guide text"

    def test_empty_content_returns_empty(self, tmp_path):
        path = tmp_path / "style.json"
        path.write_text(json.dumps({"content": "", "light_content": ""}), encoding="utf-8")
        assert _load_style_guide_content(path) == ""

    def test_truncates_oversized_content(self, tmp_path):
        path = tmp_path / "style.json"
        big = "x" * (retranslator._STYLE_CHAR_LIMIT + 1000)
        path.write_text(json.dumps({"content": big}), encoding="utf-8")
        result = _load_style_guide_content(path)
        assert len(result) <= retranslator._STYLE_CHAR_LIMIT + 100
        assert result.endswith("[...truncated]")


class TestBuildPrompt:
    def test_includes_source_text(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
        )
        assert "The cat." in prompt

    def test_no_glossary_uses_sentinel(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
        )
        assert "No glossary terms specified." in prompt

    def test_no_style_guide_uses_sentinel(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
        )
        assert "(no style guide configured)" in prompt

    def test_no_context_uses_sentinel(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
            context_text=None,
        )
        assert "(no surrounding context provided)" in prompt

    def test_whitespace_only_context_uses_sentinel(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
            context_text="   \n  ",
        )
        assert "(no surrounding context provided)" in prompt

    def test_context_is_rendered(self):
        prompt = _build_prompt(
            source_text="The cat.",
            source_language="English",
            target_language="Spanish",
            style_guide_content="",
            glossary=None,
            context_text="Earlier sentence. The cat. Later sentence.",
        )
        assert "Earlier sentence." in prompt


def _answering(*texts, mechanism="api", seen=None):
    """A stand-in for ``complete`` that hands back ``texts`` in turn."""
    replies = iter(texts)

    def fake(prompt, *, provider, model, **kw):
        if seen is not None:
            seen.append({"prompt": prompt, "provider": provider, "model": model, **kw})
        text = next(replies)
        return Completion(
            text=text, mechanism=mechanism, provider=provider, model=model or "the-default",
            prompt_tokens=100, completion_tokens=10, cost_usd=0.002 if mechanism == "api" else 0.0,
        )

    return fake


class TestRetranslateSentence:
    def test_empty_source_raises(self):
        with pytest.raises(ValueError):
            retranslate_sentence("")

    def test_whitespace_source_raises(self):
        with pytest.raises(ValueError):
            retranslate_sentence("   \n  ")

    def test_success_path(self, monkeypatch):
        monkeypatch.setattr(retranslator, "complete", _answering("El gato."))
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        result = retranslate_sentence("The cat.", model="claude-sonnet-5")
        assert result.new_translation == "El gato."
        assert result.provider == "anthropic"
        assert result.model == "claude-sonnet-5"
        assert result.mechanism == "api"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 10
        assert result.cost_usd == 0.002
        assert result.raw_response == "El gato."

    def test_strips_fences_from_llm_output(self, monkeypatch):
        monkeypatch.setattr(retranslator, "complete", _answering("```" + chr(10) + "El gato." + chr(10) + "```"))
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        result = retranslate_sentence("The cat.")
        assert result.new_translation == "El gato."
        assert result.raw_response.startswith("```")

    def test_retry_on_empty_recovers(self, monkeypatch):
        seen = []
        monkeypatch.setattr(retranslator, "complete", _answering("", "El gato.", seen=seen))
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        result = retranslate_sentence("The cat.")
        assert result.new_translation == "El gato."
        assert "previous response was empty" in seen[1]["prompt"]
        # Both calls were made, so both are counted.
        assert result.prompt_tokens == 200
        assert result.cost_usd == 0.004

    def test_retry_on_empty_then_raises(self, monkeypatch):
        monkeypatch.setattr(retranslator, "complete", _answering("", ""))
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        with pytest.raises(RetranslationError):
            retranslate_sentence("The cat.")

    def test_resolves_provider_when_not_given(self, monkeypatch):
        seen = []
        monkeypatch.setattr(retranslator, "complete", _answering("El gato.", seen=seen))
        monkeypatch.setattr(retranslator, "resolve_provider_for_model", lambda m, **kw: "deepinfra")
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        retranslate_sentence("The cat.", model="some/model")
        assert seen[0]["provider"] == "deepinfra"

    def test_falls_back_to_default_provider_when_resolve_fails(self, monkeypatch):
        seen = []

        def boom(_, **kw):
            raise ValueError("unknown model")

        monkeypatch.setattr(retranslator, "complete", _answering("El gato.", seen=seen))
        monkeypatch.setattr(retranslator, "resolve_provider_for_model", boom)
        monkeypatch.setattr(retranslator, "get_default_provider", lambda: "anthropic")
        retranslate_sentence("The cat.", model="mystery-model")
        assert seen[0]["provider"] == "anthropic"

    def test_a_given_provider_is_not_second_guessed(self, monkeypatch):
        """The same model id can sit under an API and a headless provider."""
        seen = []
        monkeypatch.setattr(retranslator, "complete", _answering("El gato.", mechanism="headless", seen=seen))
        monkeypatch.setattr(retranslator, "resolve_provider_for_model", lambda m, **kw: "anthropic")
        result = retranslate_sentence("The cat.", model="claude-sonnet-5-5", provider="claude-headless")
        assert seen[0]["provider"] == "claude-headless"
        assert result.provider == "claude-headless"
        assert result.mechanism == "headless"
        assert result.cost_usd == 0

    def test_a_headless_call_logs_usage_under_the_book(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(retranslator, "complete", _answering("El gato.", mechanism="headless", seen=seen))
        book = tmp_path / "my-book"
        retranslate_sentence("The cat.", model="m", provider="claude-headless", project_dir=book)
        assert seen[0]["usage_log"] == book / ".harness" / "retranslate" / "usage.jsonl"
        assert seen[0]["project_slug"] == "my-book"
        # One sentence, with a person waiting: not the launcher's 30 minutes.
        assert seen[0]["headless_timeout_s"] == 180

    def test_without_a_book_no_usage_log_is_named(self, monkeypatch):
        seen = []
        monkeypatch.setattr(retranslator, "complete", _answering("El gato.", seen=seen))
        retranslate_sentence("The cat.", model="m", provider="anthropic")
        assert seen[0]["usage_log"] is None
