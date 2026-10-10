"""
Three ways to reach a model, behind one call.

``llm_config.json`` lists providers of three kinds, told apart by ``type``:

- **api** (``anthropic``, ``openai-compatible``): a metered request through
  :func:`src.api_translator.call_llm`.
- **headless** (``type: "headless"``, ``cli: "claude" | "cursor"``): one
  subscription CLI process through :func:`src.harness.headless.run_headless_wave`,
  which keeps the credential scrub and the fail-closed login preflight.
- **local** (``type: "local"``): the LAN ``llama-server`` through
  :mod:`src.local_llm`, and only with a model it already has loaded.

A screen that wants to offer all three calls :func:`complete` where it would
have called ``call_llm`` and fills its picker from :func:`list_models`.
``call_llm`` itself stays API-only, so a screen that has not been wired here
cannot be handed a provider it does not know how to wait for.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from src import local_llm
from src.api_translator import (
    call_llm,
    get_default_model,
    get_model_pricing,
    get_provider_config,
    load_llm_config,
)
from src.harness.headless import cli_binary_present, run_headless_wave
from src.utils.prompt_logger import log_prompt

logger = logging.getLogger(__name__)

API = "api"
HEADLESS = "headless"
LOCAL = "local"
MECHANISMS = (API, HEADLESS, LOCAL)

# Token estimator used across the codebase: ~4 chars/token.
_CHARS_PER_TOKEN = 4

# Why a picker row cannot be chosen right now.
NO_KEY = "no_key"
CLI_MISSING = "cli_missing"
SERVER_DOWN = "server_down"
NOT_LOADED = "not_loaded"


class MechanismError(Exception):
    """A headless or local call ran and failed."""


class MechanismUnavailable(MechanismError):
    """The mechanism cannot be asked now: the server is down or the model is not loaded."""


@dataclass(frozen=True)
class Completion:
    """One answer. Token counts are the mechanism's own where it reports them, else estimates."""

    text: str
    mechanism: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def mechanism_of(pconfig: Mapping) -> str:
    """Which of :data:`MECHANISMS` a provider entry is reached by."""
    ptype = pconfig.get("type")
    return ptype if ptype in (HEADLESS, LOCAL) else API


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def _model_entry(pconfig: Mapping, model: str) -> dict:
    """The catalog entry for ``model``; a bare ``{"id": model}`` when it is not listed."""
    for entry in pconfig.get("models", []):
        if entry.get("id") == model:
            return entry
    return {"id": model}


def check_request_model(provider: str, model: Optional[str]) -> None:
    """Refuse a model that a headless or local ``provider`` does not list.

    For a model id that arrives in an HTTP request, where it would become CLI
    argv. A caller in this process may still hand :func:`complete` an id the
    catalog has not caught up with, and an API provider runs one as before.
    """
    pconfig = get_provider_config(provider)
    if not model or mechanism_of(pconfig) == API:
        return
    if not any(entry.get("id") == model for entry in pconfig.get("models", [])):
        raise ValueError(
            f"model '{model}' is not listed under provider '{provider}' in llm_config.json"
        )


def complete(
    prompt: str,
    *,
    provider: str,
    model: Optional[str] = None,
    call_type: str = "unknown",
    usage_log: Path | str | None = None,
    project_slug: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    max_retries: int = 3,
    headless_timeout_s: Optional[float] = None,
) -> Completion:
    """Send ``prompt`` to ``model`` by whichever mechanism ``provider`` is.

    ``usage_log`` is where a headless call appends its usage row (the
    ``.harness/<wave>/usage.jsonl`` convention). ``headless_timeout_s`` replaces
    the launcher's per-CLI ceiling, which is sized for a chunk of prose.
    ``temperature`` and ``max_retries`` apply where the mechanism has them: a
    CLI takes neither.

    Raises :class:`MechanismUnavailable` when a local model cannot be asked,
    :class:`MechanismError` when a headless or local call fails, and whatever
    ``call_llm`` raises on the API path.
    """
    pconfig = get_provider_config(provider)
    mechanism = mechanism_of(pconfig)

    if mechanism == API:
        if model is None:
            model = get_default_model()
        text = call_llm(
            prompt,
            provider=provider,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            call_type=call_type,
            project_slug=project_slug,
        ) or ""
        prompt_tokens, completion_tokens = _estimate_tokens(prompt), _estimate_tokens(text)
        pricing = get_model_pricing(provider, model)
        cost = (
            (prompt_tokens / 1_000_000) * pricing.get("input", 0.0)
            + (completion_tokens / 1_000_000) * pricing.get("output", 0.0)
        )
        return Completion(text, API, provider, model, prompt_tokens, completion_tokens, round(cost, 6))

    if not model:
        raise ValueError(f"a model is required for the {mechanism} provider '{provider}'")
    entry = _model_entry(pconfig, model)
    started = time.time()
    if mechanism == HEADLESS:
        text, prompt_tokens, completion_tokens, extra = _complete_headless(
            prompt, pconfig, entry,
            call_type=call_type, usage_log=usage_log, timeout_s=headless_timeout_s,
        )
    else:
        try:
            reply = local_llm.complete(
                pconfig, entry, prompt, temperature=temperature, max_tokens=max_tokens,
            )
        except (local_llm.ModelNotLoadedError, local_llm.ServerUnreachableError) as e:
            raise MechanismUnavailable(str(e)) from e
        except local_llm.LocalLLMError as e:
            raise MechanismError(str(e)) from e
        text, model = reply.text, reply.model
        prompt_tokens, completion_tokens, extra = reply.prompt_tokens, reply.completion_tokens, None

    log_prompt(
        prompt=prompt,
        response=text,
        provider=provider,
        model=model,
        call_type=call_type,
        mode=mechanism,
        temperature=temperature,
        max_tokens=max_tokens,
        duration_seconds=time.time() - started,
        project_slug=project_slug,
        extra=extra,
    )
    return Completion(
        text=text,
        mechanism=mechanism,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens if prompt_tokens else _estimate_tokens(prompt),
        completion_tokens=completion_tokens if completion_tokens else _estimate_tokens(text),
        cost_usd=0.0,
    )


def _complete_headless(
    prompt: str,
    pconfig: Mapping,
    entry: Mapping,
    *,
    call_type: str,
    usage_log: Path | str | None,
    timeout_s: Optional[float],
) -> tuple[str, Optional[int], Optional[int], dict]:
    """One CLI process for one prompt: ``(text, billed input, output, log extra)``."""
    cli = str(pconfig.get("cli") or "").strip().lower()
    if not cli:
        # The runner reads an empty family as Claude; the picker calls it missing.
        raise ValueError(f"headless provider {pconfig.get('id')!r} names no 'cli' in llm_config.json")
    model = str(entry["id"])
    effort = entry.get("effort")
    # ``--effort`` is Claude argv. A Cursor model carries its effort in the id
    # (``grok-4.7-medium``), so there the catalog id is the whole instruction.
    flags = ["--effort", str(effort)] if effort and cli == "claude" else []
    with tempfile.TemporaryDirectory(prefix="llm-headless-") as tmp:
        out = Path(tmp) / "reply.txt"
        result = run_headless_wave(
            [{
                "id": f"{call_type}-{uuid.uuid4().hex[:8]}",
                "input_text": prompt,
                "output_path": str(out),
            }],
            model=model,
            concurrency=1,
            cli=cli,
            usage_log=usage_log,
            extra_flags=flags,
            effort=str(effort) if effort else None,
            warm_first=False,
            # One job has nothing to read a cache entry back, and a write costs more.
            cache="off",
            job_timeout=timeout_s,
        )
        failed = result.get("failed") or []
        if failed:
            raise MechanismError(str(failed[0].get("error") or "headless job failed"))
        if not result.get("wrote"):
            raise MechanismError(str(result.get("error") or "headless job did not run"))
        text = out.read_text(encoding="utf-8").strip()
    usage = result.get("usage") or {}
    billed = sum(int(usage.get(k) or 0) for k in ("input", "cache_creation", "cache_read"))
    return text, billed or None, int(usage.get("output") or 0) or None, {"cli": cli, "effort": effort}


def list_models(mechanisms: Iterable[str] = MECHANISMS) -> list[dict]:
    """Picker rows for every catalog model reached by one of ``mechanisms``.

    Each row says whether it can be chosen now and, if not, why
    (``unavailable_reason``): an API provider needs its key, a headless one its
    CLI on PATH (the login is checked when it is called), and a local model has
    to be the one the server has loaded.
    """
    wanted = set(mechanisms)
    config = load_llm_config()
    default_model = config.get("default_model")
    rows: list[dict] = []
    for provider in config.get("providers", []):
        mechanism = mechanism_of(provider)
        if mechanism not in wanted:
            continue
        provider_reason, loaded = _provider_state(provider, mechanism)
        for entry in provider.get("models", []):
            reason = provider_reason
            if reason is None and mechanism == LOCAL and local_llm.served_id(entry, loaded) is None:
                reason = NOT_LOADED
            rows.append({
                "id": entry["id"],
                "name": entry.get("name", entry["id"]),
                "provider": provider["id"],
                "mechanism": mechanism,
                "pricing": entry.get("pricing", {}),
                "is_default": mechanism == API and entry["id"] == default_model,
                "available": reason is None,
                "unavailable_reason": reason,
            })
    return rows


def _provider_state(provider: Mapping, mechanism: str) -> tuple[Optional[str], list[str]]:
    """``(why nothing on this provider can be chosen, loaded local ids)``."""
    if mechanism == API:
        env_var = provider.get("api_key_env_var")
        return (None if env_var and os.getenv(env_var) else NO_KEY), []
    if mechanism == HEADLESS:
        return (None if cli_binary_present(str(provider.get("cli") or "")) else CLI_MISSING), []
    if not os.getenv(provider.get("api_key_env_var") or local_llm.KEY_ENV):
        return NO_KEY, []
    try:
        return None, local_llm.loaded_models(provider)
    except local_llm.LocalLLMError as e:
        logger.info("Local provider %s cannot be listed: %s", provider.get("id"), e)
        return SERVER_DOWN, []
