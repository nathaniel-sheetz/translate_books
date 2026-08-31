"""Fixtures for the action-registry tests.

Reuses the annotation-review project fixture rather than building a second
book: an action is an adapter over that pipeline, so testing it against a
different-shaped project would be testing a fiction.
"""

from __future__ import annotations

import json

import pytest

from src.app_config import load_app_config
from tests.test_annotations.conftest import project  # noqa: F401 - re-exported fixture


@pytest.fixture(autouse=True)
def _clear_app_config_cache():
    """``load_app_config`` caches after first read; tests mutate the block."""
    load_app_config(force_reload=True)
    yield
    load_app_config(force_reload=True)


def make_book(root, name, *, group=None, harness=True, archived=None):
    """Create a minimal project dir under ``root`` (optionally in a group)."""
    parent = root / group if group else root
    parent.mkdir(parents=True, exist_ok=True)
    book = parent / name
    (book / "chunks").mkdir(parents=True)
    if harness:
        (book / ".harness").mkdir()
        (book / ".harness" / "config.json").write_text("{}", encoding="utf-8")
    if archived is not None:
        (book / "project.json").write_text(
            json.dumps({"archived": archived}), encoding="utf-8"
        )
    return book
