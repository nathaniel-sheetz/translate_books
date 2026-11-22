"""The harness's ``task: modernize`` mode: what differs from translating.

A modernize book takes an old text in the target language (an 1880s Madrid
Spanish version, say) and brings it up to current Mexican Spanish with the
fewest changes. It runs through the same harness steps as a translation:
setup, chunk, prepare, headless fan-out, commit, align, reader, epub. What
differs is collected here so ``src.harness.flow`` only asks:

* :func:`is_modernize`: which task the book's config names
* :func:`template_path`: the prompt template the book renders
* :func:`prompt_source`: the chunk text the prompt carries (spelling fixed)
* :func:`fix_draft`: the spelling pass again, on the model's draft, at commit

Translation books never reach any of this: every helper returns the
translation-mode value when the task is not ``modernize``, so their prompts
stay byte-identical.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.modernize.orthography import modernize_spelling

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATES = {"modernize": "prompts/modernize.txt"}


def is_modernize(cfg: dict) -> bool:
    return (cfg.get("task") or "translate") == "modernize"


def template_path(cfg: dict) -> Optional[Path]:
    """The book's prompt template, or ``None`` for ``prompts/translation.txt``.

    ``prompt_template`` in the config wins (relative to the repo root); with
    none, a modernize book renders ``prompts/modernize.txt``.
    """
    name = cfg.get("prompt_template") or DEFAULT_TEMPLATES.get(cfg.get("task") or "")
    if not name:
        return None
    p = Path(name)
    return p if p.is_absolute() else REPO_ROOT / p


def source_language(cfg: dict) -> str:
    return cfg.get("source_language") or "English"


def prompt_source(cfg: dict, text: str) -> str:
    """The source text as the prompt shows it: spelling fixed for modernize."""
    return modernize_spelling(text) if is_modernize(cfg) else text


def fix_draft(cfg: dict, text: str) -> str:
    """The spelling pass on a modernize draft (a no-op on text already fixed)."""
    return modernize_spelling(text) if is_modernize(cfg) else text


_LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German",
                   "it": "Italian", "pt": "Portuguese", "la": "Latin"}


def language_name(code: str) -> str:
    """The language name a prompt uses for a source language code."""
    return _LANGUAGE_NAMES.get((code or "").lower().split("-")[0], code)
