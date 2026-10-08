"""
Client for ``scripts/embed_server.py``: the aligner's encoder on another machine.

:class:`RemoteEmbedder` stands in for the sentence-transformers model wherever
the aligner calls ``model.encode(texts, normalize_embeddings=True)``. While the
server answers, this process never imports torch. When it does not — off the
LAN, server stopped, wrong model loaded — the call falls back to the local
model and the server is left alone for ``RETRY_AFTER_S`` before it is tried
again, so an alignment is slower but never fails for want of the server.

Configured by ``alignment.embed_url`` in ``app_config.json`` and the bearer key
in the ``ALIGN_EMBED_KEY`` environment variable (``.env``).
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Callable, Optional

import numpy as np

from src.app_config import get_alignment_config

logger = logging.getLogger(__name__)

KEY_ENV = "ALIGN_EMBED_KEY"
# An unreachable server must cost little before the local model takes over;
# a large chunk on a busy server may still take a while to answer.
CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 120
RETRY_AFTER_S = 60


class RemoteEmbedder:
    """Encode on the embedding server, or locally when it cannot be used."""

    def __init__(self, url: str, key: str, model_name: str, load_local: Callable[[], object]):
        self.url = url.rstrip("/")
        self.key = key
        self.model_name = model_name
        self._load_local = load_local
        self._local = None
        self._session = None
        self._skip_until = 0.0

    def encode(self, texts, normalize_embeddings: bool = False, **kwargs) -> np.ndarray:
        texts = list(texts)
        if time.monotonic() >= self._skip_until:
            try:
                return self._encode_remote(texts, normalize_embeddings)
            except Exception as e:
                self._skip_until = time.monotonic() + RETRY_AFTER_S
                logger.warning(
                    "Embedding server %s unusable (%s); encoding locally for %ds",
                    self.url, e, RETRY_AFTER_S,
                )
        if self._local is None:
            self._local = self._load_local()
        return self._local.encode(texts, normalize_embeddings=normalize_embeddings, **kwargs)

    def _encode_remote(self, texts: list[str], normalize: bool) -> np.ndarray:
        import requests

        if self._session is None:
            self._session = requests.Session()
        resp = self._session.post(
            self.url + "/embed",
            json={"model": self.model_name, "texts": texts, "normalize": normalize},
            headers={"Authorization": "Bearer " + self.key},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        vectors = np.load(io.BytesIO(resp.content), allow_pickle=False)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise RuntimeError(f"expected {len(texts)} vectors, got shape {vectors.shape}")
        return vectors


def remote_embedder(model_name: str, load_local: Callable[[], object]) -> Optional[RemoteEmbedder]:
    """Return the configured :class:`RemoteEmbedder`, or ``None`` when none is set up."""
    url = get_alignment_config().get("embed_url")
    if not isinstance(url, str) or not url.strip():
        return None
    key = os.environ.get(KEY_ENV)
    if not key:
        from dotenv import load_dotenv

        load_dotenv()
        key = os.environ.get(KEY_ENV)
    if not key:
        logger.warning("alignment.embed_url is set but %s is not; encoding locally", KEY_ENV)
        return None
    return RemoteEmbedder(url.strip(), key, model_name, load_local)
