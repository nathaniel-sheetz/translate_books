"""Tests for src/style_guide_wizard.py — focused on conditional question
filtering and the LLM-prompt manifest summary integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.style_guide_wizard import (
    answers_to_style_guide_fallback,
    build_question_prompt,
    dialect_id_from_locale,
    format_answered_questions,
    get_active_questions,
    load_conditional_questions,
    load_fixed_questions,
    load_question_config,
    option_ids,
    resolve_answer,
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


_DIALECT_Q = {
    "id": "dialect",
    "question": "Dialect?",
    "options": [
        {"label": "Mexican Spanish", "style_guide_effect": "Use MX."},
        {"label": "Castilian Spanish", "style_guide_effect": "Use ES."},
        {"label": "Generic Latin America", "style_guide_effect": "Use LATAM."},
    ],
}


class TestOptionIds:
    def test_ids_are_slugs_aligned_to_options(self):
        assert option_ids(_DIALECT_Q) == [
            "mexican_spanish",
            "castilian_spanish",
            "generic_latin_america",
        ]

    def test_colliding_labels_are_disambiguated(self):
        q = {"options": [{"label": "Keep it"}, {"label": "keep  it!"}, {"label": "Keep it"}]}
        assert option_ids(q) == ["keep_it", "keep_it_2", "keep_it_3"]


class TestResolveAnswer:
    def test_int_index(self):
        assert resolve_answer(_DIALECT_Q, 0) == ("Mexican Spanish", "Use MX.", True)

    def test_numeric_string_index(self):
        assert resolve_answer(_DIALECT_Q, "2") == ("Generic Latin America", "Use LATAM.", True)

    def test_option_id(self):
        assert resolve_answer(_DIALECT_Q, "castilian_spanish") == ("Castilian Spanish", "Use ES.", True)

    def test_exact_label_case_and_space_insensitive(self):
        assert resolve_answer(_DIALECT_Q, "  mexican spanish ") == ("Mexican Spanish", "Use MX.", True)

    def test_label_internal_whitespace_collapsed(self):
        q = {"options": [{"label": "Keep  it  literal", "style_guide_effect": "Keep."}]}
        # Matching ignores irregular spacing, but the canonical label is returned verbatim.
        assert resolve_answer(q, "keep it literal") == ("Keep  it  literal", "Keep.", True)

    def test_label_less_option_does_not_crash(self):
        q = {"options": [{"style_guide_effect": "No label."}]}
        assert resolve_answer(q, 0) == ("", "No label.", True)

    def test_unknown_string_is_custom(self):
        label, effect, matched = resolve_answer(_DIALECT_Q, "tú throughout, kittens speak familiarly")
        assert matched is False
        assert label == "tú throughout, kittens speak familiarly"
        assert effect == ""

    def test_out_of_range_int_is_custom_not_wrong_option(self):
        # The old code silently dropped this; it must never resolve to an option.
        label, effect, matched = resolve_answer(_DIALECT_Q, 9)
        assert matched is False
        assert effect == ""


# A dialect question carrying the full shipped option set, so the locale→dialect
# mapping can resolve every region (the trimmed _DIALECT_Q only has three).
_FULL_DIALECT_Q = {
    "id": "dialect",
    "question": "Dialect?",
    "options": [
        {"label": "Mexican Spanish"},
        {"label": "Castilian Spanish"},
        {"label": "Rioplatense Spanish"},
        {"label": "Colombian Spanish"},
        {"label": "Generic Latin America"},
    ],
}


class TestDialectFromLocale:
    @pytest.mark.parametrize(
        "locale,expected",
        [
            ("mx", "mexican_spanish"),
            ("MX", "mexican_spanish"),
            ("es-MX", "mexican_spanish"),
            ("es_mx", "mexican_spanish"),
            ("Mexico", "mexican_spanish"),
            ("es", "generic_latin_america"),
            ("eses", "castilian_spanish"),
            ("es-ES", "castilian_spanish"),
            ("spain", "castilian_spanish"),
            ("ar", "rioplatense_spanish"),
            ("argentina", "rioplatense_spanish"),
            ("co", "colombian_spanish"),
            ("latam", "generic_latin_america"),
            ("419", "generic_latin_america"),
            ("es-419", "generic_latin_america"),
        ],
    )
    def test_known_locales_map_to_option_ids(self, locale, expected):
        assert dialect_id_from_locale(locale, _FULL_DIALECT_Q) == expected

    def test_unknown_locale_returns_none(self):
        assert dialect_id_from_locale("fr-FR", _FULL_DIALECT_Q) is None

    def test_empty_locale_returns_none(self):
        assert dialect_id_from_locale("", _FULL_DIALECT_Q) is None

    def test_mapped_id_absent_from_options_returns_none(self):
        # _DIALECT_Q has no Rioplatense option, so an Argentine locale must not
        # resolve to an id the question can't offer.
        assert dialect_id_from_locale("ar", _DIALECT_Q) is None
        # …but a region that IS present still resolves.
        assert dialect_id_from_locale("mx", _DIALECT_Q) == "mexican_spanish"


class TestFormatAnsweredQuestions:
    def test_effects_only_when_requested(self):
        without = format_answered_questions([_DIALECT_Q], {"dialect": "mexican_spanish"})
        assert without == "- Dialect? -> Mexican Spanish"
        with_fx = format_answered_questions(
            [_DIALECT_Q], {"dialect": "mexican_spanish"}, include_effects=True
        )
        assert "Use MX." in with_fx


class TestAnswersToStyleGuideFallback:
    def test_option_id_pulls_effect(self):
        out = answers_to_style_guide_fallback([_DIALECT_Q], {"dialect": "castilian_spanish"})
        assert out == "Use ES."

    def test_custom_text_uses_question_header(self):
        out = answers_to_style_guide_fallback([_DIALECT_Q], {"dialect": "andean Spanish, formal"})
        assert out == "DIALECT\nandean Spanish, formal"


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
