"""
Generation on a local ``llama-server``, with whichever model it already has loaded.

A ``local`` provider in ``llm_config.json`` names the server and the models it
may be asked for. This client never starts, stops or swaps a model: it asks the
server what is in memory and sends a prompt only when the model that was asked
for is there. A model that is not loaded is refused before anything is sent,
because a router-mode server would load it and push out the one that is, and a
server with one model answers to any name.

:func:`loaded_ids` is the one rule for what counts as loaded; the save check
(``src/save_check_model.py``) reads the same listing through it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

KEY_ENV = "LOCAL_LLM_KEY"

# An unreachable server is found out in the time it takes to notice a pause.
CONNECT_TIMEOUT_S = 2
LIST_TIMEOUT_S = 5
# Short, for the reason the save check gives: a model swapped in behind a stale
# listing would be asked under the old model's name.
LOADED_TTL_S = 5
DEFAULT_TIMEOUT_S = 120.0


class LocalLLMError(Exception):
    """The local server was asked and the call failed."""


class ServerUnreachableError(LocalLLMError):
    """The server did not answer, or no key is set to ask it with."""


class ModelNotLoadedError(LocalLLMError):
    """The model asked for is not the one the server has in memory."""


@dataclass(frozen=True)
class LocalReply:
    text: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


def loaded_ids(payload: object) -> list[str]:
    """Ids of the models in memory, from a ``/v1/models`` reply."""
    entries = payload.get("data") if isinstance(payload, Mapping) else None
    ids = []
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        # A router-mode server lists every model it could load. Naming one
        # that is not in memory would load it and push out the one that is.
        status = entry.get("status")
        state = status.get("value") if isinstance(status, Mapping) else status
        if state is not None and state != "loaded":
            continue
        if isinstance(entry.get("id"), str):
            ids.append(entry["id"])
    return ids


def served_id(model: Mapping, loaded: list[str]) -> Optional[str]:
    """The loaded id that serves catalog ``model``, matched on its id or an alias.

    Whole names, without regard to case. The id returned is the server's own
    spelling, which is the one a request has to carry.
    """
    aliases = model.get("aliases") or ()
    if isinstance(aliases, str):
        aliases = (aliases,)
    names = {str(n).lower() for n in (model.get("id"), *aliases) if n}
    return next((m for m in loaded if m.lower() in names), None)


def _default_session():
    import requests

    return requests.Session()


def _base_url(provider: Mapping) -> str:
    url = str(provider.get("base_url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ServerUnreachableError(
            f"provider {provider.get('id')!r} has no http(s) base_url in llm_config.json"
        )
    return url


def _key(provider: Mapping) -> str:
    name = provider.get("api_key_env_var") or KEY_ENV
    key = os.environ.get(name)
    if not key:
        from dotenv import load_dotenv

        load_dotenv()
        key = os.environ.get(name)
    if not key:
        raise ServerUnreachableError(f"{name} is not set")
    return key


_loaded_cache: dict[str, tuple[float, list[str]]] = {}
_loaded_lock = threading.Lock()


def loaded_models(provider: Mapping, *, session=None, fresh: bool = False) -> list[str]:
    """Ids the provider's server has in memory, asked at most every ``LOADED_TTL_S``."""
    url = _base_url(provider)
    if not fresh:
        with _loaded_lock:
            cached = _loaded_cache.get(url)
        if cached is not None and time.monotonic() - cached[0] < LOADED_TTL_S:
            return cached[1]
    http = session if session is not None else _default_session()
    try:
        resp = http.get(
            url + "/models",
            headers={"Authorization": "Bearer " + _key(provider)},
            timeout=(CONNECT_TIMEOUT_S, LIST_TIMEOUT_S),
        )
    except LocalLLMError:
        raise
    except Exception as e:
        raise ServerUnreachableError(f"local server {url} did not answer ({type(e).__name__})") from e
    if resp.status_code != 200:
        raise ServerUnreachableError(f"local server {url} answered HTTP {resp.status_code}")
    try:
        ids = loaded_ids(resp.json())
    except ValueError as e:
        raise ServerUnreachableError(f"{url} answered, but not as a model server ({e})") from e
    with _loaded_lock:
        _loaded_cache[url] = (time.monotonic(), ids)
    return ids


def complete(
    provider: Mapping,
    model: Mapping,
    prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout_s: Optional[float] = None,
    session=None,
) -> LocalReply:
    """Send ``prompt`` to catalog ``model`` if the server has it loaded.

    Raises :class:`ModelNotLoadedError` without sending anything when it does
    not, :class:`ServerUnreachableError` when the server cannot be asked, and
    :class:`LocalLLMError` when it answers with a failure, drops the connection
    once the prompt is on its way, or returns an answer that is cut off or is
    all reasoning.
    """
    url = _base_url(provider)
    http = session if session is not None else _default_session()
    # Asked afresh: this is the check that decides whether a prompt is sent.
    loaded = loaded_models(provider, session=http, fresh=True)
    target = served_id(model, loaded)
    if target is None:
        have = ", ".join(loaded) if loaded else "nothing"
        raise ModelNotLoadedError(
            f"{model.get('id')} is not loaded on the local server (loaded: {have})"
        )
    read_timeout = float(timeout_s or provider.get("timeout_seconds") or DEFAULT_TIMEOUT_S)
    try:
        resp = http.post(
            url + "/chat/completions",
            json={
                "model": target,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "cache_prompt": True,
                # Thinking is switched off in the chat template itself; left
                # on, the reasoning is paid for out of max_tokens.
                "chat_template_kwargs": {"enable_thinking": False},
            },
            headers={"Authorization": "Bearer " + _key(provider)},
            timeout=(CONNECT_TIMEOUT_S, read_timeout),
        )
    except Exception as e:
        if type(e).__name__ in ("ReadTimeout", "Timeout"):
            raise LocalLLMError(
                f"{target} did not answer within {read_timeout:.0f}s. Raise "
                f"'timeout_seconds' for the provider in llm_config.json."
            ) from e
        if type(e).__name__ == "ConnectTimeout":
            raise ServerUnreachableError(f"local server {url} did not answer ({type(e).__name__})") from e
        # The server answered the loaded check a moment ago, so this is a
        # connection lost with the prompt already on its way.
        raise LocalLLMError(
            f"local server {url} dropped the connection ({type(e).__name__}); "
            f"the prompt may have reached {target}"
        ) from e
    if resp.status_code != 200:
        raise LocalLLMError(f"local server answered HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoned = bool(message.get("reasoning_content"))
        cut_off = choice.get("finish_reason") == "length"
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise LocalLLMError(f"local server reply could not be read ({e})") from e
    # Raised, not returned: a caller that retries an empty answer would wait
    # out the same result twice, and a cut-off one reads as a whole answer.
    if not text.strip() and reasoned:
        raise LocalLLMError(
            f"{target} reasoned and wrote no answer (max_tokens={max_tokens}); "
            f"its chat template did not switch thinking off"
        )
    if cut_off:
        raise LocalLLMError(f"{target} was cut off at max_tokens={max_tokens}; the answer is incomplete")
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    return LocalReply(
        text=text,
        model=target,
        prompt_tokens=_int(usage.get("prompt_tokens")),
        completion_tokens=_int(usage.get("completion_tokens")),
    )


def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
