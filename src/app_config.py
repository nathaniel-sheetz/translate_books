"""
System-level application configuration loaded from ``app_config.json``.

This file holds cross-project settings that aren't LLM-specific (those
stay in ``llm_config.json``).  The config is cached after first load;
call ``load_app_config(force_reload=True)`` to re-read from disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_APP_CONFIG_CACHE: dict | None = None

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "app_config.json"

_FORCED_GLOSSARY_CACHE: list[dict] | None = None


def load_app_config(*, force_reload: bool = False) -> dict:
    """Load system config from ``app_config.json``, returning ``{}`` if absent."""
    global _APP_CONFIG_CACHE
    if _APP_CONFIG_CACHE is not None and not force_reload:
        return _APP_CONFIG_CACHE

    if _CONFIG_PATH.exists():
        try:
            _APP_CONFIG_CACHE = json.loads(
                _CONFIG_PATH.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning("Failed to parse app_config.json: %s", e)
            _APP_CONFIG_CACHE = {}
    else:
        _APP_CONFIG_CACHE = {}
    return _APP_CONFIG_CACHE


def get_enabled_evaluators() -> Optional[list[str]]:
    """Return the system-level evaluator whitelist, or ``None`` for all."""
    cfg = load_app_config()
    val = cfg.get("enabled_evaluators")
    if isinstance(val, list) and all(isinstance(v, str) for v in val):
        return val
    return None


def get_length_config() -> dict:
    """Return the ``length_config`` section from app_config, or ``{}``."""
    cfg = load_app_config()
    val = cfg.get("length_config")
    if isinstance(val, dict):
        return val
    return {}


def get_blacklist_path() -> Optional[Path]:
    """Return the resolved ``blacklist_path`` from app_config, or ``None``."""
    cfg = load_app_config()
    val = cfg.get("blacklist_path")
    if isinstance(val, str) and val.strip():
        return _CONFIG_PATH.parent / val
    return None


def get_judge_suites() -> dict[str, list[str]]:
    """Return user-defined judge suites from the ``judge_suites`` section.

    A suite is a named list of judge names runnable together via the judges
    CLI/runner. Returns ``{}`` when the section is absent or malformed; built-in
    suites are layered on top by ``src.judges.registry``.
    """
    cfg = load_app_config()
    val = cfg.get("judge_suites")
    if not isinstance(val, dict):
        return {}
    suites: dict[str, list[str]] = {}
    for name, members in val.items():
        if isinstance(members, list) and all(isinstance(m, str) for m in members):
            suites[str(name)] = list(members)
    return suites


def get_alignment_config() -> dict:
    """Return the ``alignment`` section from app_config, or ``{}``.

    ``embed_url`` names an embedding server (``scripts/embed_server.py``) the
    aligner encodes on instead of loading the model in this process.
    """
    cfg = load_app_config()
    val = cfg.get("alignment")
    if isinstance(val, dict):
        return val
    return {}


def get_save_check_config() -> dict:
    """Return the ``save_check`` section from app_config, or ``{}``.

    ``enabled`` (default true) turns the save-time check off altogether;
    ``disabled_rules`` lists rule ids from ``src.save_check.RULES`` to silence.
    ``model`` names the inference server the check's model layer asks
    (``src.save_check_model.read_model_config``); absent, only the rules run.
    """
    cfg = load_app_config()
    val = cfg.get("save_check")
    if isinstance(val, dict):
        return val
    return {}


def load_forced_glossary_terms(*, force_reload: bool = False) -> list[dict]:
    """Return the words the glossary candidate extractor must always surface.

    A shim over the house lexicon (``src.house.lexicon``), which replaced
    ``forced_glossary_terms.json``: its every-book entries come back in the
    ``{term, type_guess}`` shape the extractor has always read. An install with
    no ``prompts/house_lexicon.json`` yet still has its old forced-terms file
    read, so nothing is lost before it migrates.
    """
    global _FORCED_GLOSSARY_CACHE
    if _FORCED_GLOSSARY_CACHE is not None and not force_reload:
        return list(_FORCED_GLOSSARY_CACHE)

    from src.house.lexicon import forced_terms

    _FORCED_GLOSSARY_CACHE = forced_terms()
    return list(_FORCED_GLOSSARY_CACHE)
