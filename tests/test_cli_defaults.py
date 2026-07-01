"""CLI default model consistency — aliases and argparse should track DEFAULT_MODEL."""

from scripts.compare_models import _resolve_model
from src.api_translator import DEFAULT_MODEL


def test_sonnet_alias_resolves_to_default_model():
    assert _resolve_model("sonnet") == DEFAULT_MODEL


def test_default_model_is_sonnet_5():
    assert DEFAULT_MODEL == "claude-sonnet-5"
