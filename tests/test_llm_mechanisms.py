"""Unit tests for src.llm_mechanisms (no API call, no CLI process, no server)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import api_translator, llm_mechanisms, local_llm
from src.llm_mechanisms import (
    Completion,
    MechanismError,
    MechanismUnavailable,
    check_request_model,
    complete,
    list_models,
    mechanism_of,
)

CATALOG = {
    "default_provider": "anthropic",
    "default_model": "claude-sonnet-5",
    "providers": [
        {
            "id": "anthropic", "type": "anthropic", "api_key_env_var": "ANTHROPIC_API_KEY",
            "models": [{"id": "claude-sonnet-5", "name": "Claude Sonnet 5",
                        "pricing": {"input": 2.0, "output": 10.0}}],
        },
        {
            "id": "deepinfra", "type": "openai-compatible", "api_key_env_var": "DEEPINFRA_API_KEY",
            "base_url": "https://api.deepinfra.com/v1/openai",
            "models": [{"id": "big/model", "name": "Big", "pricing": {"input": 0.1, "output": 0.5}}],
        },
        {
            "id": "claude-headless", "type": "headless", "cli": "claude",
            "models": [{"id": "claude-sonnet-5-5", "name": "Sonnet 5.5 (headless)", "effort": "medium"}],
        },
        {
            "id": "cursor-headless", "type": "headless", "cli": "cursor",
            "models": [{"id": "grok-4.7-medium", "name": "Grok 4.7 (headless)"}],
        },
        {
            "id": "local", "type": "local", "base_url": "http://box:8080/v1",
            "api_key_env_var": "LOCAL_LLM_KEY",
            "models": [
                {"id": "gemma-4-31b", "name": "Gemma (local)", "aliases": ["gemma31"]},
                {"id": "qwen3.8-27b", "name": "Qwen (local)", "aliases": ["qwen27"]},
            ],
        },
    ],
}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(api_translator, "_LLM_CONFIG_CACHE", json.loads(json.dumps(CATALOG)))


class _Wave:
    """A stand-in for ``run_headless_wave`` that writes ``text`` as the one job's draft."""

    def __init__(self, text="El gato.", result=None, usage=None):
        self.text = text
        self.result = result
        self.usage = usage
        self.calls = []

    def __call__(self, jobs, **kwargs):
        self.calls.append({"jobs": jobs, **kwargs})
        if self.result is not None:
            return self.result
        Path(jobs[0]["output_path"]).write_text(self.text + chr(10), encoding="utf-8")
        out = {"wrote": [jobs[0]["id"]], "failed": [], "counts": {"wrote": 1, "failed": 0, "todo": 1}}
        if self.usage is not None:
            out["usage"] = self.usage
        return out


def test_mechanism_of_reads_the_provider_type():
    by_id = {p["id"]: mechanism_of(p) for p in CATALOG["providers"]}
    assert by_id == {
        "anthropic": "api", "deepinfra": "api",
        "claude-headless": "headless", "cursor-headless": "headless", "local": "local",
    }
    # An entry with no type is what call_llm has always treated as an API.
    assert mechanism_of({"id": "x"}) == "api"


class TestCheckRequestModel:
    @pytest.mark.parametrize("provider", ["claude-headless", "cursor-headless", "local"])
    def test_a_model_a_headless_or_local_provider_does_not_list_is_refused(self, provider):
        with pytest.raises(ValueError, match=f"not listed under provider '{provider}'"):
            check_request_model(provider, "claude-opus-9")

    @pytest.mark.parametrize("provider, model", [
        ("claude-headless", "claude-sonnet-5-5"),
        ("cursor-headless", "grok-4.7-medium"),
        ("local", "qwen3.8-27b"),
    ])
    def test_a_listed_model_passes(self, provider, model):
        check_request_model(provider, model)

    def test_an_api_provider_still_runs_a_model_the_catalog_does_not_list(self):
        check_request_model("anthropic", "claude-opus-9")

    def test_no_model_is_left_for_complete_to_refuse(self):
        check_request_model("claude-headless", None)

    def test_an_unknown_provider_is_a_value_error(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            check_request_model("nope", "m")


class TestCompleteApi:
    def test_goes_through_call_llm_and_prices_the_answer(self, monkeypatch):
        seen = {}

        def fake_call_llm(prompt, **kw):
            seen.update(kw, prompt=prompt)
            return "El gato."

        monkeypatch.setattr(llm_mechanisms, "call_llm", fake_call_llm)
        done = complete("x" * 4000, provider="deepinfra", model="big/model",
                        call_type="retranslate_sentence", temperature=0.1, max_retries=2)
        assert done == Completion("El gato.", "api", "deepinfra", "big/model", 1000, 2, 0.000101)
        assert seen["provider"] == "deepinfra" and seen["model"] == "big/model"
        assert seen["temperature"] == 0.1 and seen["max_retries"] == 2
        assert seen["call_type"] == "retranslate_sentence"

    def test_no_model_means_the_catalog_default(self, monkeypatch):
        monkeypatch.setattr(llm_mechanisms, "call_llm", lambda prompt, **kw: "ok")
        assert complete("p", provider="anthropic").model == "claude-sonnet-5"

    def test_a_none_answer_is_an_empty_one(self, monkeypatch):
        monkeypatch.setattr(llm_mechanisms, "call_llm", lambda prompt, **kw: None)
        assert complete("p", provider="anthropic", model="claude-sonnet-5").text == ""

    def test_an_unknown_provider_is_a_value_error(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            complete("p", provider="nope", model="m")


class TestCompleteHeadless:
    def test_runs_one_claude_job_at_the_models_effort(self, monkeypatch, tmp_path):
        wave = _Wave("El gato.")
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", wave)
        log = tmp_path / ".harness" / "retranslate" / "usage.jsonl"
        done = complete("Translate: The cat.", provider="claude-headless", model="claude-sonnet-5-5",
                        call_type="retranslate_sentence", usage_log=log, headless_timeout_s=180)
        assert done.text == "El gato."
        assert (done.mechanism, done.provider, done.model) == ("headless", "claude-headless", "claude-sonnet-5-5")
        assert done.cost_usd == 0
        call = wave.calls[0]
        assert len(call["jobs"]) == 1
        assert call["jobs"][0]["input_text"] == "Translate: The cat."
        assert call["jobs"][0]["id"].startswith("retranslate_sentence-")
        assert call["cli"] == "claude" and call["model"] == "claude-sonnet-5-5"
        assert call["concurrency"] == 1
        assert call["extra_flags"] == ["--effort", "medium"]
        assert call["effort"] == "medium"
        assert call["usage_log"] == log
        assert call["job_timeout"] == 180
        assert call["cache"] == "off"

    def test_a_cursor_model_carries_its_effort_in_the_id(self, monkeypatch):
        wave = _Wave()
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", wave)
        complete("p", provider="cursor-headless", model="grok-4.7-medium")
        call = wave.calls[0]
        assert call["cli"] == "cursor" and call["model"] == "grok-4.7-medium"
        assert call["extra_flags"] == []

    def test_an_effort_on_a_cursor_entry_is_not_turned_into_claude_argv(self, monkeypatch):
        cfg = json.loads(json.dumps(CATALOG))
        cfg["providers"][3]["models"][0]["effort"] = "high"
        monkeypatch.setattr(api_translator, "_LLM_CONFIG_CACHE", cfg)
        wave = _Wave()
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", wave)
        complete("p", provider="cursor-headless", model="grok-4.7-medium")
        assert wave.calls[0]["extra_flags"] == []

    def test_a_model_the_catalog_does_not_list_still_runs(self, monkeypatch):
        wave = _Wave()
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", wave)
        complete("p", provider="claude-headless", model="claude-opus-9")
        assert wave.calls[0]["model"] == "claude-opus-9"
        assert wave.calls[0]["extra_flags"] == []

    def test_token_counts_are_the_clis_own(self, monkeypatch):
        usage = {"input": 12, "cache_creation": 4000, "cache_read": 100, "output": 31}
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", _Wave(usage=usage))
        done = complete("p", provider="claude-headless", model="claude-sonnet-5-5")
        assert (done.prompt_tokens, done.completion_tokens) == (4112, 31)

    def test_without_a_usage_block_the_counts_are_estimated(self, monkeypatch):
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", _Wave("x" * 40))
        done = complete("y" * 400, provider="claude-headless", model="claude-sonnet-5-5")
        assert (done.prompt_tokens, done.completion_tokens) == (100, 10)

    def test_a_wave_that_refused_to_start_carries_the_clis_message(self, monkeypatch):
        refused = {"error": "subscription preflight failed: claude is not logged in",
                   "wrote": [], "failed": [], "counts": {"wrote": 0, "failed": 0, "todo": 0}}
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", _Wave(result=refused))
        with pytest.raises(MechanismError, match="not logged in") as raised:
            complete("p", provider="claude-headless", model="claude-sonnet-5-5")
        assert not isinstance(raised.value, MechanismUnavailable)

    def test_a_failed_job_carries_its_error(self, monkeypatch):
        failed = {"wrote": [], "failed": [{"id": "j", "error": "timeout after 180s"}],
                  "counts": {"wrote": 0, "failed": 1, "todo": 1}}
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", _Wave(result=failed))
        with pytest.raises(MechanismError, match="timeout after 180s"):
            complete("p", provider="claude-headless", model="claude-sonnet-5-5")

    def test_a_model_is_required(self):
        with pytest.raises(ValueError, match="model is required"):
            complete("p", provider="claude-headless")

    @pytest.mark.parametrize("cli", ["", None, "  "])
    def test_a_provider_that_names_no_cli_does_not_run_claude(self, monkeypatch, cli):
        """The runner reads an empty family as Claude."""
        cfg = json.loads(json.dumps(CATALOG))
        cfg["providers"][2]["cli"] = cli
        monkeypatch.setattr(api_translator, "_LLM_CONFIG_CACHE", cfg)
        wave = _Wave()
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", wave)
        with pytest.raises(ValueError, match="names no 'cli'"):
            complete("p", provider="claude-headless", model="claude-sonnet-5-5")
        assert wave.calls == []

    def test_the_call_is_written_to_prompt_history(self, monkeypatch):
        logged = []
        monkeypatch.setattr(llm_mechanisms, "run_headless_wave", _Wave("El gato."))
        monkeypatch.setattr(llm_mechanisms, "log_prompt", lambda **kw: logged.append(kw))
        complete("p", provider="claude-headless", model="claude-sonnet-5-5",
                 call_type="retranslate_sentence", project_slug="my-book")
        assert logged[0]["mode"] == "headless"
        assert logged[0]["response"] == "El gato."
        assert logged[0]["project_slug"] == "my-book"
        assert logged[0]["extra"] == {"cli": "claude", "effort": "medium"}


class TestCompleteLocal:
    def test_asks_the_local_client_for_the_catalog_entry(self, monkeypatch):
        seen = {}

        def fake(provider, model, prompt, **kw):
            seen.update(provider=provider, model=model, prompt=prompt, **kw)
            return local_llm.LocalReply("El gato.", "gemma31", 812, 9)

        monkeypatch.setattr(local_llm, "complete", fake)
        done = complete("p", provider="local", model="gemma-4-31b", temperature=0.2, max_tokens=300)
        # The model reported is the one the server answered as.
        assert done == Completion("El gato.", "local", "local", "gemma31", 812, 9, 0.0)
        assert seen["provider"]["base_url"] == "http://box:8080/v1"
        assert seen["model"]["aliases"] == ["gemma31"]
        assert seen["temperature"] == 0.2 and seen["max_tokens"] == 300

    def test_missing_token_counts_are_estimated(self, monkeypatch):
        monkeypatch.setattr(local_llm, "complete",
                            lambda *a, **k: local_llm.LocalReply("x" * 40, "gemma-4-31b"))
        done = complete("y" * 400, provider="local", model="gemma-4-31b")
        assert (done.prompt_tokens, done.completion_tokens) == (100, 10)

    @pytest.mark.parametrize("error", [
        local_llm.ModelNotLoadedError("qwen3.8-27b is not loaded on the local server (loaded: gemma-4-31b)"),
        local_llm.ServerUnreachableError("local server http://box:8080/v1 did not answer (ConnectionError)"),
    ])
    def test_a_model_that_cannot_be_asked_is_unavailable(self, monkeypatch, error):
        def boom(*a, **k):
            raise error

        monkeypatch.setattr(local_llm, "complete", boom)
        with pytest.raises(MechanismUnavailable) as raised:
            complete("p", provider="local", model="qwen3.8-27b")
        assert str(raised.value) == str(error)

    def test_a_failed_call_is_an_error_not_unavailable(self, monkeypatch):
        def boom(*a, **k):
            raise local_llm.LocalLLMError("local server answered HTTP 500: oops")

        monkeypatch.setattr(local_llm, "complete", boom)
        with pytest.raises(MechanismError, match="HTTP 500") as raised:
            complete("p", provider="local", model="gemma-4-31b")
        assert not isinstance(raised.value, MechanismUnavailable)

    def test_the_call_is_written_to_prompt_history(self, monkeypatch):
        logged = []
        monkeypatch.setattr(local_llm, "complete",
                            lambda *a, **k: local_llm.LocalReply("El gato.", "gemma-4-31b", 5, 2))
        monkeypatch.setattr(llm_mechanisms, "log_prompt", lambda **kw: logged.append(kw))
        complete("p", provider="local", model="gemma-4-31b", call_type="retranslate_sentence")
        assert logged[0]["mode"] == "local"
        assert logged[0]["model"] == "gemma-4-31b"


class TestListModels:
    @pytest.fixture
    def ready(self, monkeypatch):
        """Keys set, both CLIs on PATH, Gemma loaded."""
        for name in ("ANTHROPIC_API_KEY", "DEEPINFRA_API_KEY", "LOCAL_LLM_KEY"):
            monkeypatch.setenv(name, "k")
        monkeypatch.setattr(llm_mechanisms, "cli_binary_present", lambda cli: True)
        monkeypatch.setattr(local_llm, "loaded_models", lambda provider, **k: ["gemma-4-31b"])

    def _rows(self, **kw):
        return {(r["provider"], r["id"]): r for r in list_models(**kw)}

    def test_every_catalog_model_gets_a_row_in_catalog_order(self, ready):
        assert [(r["provider"], r["id"], r["mechanism"]) for r in list_models()] == [
            ("anthropic", "claude-sonnet-5", "api"),
            ("deepinfra", "big/model", "api"),
            ("claude-headless", "claude-sonnet-5-5", "headless"),
            ("cursor-headless", "grok-4.7-medium", "headless"),
            ("local", "gemma-4-31b", "local"),
            ("local", "qwen3.8-27b", "local"),
        ]

    def test_only_the_loaded_local_model_is_available(self, ready):
        rows = self._rows()
        assert rows[("local", "gemma-4-31b")]["available"] is True
        assert rows[("local", "qwen3.8-27b")]["available"] is False
        assert rows[("local", "qwen3.8-27b")]["unavailable_reason"] == "not_loaded"

    def test_a_local_model_loaded_under_its_alias_is_available(self, ready, monkeypatch):
        monkeypatch.setattr(local_llm, "loaded_models", lambda provider, **k: ["qwen27"])
        rows = self._rows()
        assert rows[("local", "qwen3.8-27b")]["available"] is True
        assert rows[("local", "gemma-4-31b")]["unavailable_reason"] == "not_loaded"

    def test_a_server_that_is_down_takes_both_local_rows_with_it(self, ready, monkeypatch):
        def down(provider, **k):
            raise local_llm.ServerUnreachableError("did not answer")

        monkeypatch.setattr(local_llm, "loaded_models", down)
        rows = self._rows()
        assert {rows[("local", m)]["unavailable_reason"] for m in ("gemma-4-31b", "qwen3.8-27b")} == {"server_down"}

    def test_the_server_is_asked_once_for_the_whole_provider(self, ready, monkeypatch):
        asks = []
        monkeypatch.setattr(local_llm, "loaded_models", lambda provider, **k: asks.append(1) or [])
        list_models()
        assert len(asks) == 1

    def test_no_local_key_does_not_ask_the_server(self, ready, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_KEY")
        monkeypatch.setattr(local_llm, "loaded_models",
                            lambda provider, **k: pytest.fail("asked without a key"))
        assert self._rows()[("local", "gemma-4-31b")]["unavailable_reason"] == "no_key"

    def test_a_missing_cli_is_said_per_family(self, ready, monkeypatch):
        monkeypatch.setattr(llm_mechanisms, "cli_binary_present", lambda cli: cli == "claude")
        rows = self._rows()
        assert rows[("claude-headless", "claude-sonnet-5-5")]["available"] is True
        assert rows[("cursor-headless", "grok-4.7-medium")]["unavailable_reason"] == "cli_missing"

    def test_an_api_provider_needs_its_key(self, ready, monkeypatch):
        monkeypatch.delenv("DEEPINFRA_API_KEY")
        rows = self._rows()
        assert rows[("anthropic", "claude-sonnet-5")]["available"] is True
        assert rows[("deepinfra", "big/model")]["unavailable_reason"] == "no_key"

    def test_the_default_is_the_api_model_of_that_id(self, ready):
        assert [r["id"] for r in list_models() if r["is_default"]] == ["claude-sonnet-5"]

    def test_a_headless_model_sharing_the_default_id_is_not_the_default(self, ready, monkeypatch):
        cfg = json.loads(json.dumps(CATALOG))
        cfg["providers"][2]["models"].append({"id": "claude-sonnet-5", "name": "Sonnet 5 (headless)"})
        monkeypatch.setattr(api_translator, "_LLM_CONFIG_CACHE", cfg)
        assert [(r["provider"], r["id"]) for r in list_models() if r["is_default"]] == [
            ("anthropic", "claude-sonnet-5"),
        ]

    def test_a_screen_can_ask_for_some_mechanisms_only(self, ready):
        assert {r["mechanism"] for r in list_models(mechanisms=("api", "headless"))} == {"api", "headless"}
        assert {r["mechanism"] for r in list_models(mechanisms=("local",))} == {"local"}

    def test_headless_rows_carry_no_price(self, ready):
        assert self._rows()[("claude-headless", "claude-sonnet-5-5")]["pricing"] == {}
