"""Unit tests for src.local_llm (no server: every call goes through a stub session)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import local_llm
from src.local_llm import (
    LocalLLMError,
    ModelNotLoadedError,
    ServerUnreachableError,
    complete,
    loaded_ids,
    loaded_models,
    served_id,
)

PROVIDER = {
    "id": "local",
    "type": "local",
    "base_url": "http://box:8080/v1/",
    "api_key_env_var": "LOCAL_LLM_KEY",
    "timeout_seconds": 90,
}
GEMMA = {"id": "gemma-4-31b", "aliases": ["gemma31"]}
QWEN = {"id": "qwen3.8-27b", "aliases": ["qwen27"]}


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    """Answers ``/models`` with ``loaded`` and ``/chat/completions`` with ``reply``."""

    def __init__(self, loaded=("gemma-4-31b",), reply="Hola.", status=200, post_error=None):
        self.loaded = list(loaded)
        self.reply = reply
        self.status = status
        self.post_error = post_error
        self.gets = []
        self.posts = []

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return _Resp({"data": [{"id": m} for m in self.loaded]})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.post_error is not None:
            raise self.post_error
        return _Resp(
            {
                "choices": [{"message": {"content": self.reply}}],
                "usage": {"prompt_tokens": 812, "completion_tokens": 9},
            },
            self.status,
        )


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_KEY", "test-key")


class TestLoadedIds:
    def test_a_plain_listing_is_all_loaded(self):
        assert loaded_ids({"data": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]

    def test_router_entries_not_in_memory_are_dropped(self):
        payload = {"data": [
            {"id": "a", "status": {"value": "loaded"}},
            {"id": "b", "status": {"value": "unloaded"}},
            {"id": "c", "status": "loading"},
            {"id": "d", "status": "loaded"},
        ]}
        assert loaded_ids(payload) == ["a", "d"]

    def test_junk_is_not_an_error(self):
        assert loaded_ids(None) == []
        assert loaded_ids({"data": None}) == []
        assert loaded_ids({"data": ["x", {"id": 3}, {"id": "ok"}]}) == ["ok"]


class TestServedId:
    def test_matches_the_id(self):
        assert served_id(GEMMA, ["gemma-4-31b"]) == "gemma-4-31b"

    def test_matches_an_alias_and_returns_the_servers_spelling(self):
        assert served_id(GEMMA, ["Gemma31"]) == "Gemma31"

    def test_another_model_is_not_a_match(self):
        assert served_id(QWEN, ["gemma-4-31b"]) is None

    def test_a_longer_name_is_not_a_match(self):
        assert served_id(GEMMA, ["gemma-4-31b-it-q8"]) is None

    def test_a_single_alias_given_as_a_string(self):
        assert served_id({"id": "x", "aliases": "y"}, ["y"]) == "y"


class TestLoadedModels:
    def test_asks_the_server_with_the_key(self):
        session = _Session(loaded=["qwen27"])
        assert loaded_models(PROVIDER, session=session) == ["qwen27"]
        assert session.gets[0]["url"] == "http://box:8080/v1/models"
        assert session.gets[0]["headers"] == {"Authorization": "Bearer test-key"}

    def test_a_second_ask_inside_the_ttl_is_answered_from_memory(self):
        session = _Session()
        loaded_models(PROVIDER, session=session)
        loaded_models(PROVIDER, session=session)
        assert len(session.gets) == 1

    def test_fresh_asks_again(self):
        session = _Session()
        loaded_models(PROVIDER, session=session)
        loaded_models(PROVIDER, session=session, fresh=True)
        assert len(session.gets) == 2

    def test_a_server_that_does_not_answer(self):
        class Down:
            def get(self, *a, **k):
                raise ConnectionError("refused")

        with pytest.raises(ServerUnreachableError, match="did not answer"):
            loaded_models(PROVIDER, session=Down())

    def test_a_refused_key(self):
        class Refusing:
            def get(self, *a, **k):
                return _Resp({"error": "nope"}, 401)

        with pytest.raises(ServerUnreachableError, match="HTTP 401"):
            loaded_models(PROVIDER, session=Refusing())

    def test_no_key_is_reported_before_anything_is_sent(self, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_KEY")
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        session = _Session()
        with pytest.raises(ServerUnreachableError, match="LOCAL_LLM_KEY is not set"):
            loaded_models(PROVIDER, session=session)
        assert session.gets == []

    def test_a_provider_with_no_url(self):
        with pytest.raises(ServerUnreachableError, match="base_url"):
            loaded_models({"id": "local", "type": "local"}, session=_Session())

    def test_something_else_answering_on_the_port(self):
        """A proxy page is a 200 too. Not a bare ValueError: the picker lists every mechanism."""
        class NotAModelServer:
            def get(self, *a, **k):
                return _Resp(ValueError("Expecting value: line 1 column 1 (char 0)"))

        with pytest.raises(ServerUnreachableError, match="not as a model server"):
            loaded_models(PROVIDER, session=NotAModelServer())


class TestComplete:
    def test_sends_the_prompt_to_the_loaded_model(self):
        session = _Session(reply="El gato.")
        reply = complete(PROVIDER, GEMMA, "Translate: The cat.", temperature=0.2, max_tokens=500, session=session)
        assert reply.text == "El gato."
        assert reply.model == "gemma-4-31b"
        assert (reply.prompt_tokens, reply.completion_tokens) == (812, 9)
        sent = session.posts[0]
        assert sent["url"] == "http://box:8080/v1/chat/completions"
        assert sent["json"]["messages"] == [{"role": "user", "content": "Translate: The cat."}]
        assert sent["json"]["temperature"] == 0.2
        assert sent["json"]["max_tokens"] == 500

    def test_thinking_is_switched_off(self):
        session = _Session()
        complete(PROVIDER, GEMMA, "p", session=session)
        assert session.posts[0]["json"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_the_request_names_the_servers_own_id(self):
        session = _Session(loaded=["gemma31"])
        reply = complete(PROVIDER, GEMMA, "p", session=session)
        assert session.posts[0]["json"]["model"] == "gemma31"
        assert reply.model == "gemma31"

    def test_a_model_that_is_not_loaded_is_refused_and_nothing_is_sent(self):
        session = _Session(loaded=["gemma-4-31b"])
        with pytest.raises(ModelNotLoadedError) as raised:
            complete(PROVIDER, QWEN, "p", session=session)
        assert "qwen3.8-27b is not loaded" in str(raised.value)
        assert "gemma-4-31b" in str(raised.value)
        assert session.posts == []

    def test_an_empty_server_is_refused_too(self):
        session = _Session(loaded=[])
        with pytest.raises(ModelNotLoadedError, match="loaded: nothing"):
            complete(PROVIDER, GEMMA, "p", session=session)
        assert session.posts == []

    def test_what_is_loaded_is_asked_afresh_before_every_prompt(self):
        """A model swapped out since the picker was drawn must not be asked by name."""
        session = _Session(loaded=["gemma-4-31b"])
        loaded_models(PROVIDER, session=session)
        session.loaded = ["qwen3.8-27b"]
        with pytest.raises(ModelNotLoadedError):
            complete(PROVIDER, GEMMA, "p", session=session)
        assert session.posts == []

    def test_the_providers_timeout_is_the_read_timeout(self):
        session = _Session()
        complete(PROVIDER, GEMMA, "p", session=session)
        assert session.posts[0]["timeout"] == (local_llm.CONNECT_TIMEOUT_S, 90.0)

    def test_a_callers_timeout_wins(self):
        session = _Session()
        complete(PROVIDER, GEMMA, "p", timeout_s=30, session=session)
        assert session.posts[0]["timeout"] == (local_llm.CONNECT_TIMEOUT_S, 30.0)

    def test_a_slow_answer_says_which_setting_to_raise(self):
        class ReadTimeout(Exception):
            pass

        session = _Session(post_error=ReadTimeout())
        with pytest.raises(LocalLLMError, match="timeout_seconds") as raised:
            complete(PROVIDER, GEMMA, "p", session=session)
        assert not isinstance(raised.value, ServerUnreachableError)

    def test_a_connection_dropped_after_the_prompt_went_out_is_a_failure(self):
        """Not "unreachable": that one promises nothing was sent."""
        session = _Session(post_error=ConnectionError("reset"))
        with pytest.raises(LocalLLMError, match="dropped the connection") as raised:
            complete(PROVIDER, GEMMA, "p", session=session)
        assert not isinstance(raised.value, ServerUnreachableError)

    def test_a_server_that_stops_accepting_connections_is_unreachable(self):
        class ConnectTimeout(Exception):
            pass

        session = _Session(post_error=ConnectTimeout())
        with pytest.raises(ServerUnreachableError):
            complete(PROVIDER, GEMMA, "p", session=session)

    def test_a_server_error_is_a_failure(self):
        session = _Session(status=500)
        with pytest.raises(LocalLLMError, match="HTTP 500"):
            complete(PROVIDER, GEMMA, "p", session=session)

    def test_a_reply_without_usage_has_no_token_counts(self):
        class NoUsage(_Session):
            def post(self, url, json=None, headers=None, timeout=None):
                return _Resp({"choices": [{"message": {"content": "Hola."}}]})

        reply = complete(PROVIDER, GEMMA, "p", session=NoUsage())
        assert reply.text == "Hola."
        assert reply.prompt_tokens is None and reply.completion_tokens is None

    def test_a_reply_that_cannot_be_read(self):
        class Garbled(_Session):
            def post(self, url, json=None, headers=None, timeout=None):
                return _Resp({"choices": []})

        with pytest.raises(LocalLLMError, match="could not be read"):
            complete(PROVIDER, GEMMA, "p", session=Garbled())

    def test_an_answer_that_is_all_reasoning_is_a_failure_not_an_empty_reply(self):
        """Reasoning is paid for out of max_tokens; an empty reply would be asked for twice."""
        class Reasoning(_Session):
            def post(self, url, json=None, headers=None, timeout=None):
                return _Resp({"choices": [{
                    "message": {"content": "", "reasoning_content": "The cat is a noun, so"},
                    "finish_reason": "length",
                }]})

        with pytest.raises(LocalLLMError, match="reasoned and wrote no answer") as raised:
            complete(PROVIDER, GEMMA, "p", max_tokens=64, session=Reasoning())
        assert "max_tokens=64" in str(raised.value)
        assert not isinstance(raised.value, ServerUnreachableError)

    def test_an_answer_cut_off_at_max_tokens_is_not_returned_as_whole(self):
        class CutOff(_Session):
            def post(self, url, json=None, headers=None, timeout=None):
                return _Resp({"choices": [{
                    "message": {"content": "El gato se sentó en la"},
                    "finish_reason": "length",
                }]})

        with pytest.raises(LocalLLMError, match="cut off at max_tokens=64"):
            complete(PROVIDER, GEMMA, "p", max_tokens=64, session=CutOff())

    def test_an_answer_that_finished_is_returned_whatever_it_reasoned(self):
        class Finished(_Session):
            def post(self, url, json=None, headers=None, timeout=None):
                return _Resp({"choices": [{
                    "message": {"content": "El gato.", "reasoning_content": "Short."},
                    "finish_reason": "stop",
                }]})

        assert complete(PROVIDER, GEMMA, "p", session=Finished()).text == "El gato."
