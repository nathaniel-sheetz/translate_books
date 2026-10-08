"""The remote embedder and the server it talks to, end to end over loopback."""

import threading
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from scripts.embed_server import build_handler
from src import embed_client
from src.embed_client import RemoteEmbedder, remote_embedder

MODEL = "test-model"
KEY = "test-key"


class FakeModel:
    """Stands in for sentence-transformers on either end: one row per text,
    its first component the text's length, so order and count are checkable."""

    def __init__(self, marker: float):
        self.marker = marker
        self.calls = 0

    def encode(self, texts, normalize_embeddings=False, **kwargs):
        self.calls += 1
        return np.array([[len(t), self.marker] for t in texts], dtype=np.float32).reshape(-1, 2)


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


def test_failed_server_is_left_alone_until_the_retry_time(server, monkeypatch):
    url, remote_model = server
    embedder, local = _embedder(url, key="wrong-key")
    now = [1000.0]
    monkeypatch.setattr(embed_client.time, "monotonic", lambda: now[0])
    posts = []
    real_remote = embedder._encode_remote
    monkeypatch.setattr(
        embedder, "_encode_remote", lambda *a: (posts.append(1), real_remote(*a))[1]
    )

    embedder.encode(["a"])
    embedder.encode(["a"])
    assert len(posts) == 1
    assert local.calls == 2

    embedder.key = KEY
    now[0] += embed_client.RETRY_AFTER_S
    assert embedder.encode(["a"]).tolist() == [[1.0, 1.0]]
    assert len(posts) == 2
    assert remote_model.calls == 1


def test_unreachable_server_falls_back(monkeypatch):
    monkeypatch.setattr(embed_client, "CONNECT_TIMEOUT_S", 0.5)
    # Port 9 (discard) on loopback: nothing listens there.
    embedder, local = _embedder("http://127.0.0.1:9")

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


def test_embedder_from_config(monkeypatch):
    monkeypatch.setattr(embed_client, "get_alignment_config", lambda: {"embed_url": "http://x:1/ "})
    monkeypatch.setenv(embed_client.KEY_ENV, KEY)

    embedder = remote_embedder(MODEL, lambda: None)

    assert (embedder.url, embedder.key, embedder.model_name) == ("http://x:1", KEY, MODEL)
