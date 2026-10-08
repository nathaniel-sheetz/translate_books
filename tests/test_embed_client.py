"""The remote embedder and the server it talks to, end to end over loopback."""

import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import embed_server
from scripts.embed_server import build_handler
from src import embed_client
from src.embed_client import RemoteEmbedder, remote_embedder

MODEL = "test-model"
KEY = "test-key"


class FakeModel:
    """Stands in for sentence-transformers on either end: one row per text,
    its first component the text's length, so order and count are checkable.
    No texts give shape (0,), as the real model does."""

    def __init__(self, marker: float):
        self.marker = marker
        self.calls = 0
        self.kwargs = None
        self.error = None

    def encode(self, texts, normalize_embeddings=False, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return np.array([[len(t), self.marker] for t in texts], dtype=np.float32)


@pytest.fixture
def server():
    model = FakeModel(marker=1.0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(model, MODEL, "cpu", KEY, 32))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", model
    httpd.shutdown()
    httpd.server_close()


def _embedder(url, key=KEY, model_name=MODEL):
    local = FakeModel(marker=2.0)
    return RemoteEmbedder(url, key, model_name, lambda: local), local


@pytest.fixture
def clock(monkeypatch):
    """The client's clock, held still until a test moves it. Only the client's:
    the server thread and requests keep the real one."""
    now = [1000.0]
    monkeypatch.setattr(embed_client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _count_posts(embedder, monkeypatch):
    posts = []
    real_remote = embedder._encode_remote
    monkeypatch.setattr(
        embedder, "_encode_remote", lambda *a: (posts.append(1), real_remote(*a))[1]
    )
    return posts


def _post_with_headers(url, content_length, body=None, key=KEY):
    """POST /embed with the Content-Length as given, whatever the body holds."""
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.putrequest("POST", "/embed")
        conn.putheader("Authorization", "Bearer " + key)
        conn.putheader("Content-Length", str(content_length))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read()), resp.headers
    finally:
        conn.close()


def _post(url, content_length, body=None, key=KEY):
    return _post_with_headers(url, content_length, body, key)[:2]


def test_encodes_on_the_server(server):
    url, remote_model = server
    embedder, local = _embedder(url)

    vectors = embedder.encode(["a", "bbb", "cc"], normalize_embeddings=True)

    assert vectors.dtype == np.float32
    assert vectors.tolist() == [[1.0, 1.0], [3.0, 1.0], [2.0, 1.0]]
    assert remote_model.calls == 1
    assert local.calls == 0


@pytest.mark.parametrize(
    "key, model_name",
    [("wrong-key", MODEL), (KEY, "another-model")],
    ids=["bad key", "server holds a different model"],
)
def test_refusal_falls_back_to_the_local_model(server, key, model_name):
    url, remote_model = server
    embedder, local = _embedder(url, key=key, model_name=model_name)

    vectors = embedder.encode(["a", "bbb"], normalize_embeddings=True)

    assert vectors.tolist() == [[1.0, 2.0], [3.0, 2.0]]
    assert remote_model.calls == 0


def test_failed_server_is_left_alone_until_the_retry_time(server, monkeypatch, clock):
    url, remote_model = server
    embedder, local = _embedder(url, key="wrong-key")
    posts = _count_posts(embedder, monkeypatch)

    embedder.encode(["a"])
    embedder.encode(["a"])
    assert len(posts) == 1
    assert local.calls == 2

    embedder.key = KEY
    clock[0] += embed_client.RETRY_AFTER_S
    assert embedder.encode(["a"]).tolist() == [[1.0, 1.0]]
    assert len(posts) == 2
    assert remote_model.calls == 1


def test_wait_doubles_while_the_server_keeps_failing(server, monkeypatch, clock):
    url, remote_model = server
    embedder, local = _embedder(url, key="wrong-key")
    posts = _count_posts(embedder, monkeypatch)

    waits = []
    for _ in range(6):
        embedder.encode(["a"])
        waits.append(embedder._skip_until - clock[0])
        clock[0] = embedder._skip_until
    assert waits == [60, 120, 240, 480, 600, 600]
    assert len(posts) == 6

    # One answer from the server and the next failure waits the short time again.
    embedder.key = KEY
    assert embedder.encode(["a"]).tolist() == [[1.0, 1.0]]
    embedder.key = "wrong-key"
    embedder.encode(["a"])
    assert embedder._skip_until - clock[0] == 60


def test_failed_local_load_does_not_keep_the_server_waiting(server, clock):
    url, remote_model = server

    def no_local_model():
        raise ImportError("no sentence-transformers here")

    embedder = RemoteEmbedder(url, "wrong-key", MODEL, no_local_model)

    with pytest.raises(ImportError):
        embedder.encode(["a"])

    # The clock has not moved, and the server is asked again all the same.
    embedder.key = KEY
    assert embedder.encode(["a"]).tolist() == [[1.0, 1.0]]


def test_extra_arguments_are_encoded_locally(server):
    url, remote_model = server
    embedder, local = _embedder(url)

    vectors = embedder.encode(["a"], normalize_embeddings=True, batch_size=8)

    assert vectors.tolist() == [[1.0, 2.0]]
    assert local.kwargs == {"batch_size": 8}
    assert remote_model.calls == 0
    # Not a failure of the server's: the next plain call goes to it.
    assert embedder.encode(["a"]).tolist() == [[1.0, 1.0]]


def test_one_string_gives_one_vector(server):
    url, remote_model = server
    embedder, local = _embedder(url)

    assert embedder.encode("abc").tolist() == [3.0, 1.0]
    assert remote_model.calls == 1


def test_empty_batch_is_the_servers_to_answer(server):
    url, remote_model = server
    embedder, local = _embedder(url)

    assert embedder.encode([]).shape == (0,)
    assert remote_model.calls == 1
    assert local.calls == 0


def test_server_answers_500_when_the_encode_raises(server):
    url, remote_model = server
    embedder, local = _embedder(url)
    remote_model.error = RuntimeError("MPS backend out of memory")

    assert embedder.encode(["a"]).tolist() == [[1.0, 2.0]]
    raw = json.dumps({"texts": ["a"]}).encode("utf-8")
    assert _post(url, len(raw), raw) == (500, {"error": "encode failed"})

    remote_model.error = None
    assert _embedder(url)[0].encode(["a"]).tolist() == [[1.0, 1.0]]


def test_wrong_key_is_refused_before_the_body_is_read(server):
    url, remote_model = server

    # A thousand bytes announced and none sent: a server that read the body
    # first would wait for them and this request would time out.
    assert _post(url, 1000, key="wrong-key") == (401, {"error": "unauthorized"})


@pytest.mark.parametrize("content_length", ["-1", "abc"])
def test_malformed_content_length_is_refused(server, content_length):
    url, remote_model = server

    assert _post(url, content_length) == (400, {"error": "bad Content-Length"})


def test_body_over_the_cap_is_refused(server, monkeypatch):
    url, remote_model = server
    monkeypatch.setattr(embed_server, "MAX_BODY_BYTES", 10)

    assert _post(url, 11) == (413, {"error": "body too large"})


@pytest.mark.parametrize(
    "content_length, key, status",
    [(1000, "wrong-key", 401), ("abc", KEY, 400), (embed_server.MAX_BODY_BYTES + 1, KEY, 413)],
    ids=["wrong key", "bad Content-Length", "body over the cap"],
)
def test_refusal_that_leaves_the_body_unread_announces_the_close(
    server, content_length, key, status
):
    url, remote_model = server

    # Without the header a client keeps the connection and sends its next
    # request into the server's close of it.
    got, _, headers = _post_with_headers(url, content_length, key=key)

    assert got == status
    assert headers["Connection"] == "close"


def test_refusal_with_the_body_read_keeps_the_connection(server):
    url, remote_model = server
    raw = json.dumps({"texts": "a"}).encode("utf-8")

    status, _, headers = _post_with_headers(url, len(raw), raw)

    assert status == 400
    assert headers["Connection"] is None


@pytest.mark.parametrize(
    "body",
    [["a"], {"texts": ["a", 1]}, {"texts": ["a"], "normalize": "false"}],
    ids=["not an object", "a text that is not a string", "normalize that is not a boolean"],
)
def test_bad_request_body_is_refused(server, body):
    url, remote_model = server
    raw = json.dumps(body).encode("utf-8")

    status, _ = _post(url, len(raw), raw)

    assert status == 400
    assert remote_model.calls == 0


def test_unreachable_server_falls_back(monkeypatch):
    monkeypatch.setattr(embed_client, "CONNECT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(embed_client, "READ_TIMEOUT_S", 0.5)
    # A port bound and released a moment ago: nothing listens there.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    embedder, local = _embedder(f"http://127.0.0.1:{port}")

    assert embedder.encode(["abcd"]).tolist() == [[4.0, 2.0]]


def test_no_embedder_without_a_url(monkeypatch):
    monkeypatch.setattr(embed_client, "get_alignment_config", lambda: {})
    monkeypatch.setenv(embed_client.KEY_ENV, KEY)

    assert remote_embedder(MODEL, lambda: None) is None


def test_no_embedder_without_a_key(monkeypatch):
    monkeypatch.setattr(embed_client, "get_alignment_config", lambda: {"embed_url": "http://x"})
    monkeypatch.setenv(embed_client.KEY_ENV, "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    assert remote_embedder(MODEL, lambda: None) is None


def test_no_embedder_for_a_url_without_a_scheme(monkeypatch):
    monkeypatch.setattr(
        embed_client, "get_alignment_config", lambda: {"embed_url": "192.168.1.22:8081"}
    )
    monkeypatch.setenv(embed_client.KEY_ENV, KEY)

    assert remote_embedder(MODEL, lambda: None) is None


def test_embedder_from_config(monkeypatch):
    monkeypatch.setattr(embed_client, "get_alignment_config", lambda: {"embed_url": "http://x:1/ "})
    monkeypatch.setenv(embed_client.KEY_ENV, KEY)

    embedder = remote_embedder(MODEL, lambda: None)

    assert (embedder.url, embedder.key, embedder.model_name) == ("http://x:1", KEY, MODEL)
