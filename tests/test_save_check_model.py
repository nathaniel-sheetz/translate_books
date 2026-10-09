"""Tests for the save check's model layer: the readout, the routing, the job behind a Save."""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import save_check
from src import save_check_model as scm
from tests.test_save_check import _open, _save, client, project  # noqa: F401  (fixtures)

GEMMA = scm.Profile("gemma-4-31b", ("gemma31",), "v1", 7.5)
QWEN = scm.Profile("qwen3.8-27b", ("qwen27",), "v2", 4.0)


def _reply(logit, lead=()):
    """A chat reply whose first answer token carries ``logit`` as B against A."""
    p_b = 1 / (1 + math.exp(-logit))
    top = [{"token": "B", "logprob": math.log(p_b)}, {"token": " A", "logprob": math.log(1 - p_b)},
           {"token": "C", "logprob": -40.0}]
    content = [{"token": t, "top_logprobs": []} for t in lead]
    content.append({"token": "B" if logit > 0 else "A", "top_logprobs": top})
    return {"choices": [{"logprobs": {"content": content}, "message": {"content": "B"}}]}


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class FakeServer:
    """Stands in for ``requests.Session`` against a llama-server."""

    def __init__(self, models, logit=9.0, reason="The verb does not agree.", down=False):
        self.models, self.logit, self.reason, self.down = models, logit, reason, down
        self.listed = 0
        self.asked = []

    def get(self, url, headers=None, timeout=None):
        self.listed += 1
        if self.down:
            raise ConnectionError("refused")
        return _Response({"data": self.models})

    def post(self, url, json=None, headers=None, timeout=None):
        if self.down:
            raise ConnectionError("refused")
        self.asked.append(json)
        if json.get("logprobs"):
            return _Response(_reply(self.logit))
        return _Response({"choices": [{"message": {"content": self.reason}}]})


def _backend(server, profiles=(GEMMA, QWEN)):
    return scm.LlamaServerBackend("http://mini:8080", "key", profiles, session=server)


# -------- the prompt --------

class TestRendering:
    def test_the_edit_in_words(self):
        assert scm.changed_spans("El perro viejo.", "La perra viejo.") == '"El" -> "La"; "perro" -> "perra"'
        assert scm.changed_spans("Llegó tarde.", "Llegó muy tarde.") == 'added "muy"'
        assert scm.changed_spans("Llegó muy tarde.", "Llegó tarde.") == 'removed "muy"'

    def test_the_user_turn(self):
        assert scm.render_user("The old dog.", "El perro viejo.", "La perra viejo.") == (
            "English: The old dog.\n"
            "Before: El perro viejo.\n"
            "After: La perra viejo.\n"
            'Changed: "El" -> "La"; "perro" -> "perra"\n'
            "Answer:"
        )
        assert scm.render_user("", "Hola.", "Hola, tú.").startswith("Before:")
        assert scm.render_explain_user("", "Hola.", "Hola, tú.").endswith("\nSlip:")

    def test_an_edit_of_marks_alone_is_punctuation_only(self):
        assert scm.punctuation_only("—Hola —dijo.", "—Hola —dijo")
        assert scm.punctuation_only("Hola.", "Hola.")
        assert not scm.punctuation_only("Hola.", "Hola, tú.")
        assert not scm.punctuation_only("En 1805.", "En 1806.")

    def test_a_hit_shows_what_was_typed_or_else_what_was_taken_out(self):
        assert scm.hit_text("Has venido.", "Has venido usted.") == "usted"
        assert scm.hit_text("Llegó muy tarde.", "Llegó tarde.") == "muy"
        assert len(scm.hit_text("a", "b " * 80)) == 60

    def test_every_profile_names_a_prompt_that_exists(self):
        assert scm.PROFILES[0].name == "gemma-4-31b"
        assert all(p.prompt in scm.SYSTEMS for p in scm.PROFILES)
        assert len({p.name for p in scm.PROFILES}) == len(scm.PROFILES)


# -------- the readout --------

class TestReadLetters:
    def test_log_odds_of_b_against_a(self):
        p_slip, mass, logit = scm.read_letters(_reply(3.0))
        assert logit == pytest.approx(3.0)
        assert p_slip == pytest.approx(1 / (1 + math.exp(-3.0)))
        assert mass == pytest.approx(1.0)

    def test_a_closing_think_tag_and_a_blank_are_not_the_answer(self):
        assert scm.read_letters(_reply(-2.0, lead=("</think>", "\n\n")))[2] == pytest.approx(-2.0)

    def test_a_letter_missing_from_the_top_tokens_is_capped(self):
        reply = {"choices": [{"logprobs": {"content": [
            {"token": "B", "top_logprobs": [{"token": "B", "logprob": 0.0}]}]}}]}
        assert scm.read_letters(reply)[2] == pytest.approx(-math.log(1e-13))

    def test_neither_letter_is_an_error(self):
        reply = {"choices": [{"logprobs": {"content": [
            {"token": "The", "top_logprobs": [{"token": "The", "logprob": 0.0}]}]}}]}
        with pytest.raises(ValueError):
            scm.read_letters(reply)


# -------- which model answers --------

class TestRouting:
    def test_the_loaded_model_is_the_one_asked(self):
        server = FakeServer([{"id": "qwen3.8-27b"}], logit=5.0)
        verdict = _backend(server).judge("The cat.", "El gato.", "La gato.")
        assert verdict.model == "qwen3.8-27b" and verdict.profile == "qwen3.8-27b"
        assert verdict.flagged and verdict.score == 5.0 and verdict.threshold == 4.0
        (asked,) = server.asked
        assert asked["model"] == "qwen3.8-27b"
        assert asked["messages"][0]["content"] == scm.SYSTEM_V2
        assert asked["chat_template_kwargs"] == {"enable_thinking": False}
        assert asked["max_tokens"] == 4 and asked["top_logprobs"] == 20

    def test_the_same_score_warns_on_one_model_and_not_on_another(self):
        qwen = _backend(FakeServer([{"id": "qwen27"}], logit=5.0)).judge("", "a b", "a c")
        gemma = _backend(FakeServer([{"id": "Gemma31"}], logit=5.0)).judge("", "a b", "a c")
        assert qwen.flagged and qwen.model == "qwen27"
        assert not gemma.flagged and gemma.model == "Gemma31" and gemma.prompt == "v1"

    def test_a_model_without_a_profile_is_never_asked(self, caplog):
        server = FakeServer([{"id": "translategemma-12b"}])
        backend = _backend(server)
        assert backend.judge("", "a b", "a c") is None
        assert backend.judge("", "a b", "a c") is None
        assert backend.explain("", "a b", "a c") is None
        assert server.asked == []
        assert sum("no profile" in r.message for r in caplog.records) == 1

    def test_a_router_listing_does_not_wake_a_model_that_is_not_in_memory(self):
        server = FakeServer([
            {"id": "gemma-4-31b", "status": {"value": "unloaded"}},
            {"id": "qwen3.8-27b", "status": {"value": "loaded"}},
        ])
        assert _backend(server).judge("", "a b", "a c").model == "qwen3.8-27b"
        server.models = [{"id": "gemma-4-31b", "status": {"value": "unloaded"}}]
        assert _backend(server).judge("", "a b", "a c") is None
        assert [a["model"] for a in server.asked] == ["qwen3.8-27b"]

    def test_the_preferred_profile_wins_when_two_are_loaded(self):
        server = FakeServer([{"id": "qwen3.8-27b"}, {"id": "gemma-4-31b"}])
        assert _backend(server).judge("", "a b", "a c").model == "gemma-4-31b"

    def test_the_listing_is_asked_for_once_while_it_is_fresh(self):
        server = FakeServer([{"id": "gemma-4-31b"}])
        backend = _backend(server)
        backend.judge("", "a b", "a c")
        backend.judge("", "a b", "a d")
        assert server.listed == 1 and len(server.asked) == 2

    def test_a_server_that_is_down_is_left_alone_for_a_while(self, monkeypatch):
        server = FakeServer([{"id": "gemma-4-31b"}], down=True)
        backend = _backend(server)
        clock = [1000.0]
        monkeypatch.setattr(scm.time, "monotonic", lambda: clock[0])
        assert backend.judge("", "a b", "a c") is None
        server.down = False
        assert backend.judge("", "a b", "a c") is None
        assert server.listed == 1
        clock[0] += scm.RETRY_AFTER_S + 1
        assert backend.judge("", "a b", "a c") is not None

    def test_a_swap_is_noticed_once_the_listing_is_stale(self, monkeypatch):
        server = FakeServer([{"id": "gemma-4-31b"}])
        backend = _backend(server)
        clock = [1000.0]
        monkeypatch.setattr(scm.time, "monotonic", lambda: clock[0])
        assert backend.judge("", "a b", "a c").model == "gemma-4-31b"
        clock[0] += scm.LOADED_TTL_S + 1
        server.models = [{"id": "qwen3.8-27b"}]
        assert backend.judge("", "a b", "a c").model == "qwen3.8-27b"

    def test_an_unreadable_reply_is_no_verdict_and_no_backoff(self):
        server = FakeServer([{"id": "gemma-4-31b"}])
        backend = _backend(server)
        server.post = lambda *a, **k: _Response({"choices": [{"logprobs": {"content": []}}]})
        assert backend.judge("", "a b", "a c") is None
        del server.post
        assert backend.judge("", "a b", "a c") is not None

    def test_the_reason_is_one_line_and_none_is_empty(self):
        server = FakeServer([{"id": "gemma-4-31b"}], reason=' The verb "Has"\ndoes not agree. ')
        backend = _backend(server)
        assert backend.explain("", "a b", "a c") == 'The verb "Has" does not agree.'
        assert server.asked[0]["max_tokens"] == 60 and "logprobs" not in server.asked[0]
        server.reason = "None."
        assert backend.explain("", "a b", "a c") == ""

    @pytest.mark.parametrize("content", ["", " \n", None])
    def test_an_empty_reason_is_no_answer_and_no_backoff(self, content):
        server = FakeServer([{"id": "gemma-4-31b"}], reason=content)
        backend = _backend(server)
        assert backend.explain("", "a b", "a c") is None
        assert backend.judge("", "a b", "a c") is not None

    def test_a_failing_chat_waits_longer_each_time_up_to_a_cap(self, monkeypatch, caplog):
        server = FakeServer([{"id": "gemma-4-31b"}])
        backend = _backend(server)
        clock = [1000.0]
        monkeypatch.setattr(scm.time, "monotonic", lambda: clock[0])

        def busy(*a, **k):
            return _Response({"error": "busy"}, status=500)

        server.post = busy
        for _ in range(6):
            assert backend.judge("", "a b", "a c") is None
            listed = server.listed
            # Left alone until the wait is over: not even the listing is asked for.
            assert backend.judge("", "a b", "a c") is None and server.listed == listed
            clock[0] += scm.RETRY_AFTER_MAX_S + 1
        del server.post
        assert backend.judge("", "a b", "a c") is not None
        server.post = busy
        assert backend.judge("", "a b", "a c") is None

        waits = [r.args[-1] for r in caplog.records if "unusable" in r.getMessage()]
        assert waits == [min(scm.RETRY_AFTER_S * 2 ** n, scm.RETRY_AFTER_MAX_S) for n in range(6)] + [
            scm.RETRY_AFTER_S]

    def test_a_refused_key_is_no_verdict_and_stays_out_of_the_log(self, caplog):
        server = FakeServer([{"id": "gemma-4-31b"}])
        server.get = lambda *a, **k: _Response({"error": "Invalid API Key"}, status=401)
        backend = scm.LlamaServerBackend("http://mini:8080", "s3cret-key", (GEMMA,), session=server)
        assert backend.judge("", "a b", "a c") is None and server.asked == []
        assert "HTTP 401" in caplog.text and "s3cret-key" not in caplog.text


# -------- configuration --------

class TestConfig:
    def test_off_unless_a_backend_is_named(self):
        assert scm.read_model_config(None).backend == ""
        assert scm.read_model_config({}).backend == ""
        assert scm.backend_for(scm.read_model_config({"url": "http://mini:8080"})) is None

    def test_a_backend_and_its_url(self):
        config = scm.read_model_config({"backend": "llama-server", "url": " http://mini:8080/ "})
        assert (config.backend, config.url) == ("llama-server", "http://mini:8080")
        assert config.profiles == scm.PROFILES

    def test_what_is_malformed_is_dropped(self):
        assert scm.read_model_config("llama-server").backend == ""
        assert scm.read_model_config({"backend": "ollama", "url": "http://x"}).backend == ""
        assert scm.read_model_config({"backend": "llama-server", "url": "mini:8080"}).url == ""

    def test_a_profile_can_be_refitted_or_added(self):
        config = scm.read_model_config({"backend": "llama-server", "url": "http://x", "profiles": [
            {"name": "Gemma-4-31B", "threshold": 6},
            {"name": "my-model", "prompt": "v2", "threshold": 3.5, "aliases": "mine"},
            {"name": "no-threshold", "prompt": "v1"},
            {"name": "bad-prompt", "prompt": "v9", "threshold": 1},
        ]})
        by_name = {p.name: p for p in config.profiles}
        assert by_name["gemma-4-31b"] == scm.Profile("gemma-4-31b", ("gemma31",), "v1", 6.0)
        assert by_name["my-model"] == scm.Profile("my-model", ("mine",), "v2", 3.5)
        assert "no-threshold" not in by_name and "bad-prompt" not in by_name

    def test_a_value_of_the_wrong_type_is_dropped_without_raising(self):
        on = {"backend": "llama-server", "url": "http://x"}
        config = scm.read_model_config({**on, "profiles": [
            {"name": "list-prompt", "prompt": ["v1"], "threshold": 1},
            {"name": "dict-prompt", "prompt": {"v1": 1}, "threshold": 1},
            {"name": "bool-threshold", "prompt": "v1", "threshold": True},
            {"name": 7, "prompt": "v1", "threshold": 1},
            "gemma-4-31b", 7,
        ]})
        assert config.backend == "llama-server" and config.profiles == scm.PROFILES
        assert scm.read_model_config({**on, "profiles": {"name": "x"}}).profiles == scm.PROFILES
        assert scm.read_model_config({"backend": 7, "url": "http://x"}).backend == ""
        assert scm.read_model_config({"backend": "llama-server", "url": 7}).url == ""

    def test_no_key_means_no_backend(self, monkeypatch):
        monkeypatch.setattr(scm, "_backends", {})
        monkeypatch.delenv(scm.KEY_ENV, raising=False)
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
        config = scm.read_model_config({"backend": "llama-server", "url": "http://mini:8080"})
        assert scm.backend_for(config) is None

    def test_one_backend_per_configuration(self, monkeypatch):
        monkeypatch.setattr(scm, "_backends", {})
        monkeypatch.setenv(scm.KEY_ENV, "k")
        config = scm.read_model_config({"backend": "llama-server", "url": "http://mini:8080"})
        assert scm.backend_for(config) is scm.backend_for(config)
        assert isinstance(scm.backend_for(config), scm.LlamaServerBackend)


# -------- the queue --------

class TestJobs:
    def test_a_job_runs_and_its_result_waits_to_be_read(self):
        jobs = scm.Jobs()
        job = jobs.submit(lambda: {"id": "w1"})
        assert jobs.wait(job, 5) == (True, {"id": "w1"})
        assert jobs.wait("unknown", 0) == (True, None)

    def test_a_job_that_raises_is_done_with_nothing(self):
        jobs = scm.Jobs()
        job = jobs.submit(lambda: 1 / 0)
        assert jobs.wait(job, 5) == (True, None)
        assert jobs.wait(jobs.submit(lambda: {"ok": 1}), 5) == (True, {"ok": 1})

    def test_the_oldest_waiting_job_is_dropped_when_saves_outrun_the_model(self):
        import threading

        jobs = scm.Jobs(max_waiting=1)
        gate = threading.Event()
        running = threading.Event()
        first = jobs.submit(lambda: (running.set(), gate.wait(5), {"n": 1})[2])
        assert running.wait(5)
        second = jobs.submit(lambda: {"n": 2})
        third = jobs.submit(lambda: {"n": 3})
        assert jobs.wait(second, 0) == (True, None)
        gate.set()
        assert jobs.wait(first, 5) == (True, {"n": 1})
        assert jobs.wait(third, 5) == (True, {"n": 3})


# -------- behind a Save --------

class FakeBackend:
    name = "fake"

    def __init__(self, flagged=True, available=True, reason="“Has” does not agree with “usted”."):
        self.flagged, self.available, self.reason = flagged, available, reason
        self.judged, self.explained = [], 0

    def judge(self, en, before, after):
        self.judged.append((en, before, after))
        if not self.available:
            return None
        return scm.Verdict(flagged=self.flagged, model="gemma-4-31b", profile="gemma-4-31b", prompt="v1",
                           backend=self.name, seconds=0.9, score=9.1 if self.flagged else -4.0, threshold=7.5)

    def explain(self, en, before, after):
        self.explained += 1
        return self.reason if self.available else None


@pytest.fixture
def model(project, monkeypatch):
    """The model layer switched on, answered by a :class:`FakeBackend`."""
    import src.app_config as app_config

    backend = FakeBackend()
    monkeypatch.setattr(app_config, "get_save_check_config",
                        lambda: {"model": {"backend": "llama-server", "url": "http://mini:8080"}})
    monkeypatch.setattr(scm, "backend_for", lambda config: backend if config.backend else None)
    monkeypatch.setattr(scm, "jobs", scm.Jobs())
    return backend


def _verdict(client, body):
    return client.get(f"/api/save-check/job/{body['check_job']}").get_json()


def _log(project, name="save_checks.jsonl"):
    path = project / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


SLIP = ("El gato se sentó.", "El gato se sentaron.", "The cat sat.")


def test_no_test_reads_the_developer_s_save_check_section():
    # tests/conftest.py: a ``save_check.model`` there would send every test
    # Save in the suite to a real server.
    import src.app_config as app_config
    import web_ui.app as app_module

    assert app_config.get_save_check_config() == {}
    assert app_module._save_check_model()[0] is None


class TestBehindASave:
    def test_off_by_default(self, client, project):
        body = _save(client, 0, *SLIP).get_json()
        assert body["saved"] is True and "check_job" not in body

    def test_a_slip_the_rules_cannot_see_gets_a_warning(self, client, project, model):
        body = _save(client, 0, *SLIP).get_json()
        assert body["saved"] is True and "check" not in body
        assert _verdict(client, body) == {"done": True, "flagged": True}

        (warning,) = _log(project)
        assert warning["hits"] == [{"rule": "model", "text": "sentaron"}]
        assert warning["model"]["score"] == 9.1 and warning["model"]["model"] == "gemma-4-31b"
        assert _open(client) == [{"id": warning["id"], "es_idx": 0, "hits": warning["hits"]}]
        (readout,) = _log(project, scm.READOUT_LOG_NAME)
        assert readout["flagged"] is True and readout["es_after"] == SLIP[1] and readout["threshold"] == 7.5

    def test_a_clean_save_is_scored_and_not_warned(self, client, project, model):
        model.flagged = False
        body = _save(client, 0, "El gato se sentó.", "El gato se acostó.", "The cat sat.").get_json()
        assert _verdict(client, body) == {"done": True, "flagged": False}
        assert _log(project) == [] and _open(client) == []
        assert [r["flagged"] for r in _log(project, scm.READOUT_LOG_NAME)] == [False]

    def test_an_edit_of_punctuation_alone_is_not_sent(self, client, project, model):
        body = _save(client, 0, "El gato se sentó.", "El gato se sentó", "The cat sat.").get_json()
        assert body["check"]["hits"] == [{"rule": "closing_mark_dropped", "text": "."}]
        assert "check_job" not in body and model.judged == []

    def test_one_warning_carries_the_rule_hit_and_the_model_hit(self, client, project, model):
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentaron.", "The cat sat.").get_json()
        assert body["check"]["hits"] == [{"rule": "spelling", "text": "gatto"}]
        _verdict(client, body)
        (shown,) = _open(client)
        assert shown["id"] != body["check"]["id"]
        assert shown["hits"] == [{"rule": "spelling", "text": "gatto"},
                                 {"rule": "model", "text": "gatto … sentaron"}]
        assert _log(project)[-1]["carried_from"] == body["check"]["id"]

    def test_a_rule_warning_dismissed_before_the_verdict_is_not_raised_again(
            self, client, project, model, monkeypatch):
        # Held until the dismissal is in the log, as a slow model would hold it.
        held = []
        monkeypatch.setattr(scm.jobs, "submit", lambda work: held.append(work) or "held")
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentaron.", "The cat sat.").get_json()
        client.post("/api/save-check/dismiss",
                    json={"project_id": "test-project", "id": body["check"]["id"]})
        held[0]()
        (shown,) = _open(client)
        assert shown["hits"] == [{"rule": "model", "text": "gatto … sentaron"}]

    def test_a_verdict_on_a_sentence_saved_again_since_is_dropped(self, client, project, model, monkeypatch):
        held = []
        monkeypatch.setattr(scm.jobs, "submit", lambda work: held.append(work) or "held")
        _save(client, 0, *SLIP)
        _save(client, 0, "El gato se sentaron.", "El gato se sentó.", "The cat sat.")
        assert held[0]() is None
        assert _log(project) == []

    def test_the_model_can_be_switched_off_like_a_rule(self, client, project, model, monkeypatch):
        body = _save(client, 0, *SLIP).get_json()
        _verdict(client, body)
        assert len(_open(client)) == 1

        import src.app_config as app_config
        monkeypatch.setattr(app_config, "get_save_check_config", lambda: {
            "disabled_rules": ["model"], "model": {"backend": "llama-server", "url": "http://mini:8080"}})
        assert _open(client) == []
        body = _save(client, 1, "El perro ladró.", "El perro ladraron.", "The dog barked.").get_json()
        assert "check_job" not in body

    def test_a_server_that_cannot_answer_costs_nothing(self, client, project, model):
        model.available = False
        body = _save(client, 0, *SLIP).get_json()
        assert body["saved"] is True
        assert _verdict(client, body) == {"done": True, "flagged": False}
        assert _log(project) == [] and _log(project, scm.READOUT_LOG_NAME) == []

    def test_a_backend_that_raises_costs_nothing(self, client, project, model, monkeypatch):
        monkeypatch.setattr(model, "judge", lambda *a: 1 / 0)
        body = _save(client, 0, *SLIP).get_json()
        assert body["saved"] is True
        assert _verdict(client, body) == {"done": True, "flagged": False}

    def test_fixing_the_sentence_closes_the_warning(self, client, project, model):
        _verdict(client, _save(client, 0, *SLIP).get_json())
        model.flagged = False
        _verdict(client, _save(client, 0, "El gato se sentaron.", "El gato se sentó.", "The cat sat.").get_json())
        assert _open(client) == []


class TestBehindAReplace:
    def test_the_retranslate_replace_is_asked_about_too(self, client, project, model, monkeypatch):
        import web_ui.app as app_module
        from src.models import Chunk, ChunkMetadata, ChunkStatus
        from src.utils.file_io import save_chunk
        from tests.test_save_check import _write_alignment

        source = "The cat sat. The dog barked."
        (project / "chunks").mkdir()
        save_chunk(Chunk(
            id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
            source_text=source, translated_text="El gato se sentó. El perro ladró.",
            metadata=ChunkMetadata(char_start=0, char_end=len(source), overlap_start=0,
                                   overlap_end=0, paragraph_count=1, word_count=6),
            status=ChunkStatus.TRANSLATED,
        ), project / "chunks" / "chapter_01_chunk_000.json")

        def fake_apply(project_dir, project_id, chapter_id, edits):
            # Stands in for save + recombine + realign, which here moves the
            # sentence down a row before the verdict is in.
            _write_alignment(project_dir, chapter_id, ["Nueva.", "El gato se sentaron.", "El perro ladró."])
            return {"mtimes": {}, "orphaned_annotations": [], "corrections_purged": 0}
        monkeypatch.setattr(app_module, "_apply_chunk_edits", fake_apply)

        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project", "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000", "es_idx": 0,
            "current_translation": "El gato se sentó.", "new_translation": "El gato se sentaron.",
        })
        body = rv.get_json()
        assert rv.status_code == 200, body
        assert body["check"] is None
        assert _verdict(client, body) == {"done": True, "flagged": True}
        assert model.judged == [("The cat sat.", "El gato se sentó.", "El gato se sentaron.")]
        (shown,) = _open(client)
        assert shown["es_idx"] == 1 and shown["hits"] == [{"rule": "model", "text": "sentaron"}]


class TestAWarnedSentenceEditedAgain:
    def _warned(self, client, model):
        _verdict(client, _save(client, 0, *SLIP).get_json())
        (shown,) = _open(client)
        return shown

    def test_an_edit_that_leaves_the_slip_is_flagged_again(self, client, project, model):
        self._warned(client, model)
        body = _save(client, 0, "El gato se sentaron.", "El gato gris se sentaron.", "The cat sat.").get_json()
        _verdict(client, body)
        (shown,) = _open(client)
        assert shown["hits"] == [{"rule": "model", "text": "gris"}]

    def test_a_punctuation_edit_of_it_is_still_sent(self, client, project, model):
        self._warned(client, model)
        body = _save(client, 0, "El gato se sentaron.", "¡El gato se sentaron!", "The cat sat.").get_json()
        assert "check_job" in body
        _verdict(client, body)
        assert len(model.judged) == 2 and len(_open(client)) == 1

    def test_the_hit_is_carried_when_the_model_cannot_be_asked(self, client, project, model):
        first = self._warned(client, model)
        client.get(f"/api/save-check/reason/test-project/{first['id']}")
        model.available = False
        body = _save(client, 0, "El gato se sentaron.", "El gato gris se sentaron.", "The cat sat.").get_json()
        assert _verdict(client, body) == {"done": True, "flagged": True}
        (shown,) = _open(client)
        assert shown["id"] != first["id"]
        assert shown["hits"] == [{"rule": "model", "text": "sentaron", "reason": model.reason}]

    def test_the_second_warning_continues_the_first(self, client, project, model):
        first = self._warned(client, model)
        _verdict(client, _save(client, 0, "El gato se sentaron.", "El gato gris se sentaron.",
                               "The cat sat.").get_json())
        assert _log(project)[-1]["carried_from"] == first["id"]

    def test_a_punctuation_edit_of_it_keeps_the_word_it_named(self, client, project, model):
        self._warned(client, model)
        _verdict(client, _save(client, 0, "El gato se sentaron.", "¡El gato se sentaron!",
                               "The cat sat.").get_json())
        (shown,) = _open(client)
        assert shown["hits"] == [{"rule": "model", "text": "sentaron"}]

    def test_a_fix_made_while_the_model_cannot_be_asked_is_not_warned_again(self, client, project, model):
        self._warned(client, model)
        model.available = False
        body = _save(client, 0, "El gato se sentaron.", "El gato se sentó.", "The cat sat.").get_json()
        assert _verdict(client, body) == {"done": True, "flagged": False}
        assert _open(client) == []

    def test_a_hit_that_names_a_word_taken_out_is_carried_until_the_word_is_back(self, client, project, model):
        _verdict(client, _save(client, 0, "El gato se sentó.", "El gato sentó.", "The cat sat.").get_json())
        assert _open(client)[0]["hits"] == [{"rule": "model", "text": "se"}]
        model.available = False
        body = _save(client, 0, "El gato sentó.", "El gato gris sentó.", "The cat sat.").get_json()
        assert _verdict(client, body) == {"done": True, "flagged": True}
        assert _open(client)[0]["hits"] == [{"rule": "model", "text": "se"}]
        body = _save(client, 0, "El gato gris sentó.", "El gato gris se sentó.", "The cat sat.").get_json()
        assert _verdict(client, body) == {"done": True, "flagged": False}
        assert _open(client) == []


class TestReasonRoute:
    def test_the_reason_is_asked_for_once_and_kept(self, client, project, model):
        _verdict(client, _save(client, 0, *SLIP).get_json())
        (shown,) = _open(client)
        url = f"/api/save-check/reason/test-project/{shown['id']}"
        assert client.get(url).get_json() == {"reason": model.reason}
        assert client.get(url).get_json() == {"reason": model.reason}
        assert model.explained == 1
        assert _open(client)[0]["hits"] == [{"rule": "model", "text": "sentaron", "reason": model.reason}]

    def test_no_answer_is_empty_and_asked_again_later(self, client, project, model):
        _verdict(client, _save(client, 0, *SLIP).get_json())
        url = f"/api/save-check/reason/test-project/{_open(client)[0]['id']}"
        model.available = False
        assert client.get(url).get_json() == {"reason": ""}
        model.available = True
        assert client.get(url).get_json() == {"reason": model.reason}

    def test_a_rule_warning_has_no_reason(self, client, project, model):
        model.flagged = False
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.").get_json()
        assert client.get(f"/api/save-check/reason/test-project/{body['check']['id']}").status_code == 404
        assert client.get("/api/save-check/reason/test-project/nope").status_code == 404
        assert model.explained == 0

    def test_an_empty_answer_is_not_kept(self, client, project, model):
        _verdict(client, _save(client, 0, *SLIP).get_json())
        url = f"/api/save-check/reason/test-project/{_open(client)[0]['id']}"
        model.reason = None
        assert client.get(url).get_json() == {"reason": ""}
        assert [r["kind"] for r in _log(project)] == ["warning"]

    def test_a_malformed_id_or_an_unknown_book_is_refused(self, client, project, model):
        assert client.get("/api/save-check/reason/test-project/a$b").status_code == 400
        assert client.get("/api/save-check/reason/a$b/w1").status_code == 400
        assert client.get("/api/save-check/reason/no-such-book/w1").status_code == 404
        assert model.explained == 0


class TestJobRoute:
    def test_a_job_this_process_does_not_know_is_done_with_nothing(self, client, project, model):
        assert client.get("/api/save-check/job/nope").get_json() == {"done": True, "flagged": False}

    def test_a_malformed_id_is_refused(self, client, project, model):
        assert client.get("/api/save-check/job/a$b").status_code == 400


class TestStrings:
    def test_the_reader_assets_are_cache_busted_past_the_build_without_the_model_row(self, client, project):
        html = client.get("/read/test-project/chapter_01").get_data(as_text=True)
        assert "reader.js?v=34" not in html and "reader.css?v=8" not in html

    def test_the_model_hit_has_a_line_in_both_languages(self):
        from web_ui.i18n import get_strings

        for lang in ("en", "es"):
            assert set(get_strings(lang)["js"]["save_check_rules"]) == set(save_check.RULES)
