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
