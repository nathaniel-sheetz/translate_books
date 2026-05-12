"""Tests for src/style_guide_wizard.py — focused on conditional question
filtering and the LLM-prompt manifest summary integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.style_guide_wizard import (
    build_question_prompt,
    get_active_questions,
    load_conditional_questions,
    load_fixed_questions,
    load_question_config,
)
from src.text_feature_detector import FeatureManifest, FeatureResult


@pytest.fixture
def sample_config(tmp_path: Path) -> Path:
    """A small in-memory question config with both fixed and conditional questions."""
    cfg = {
        "fixed": [
            {
                "id": "dialect",
                "question": "Dialect?",
                "options": [{"label": "MX", "style_guide_effect": "Use MX."}],
                "default": 0,
            },
            {
                "id": "forms_of_address",
                "question": "Forms?",
                "options": [{"label": "Tu", "style_guide_effect": "Use tu."}],
                "default": 0,
            },
        ],
        "conditional": [
            {
                "id": "dialogue_formatting",
                "requires": {"feature": "dialogue", "min_count": 5},
                "question": "Dialogue?",
                "options": [{"label": "raya", "style_guide_effect": "Use raya."}],
                "default": 0,
            },
            {
                "id": "verse_handling",
                "requires": {"feature": "verse"},
                "question": "Verse?",
                "options": [{"label": "meter", "style_guide_effect": "Preserve meter."}],
                "default": 0,
            },
        ],
    }
    p = tmp_path / "questions.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _manifest(features: dict[str, FeatureResult]) -> FeatureManifest:
    return FeatureManifest(features=features, generated_at="test")


class TestLoadQuestionConfig:
    def test_dict_format(self, sample_config: Path):
        cfg = load_question_config(sample_config)
        assert {q["id"] for q in cfg["fixed"]} == {"dialect", "forms_of_address"}
        assert {q["id"] for q in cfg["conditional"]} == {
            "dialogue_formatting",
            "verse_handling",
        }

    def test_legacy_list_format_treated_as_all_fixed(self, tmp_path: Path):
        legacy = [
            {"id": "dialect", "question": "?", "options": [{"label": "x", "style_guide_effect": ""}], "default": 0}
        ]
        p = tmp_path / "legacy.json"
        p.write_text(json.dumps(legacy), encoding="utf-8")
        cfg = load_question_config(p)
        assert len(cfg["fixed"]) == 1
        assert cfg["conditional"] == []

    def test_back_compat_helpers(self, sample_config: Path):
        assert len(load_fixed_questions(sample_config)) == 2
        assert len(load_conditional_questions(sample_config)) == 2


class TestGetActiveQuestions:
    def test_only_dialogue_present(self, sample_config: Path):
        m = _manifest({
            "dialogue": FeatureResult("dialogue", True, 10, 0.9),
            "verse": FeatureResult("verse", False, 0, 0.0),
        })
        fixed, conditional, _ = get_active_questions(
            None, config_path=sample_config, manifest=m
        )
        assert {q["id"] for q in fixed} == {"dialect", "forms_of_address"}
        assert [q["id"] for q in conditional] == ["dialogue_formatting"]

    def test_empty_manifest_returns_no_conditional(self, sample_config: Path):
        m = _manifest({})
        fixed, conditional, _ = get_active_questions(
            None, config_path=sample_config, manifest=m
        )
        assert len(fixed) == 2
        assert conditional == []

    def test_min_count_threshold_enforced(self, sample_config: Path):
        # Dialogue present but only 3 instances — under the 5-min-count threshold.
        m = _manifest({"dialogue": FeatureResult("dialogue", True, 3, 0.5)})
        _, conditional, _ = get_active_questions(
            None, config_path=sample_config, manifest=m
        )
        assert conditional == []


class TestBuildQuestionPromptManifest:
    def test_prompt_does_not_include_feature_manifest(self):
        m = _manifest({
            "dialogue": FeatureResult("dialogue", True, 12, 0.9, ["Hello,' said she."]),
            "verse": FeatureResult("verse", False, 0, 0.0),
        })
        fixed = [
            {"id": "dialect", "question": "Dialect?",
             "options": [{"label": "MX", "style_guide_effect": "Use MX."}],
             "default": 0}
        ]
        prompt = build_question_prompt(
            "Some source text.",
            "Spanish",
            "mx",
            fixed,
            {"dialect": 0},
            manifest=m,
        )
        assert "FEATURE MANIFEST:" not in prompt
        assert "feature manifest" not in prompt.lower()

    def test_prompt_without_manifest_has_no_placeholder(self):
        prompt = build_question_prompt(
            "Some source text.", "Spanish", "mx", [], {}, manifest=None
        )
        assert "FEATURE MANIFEST:" not in prompt
