"""Tests for the per-request LLM timeout.

Regression cover for a batch translation that froze the dashboard for hours.
Both SDK clients were built with no ``timeout``, so each request inherited the
SDK default of 600s AND the SDK's own ``max_retries=2``, which stacked under
``call_llm``'s retry loop: nine 10-minute attempts, ~90 minutes on a single
chunk, with the worker thread blocked and the progress bar at 0% the whole time.

Observed against DeepInfra in ``logs/web_ui.log``::

    16:44:45  request sent
    16:54:45  openai._base_client: Retrying request to /chat/completions
    17:04:46  openai._base_client: Retrying request to /chat/completions

The contract pinned down here:
  1. Both clients are constructed with an explicit timeout and ``max_retries=0``
     so ``call_llm`` is the only retry loop.
  2. A timeout surfaces as ``RequestTimeoutError`` with an actionable message.
  3. ``call_llm`` does NOT retry a timeout.
  4. The budget is configurable per provider, then by env var, then a default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api_translator import (
    DEFAULT_REQUEST_TIMEOUT,
    RequestTimeoutError,
    call_anthropic_api,
    call_llm,
    call_openai_api,
    get_request_timeout,
)


class TestGetRequestTimeout:

    def test_defaults_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
        assert get_request_timeout("anthropic") == DEFAULT_REQUEST_TIMEOUT

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "42")
        assert get_request_timeout("anthropic") == 42.0

    def test_provider_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "42")
        with patch("src.api_translator.get_provider_config",
                   return_value={"id": "slowpoke", "timeout_seconds": 900}):
            assert get_request_timeout("slowpoke") == 900.0

    def test_unknown_provider_falls_back(self, monkeypatch):
        monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
        assert get_request_timeout("nope-not-a-provider") == DEFAULT_REQUEST_TIMEOUT

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
    def test_garbage_and_nonpositive_fall_back(self, monkeypatch, bad):
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", bad)
        assert get_request_timeout("anthropic") == DEFAULT_REQUEST_TIMEOUT


class TestClientsCarryTheTimeout:

    def test_anthropic_client_gets_timeout_and_no_sdk_retries(self, monkeypatch):
        pytest.importorskip("anthropic")
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "77")
        with patch("anthropic.Anthropic") as cls:
            client = MagicMock()
            block = MagicMock()
            block.type = "text"
            block.text = "Hola"
            client.messages.create.return_value = MagicMock(content=[block], usage=None)
            cls.return_value = client
            call_anthropic_api("prompt", api_key="k")

        kwargs = cls.call_args.kwargs
        assert kwargs["timeout"] == 77.0
        assert kwargs["max_retries"] == 0, (
            "SDK-level retries multiply call_llm's retries behind its back"
        )

    def test_anthropic_uses_provider_id_timeout_not_type_default(self, monkeypatch):
        """A second anthropic-type entry must get its own timeout_seconds."""
        pytest.importorskip("anthropic")
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "77")
        with patch("src.api_translator.get_provider_config",
                   return_value={"id": "custom-claude", "type": "anthropic",
                                 "timeout_seconds": 900}):
            with patch("anthropic.Anthropic") as cls:
                client = MagicMock()
                block = MagicMock()
                block.type = "text"
                block.text = "Hola"
                client.messages.create.return_value = MagicMock(
                    content=[block], usage=None,
                )
                cls.return_value = client
                call_anthropic_api("prompt", api_key="k",
                                   provider_id="custom-claude")

        assert cls.call_args.kwargs["timeout"] == 900.0

    def test_openai_client_gets_timeout_and_no_sdk_retries(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "88")
        with patch("openai.OpenAI") as cls:
            client = MagicMock()
            choice = MagicMock()
            choice.message.content = "Hola"
            client.chat.completions.create.return_value = MagicMock(choices=[choice])
            cls.return_value = client
            call_openai_api("prompt", api_key="k", base_url="http://x")

        kwargs = cls.call_args.kwargs
        assert kwargs["timeout"] == 88.0
        assert kwargs["max_retries"] == 0


class TestTimeoutSurfacesActionably:

    def test_openai_timeout_becomes_request_timeout_error(self, monkeypatch):
        openai = pytest.importorskip("openai")
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "180")
        with patch("openai.OpenAI") as cls:
            client = MagicMock()
            client.chat.completions.create.side_effect = openai.APITimeoutError(
                request=MagicMock()
            )
            cls.return_value = client
            with pytest.raises(RequestTimeoutError) as exc:
                call_openai_api("prompt", model="google/gemma-4-31B-it",
                                api_key="k", base_url="http://x",
                                provider_id="deepinfra")

        msg = str(exc.value)
        assert "deepinfra" in msg
        assert "180" in msg
        assert "google/gemma-4-31B-it" in msg
        assert "timeout_seconds" in msg, "the message must say how to fix it"

    def test_anthropic_timeout_becomes_request_timeout_error(self, monkeypatch):
        anthropic = pytest.importorskip("anthropic")
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "180")
        with patch("anthropic.Anthropic") as cls:
            client = MagicMock()
            client.messages.create.side_effect = anthropic.APITimeoutError(
                request=MagicMock()
            )
            cls.return_value = client
            with pytest.raises(RequestTimeoutError):
                call_anthropic_api("prompt", api_key="k")


class TestCallLlmDoesNotRetryTimeouts:

    def test_timeout_is_raised_on_the_first_attempt(self):
        """Retrying a stall triples the wait and changes nothing."""
        with patch("src.api_translator._dispatch_llm_call",
                   side_effect=RequestTimeoutError("stalled")) as dispatch:
            with pytest.raises(RequestTimeoutError):
                call_llm("prompt", provider="deepinfra",
                         model="google/gemma-4-31B-it", max_retries=3)

        assert dispatch.call_count == 1, (
            f"timeout must not be retried; dispatched {dispatch.call_count} times"
        )

    def test_ordinary_api_errors_are_still_retried(self):
        """The no-retry rule is scoped to timeouts only."""
        from src.api_translator import APIError

        with patch("src.api_translator._dispatch_llm_call",
                   side_effect=APIError("transient")) as dispatch:
            with pytest.raises(APIError):
                call_llm("prompt", provider="anthropic",
                         model="claude-sonnet-5", max_retries=3)

        assert dispatch.call_count == 3
