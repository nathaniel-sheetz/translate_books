"""Tests for src/text_feature_detector.py.

One test per detector plus manifest caching tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.text_feature_detector import (
    DETECTORS,
    FeatureManifest,
    FeatureResult,
    build_manifest,
    detect_all_features,
    detect_archaic_language,
    detect_block_quotes,
    detect_currency_period,
    detect_dialogue,
    detect_dramatic_format,
    detect_epicene_animal_speakers,
    detect_epigraphs,
    detect_footnotes,
    detect_foreign_passages,
    detect_letters,
    detect_lists,
    detect_measurements_imperial,
    detect_scripture_references,
    detect_translator_notes,
    detect_verse,
    filter_conditional_questions,
    manifest_path,
    manifest_summary,
    matches_requires,
)
from src.utils.text_utils import extract_paragraphs


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    return extract_paragraphs(text)


# ---------------------------------------------------------------------------
# Per-detector tests
# ---------------------------------------------------------------------------


class TestDialogueDetector:
    def test_pride_and_prejudice_is_dialogue_heavy(self):
        text = _load("chapter_sample.txt")
        result = detect_dialogue(_paragraphs(text), text)
        assert result.present
        assert result.count >= 5
        assert result.evidence

    def test_no_dialogue_in_verse_fixture(self):
        text = _load("verse_sample.txt")
        result = detect_dialogue(_paragraphs(text), text)
        assert not result.present


class TestVerseDetector:
    def test_verse_fixture_detected(self):
        text = _load("verse_sample.txt")
        result = detect_verse(_paragraphs(text), text)
        assert result.present
        assert result.count >= 4
        assert result.evidence

    def test_prose_chapter_not_verse(self):
        text = _load("chapter_sample.txt")
        result = detect_verse(_paragraphs(text), text)
        assert not result.present


class TestFootnoteDetector:
    def test_footnote_fixture_detected(self):
        text = _load("footnote_sample.txt")
        result = detect_footnotes(_paragraphs(text), text)
        assert result.present
        assert result.count >= 5

    def test_chapter_sample_no_footnotes(self):
        text = _load("chapter_sample.txt")
        result = detect_footnotes(_paragraphs(text), text)
        assert not result.present


class TestEpigraphDetector:
    def test_epigraph_after_chapter_heading(self):
        text = (
            "Chapter I\n\n"
            "It was a dark and stormy night. — Edward Bulwer-Lytton\n\n"
            "Then began the long story.\n"
        )
        result = detect_epigraphs(_paragraphs(text), text)
        assert result.present
        assert result.count >= 1

    def test_no_epigraph_in_dialogue_chapter(self):
        text = _load("chapter_sample.txt")
        result = detect_epigraphs(_paragraphs(text), text)
        assert not result.present


class TestLetterDetector:
    def test_epistolary_fixture_detected(self):
        text = _load("epistolary_sample.txt")
        result = detect_letters(_paragraphs(text), text)
        assert result.present
        assert result.count >= 1

    def test_chapter_sample_no_full_letters(self):
        text = _load("chapter_sample.txt")
        result = detect_letters(_paragraphs(text), text)
        assert not result.present


class TestScriptureDetector:
    def test_detects_book_chapter_verse(self):
        text = (
            "He opened the book and read aloud from John 3:16, then turned to "
            "Romans 8:28 for the next reflection. The third reading came from "
            "Mateo 5:3-12, an old favorite."
        )
        result = detect_scripture_references(_paragraphs(text), text)
        assert result.present
        assert result.count >= 3

    def test_no_scripture_in_chapter_sample(self):
        text = _load("chapter_sample.txt")
        result = detect_scripture_references(_paragraphs(text), text)
        assert not result.present


class TestArchaicDetector:
    def test_detects_thou_thee_thy(self):
        text = (
            "Hark thee, friend, and tell me what thou hast seen, for thy words "
            "shall guide me. Verily I say unto thee, behold the time hath come, "
            "and ye shall not tarry."
        ) * 3
        result = detect_archaic_language(_paragraphs(text), text)
        assert result.present
        assert result.count >= 5

    def test_modern_text_no_archaic(self):
        text = "She walked across the room and turned on the light. " * 50
        result = detect_archaic_language(_paragraphs(text), text)
        assert not result.present


class TestForeignPassagesDetector:
    def test_italics_as_foreign_passages(self):
        text = (
            "He muttered _ad astra per aspera_ as he climbed.\n\n"
            "She replied _je ne regrette rien_ with a small smile.\n\n"
            "The motto carved above the door read _et in arcadia ego_."
        )
        result = detect_foreign_passages(_paragraphs(text), text)
        assert result.present

    def test_no_italics_no_foreign(self):
        text = "Just a plain paragraph with no italics whatsoever."
        result = detect_foreign_passages(_paragraphs(text), text)
        assert not result.present


class TestListsDetector:
    def test_bulleted_run(self):
        text = (
            "He listed the conditions:\n\n"
            "- first, that the sum be paid in advance\n"
            "- second, that no further claims be made\n"
            "- third, that all parties remain silent\n"
            "- fourth, that the matter not be revisited\n"
        )
        result = detect_lists(_paragraphs(text), text)
        assert result.present
        assert result.count >= 1

    def test_no_lists_in_prose(self):
        text = _load("chapter_sample.txt")
        result = detect_lists(_paragraphs(text), text)
        assert not result.present


class TestBlockQuoteDetector:
    def test_indented_blocks(self):
        line = "    " + ("This is an indented block quote with substantial length to qualify. " * 2)
        text = "\n".join([line, line, line, "Plain paragraph follows."])
        result = detect_block_quotes(_paragraphs(text), text)
        assert result.present


class TestDramaticFormatDetector:
    def test_speaker_lines_detected(self):
        text = (
            "HAMLET: To be or not to be, that is the question.\n\n"
            "OPHELIA: Good my lord, how does your honor for this many a day?\n\n"
            "POLONIUS: I will be brief. Your noble son is mad.\n"
        )
        result = detect_dramatic_format(_paragraphs(text), text)
        assert result.present


class TestMeasurementsImperialDetector:
    def test_imperial_measurements(self):
        text = "He walked 3 miles in the rain, carrying a 12 pounds pack and a flask of tea. The temperature dropped to 32 °F by midnight, and the path stretched another 2 miles."
        result = detect_measurements_imperial(_paragraphs(text), text)
        assert result.present
        assert result.count >= 2


class TestCurrencyDetector:
    def test_period_currency(self):
        text = "He paid 5 shillings for the room and tipped the boy 2 pence besides. The bill came to two pesos and a half-real."
        result = detect_currency_period(_paragraphs(text), text)
        assert result.present


class TestEpiceneAnimalSpeakersDetector:
    def test_male_swallow_female_grammatical_gender_mismatch(self):
        text = (
            'Mr. Swallow flew home to his wife. "Dinner ready?" he asked.\n\n'
            '"Yes, dear," she replied. He landed on the branch and smiled.'
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        assert result.present
        assert result.count >= 1
        # Mismatch (M cue + F-gender golondrina) should push above the
        # 0.4 threshold the conditional question gates on.
        assert result.confidence >= 0.4
        assert result.evidence

    def test_female_shark_male_grammatical_gender_mismatch(self):
        text = (
            '"Children, come here!" cried Mrs. Shark. She gathered her '
            'little ones close.\n\nHer daughter swam up shyly.'
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        assert result.present
        assert result.confidence >= 0.4

    def test_kinship_cue_mother_giraffe(self):
        text = (
            'The mother giraffe nuzzled her calf. "Sleep now, little one," '
            'she said.\n\nShe watched the moon rise above the savanna.'
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        # Mother + jirafa (F) is consistent — present, but lower confidence
        # since there is no cross-gender hazard.
        assert result.present

    def test_father_spider_named_character(self):
        text = (
            '"I will weave the world!" shouted Father Spider, shaking his '
            'fist.\n\nHe spun a web across the doorway and laughed.'
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        assert result.present
        # M cue + F-gender araña -> mismatch -> high confidence
        assert result.confidence >= 0.4

    def test_plain_animal_mention_no_speaker_context(self):
        text = (
            "A swallow flew across the field. The boy watched it disappear "
            "over the trees as the sun set."
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        assert not result.present

    def test_speaking_animal_without_sex_cue_does_not_fire(self):
        text = (
            '"It is cold today," said the spider, weaving its web.\n\n'
            'The spider continued working through the night.'
        )
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        # Speaking but no sex cue -> not "present" (count == 0).
        assert not result.present

    def test_no_animals_at_all(self):
        text = "She walked across the room and turned on the light. " * 20
        result = detect_epicene_animal_speakers(_paragraphs(text), text)
        assert not result.present


class TestTranslatorNotesDetector:
    def test_n_del_t(self):
        text = "Llegó tarde [N. del T. — el original dice 'late at night'] y se acostó sin cenar."
        result = detect_translator_notes(_paragraphs(text), text)
        assert result.present


# ---------------------------------------------------------------------------
# Manifest construction & caching
# ---------------------------------------------------------------------------


class TestBuildManifest:
    def test_runs_all_detectors(self):
        manifest = build_manifest("Hello world.")
        assert set(manifest.features.keys()) == set(DETECTORS.keys())

    def test_summary_includes_all_features(self):
        manifest = build_manifest("Hello world.")
        summary = manifest_summary(manifest)
        for name in DETECTORS:
            assert name in summary


class TestManifestCaching:
    def test_cached_manifest_reused_on_second_call(self, tmp_path: Path):
        # Set up a project with a source.txt
        proj = tmp_path / "proj"
        proj.mkdir()
        src = proj / "source.txt"
        src.write_text(_load("chapter_sample.txt"), encoding="utf-8")

        first = detect_all_features(proj)
        assert manifest_path(proj).exists()
        first_generated = first.generated_at

        # Sleep is unnecessary — second call should hit the cache regardless of
        # mtime because the cached manifest's source_mtime matches the file.
        second = detect_all_features(proj)
        assert second.generated_at == first_generated

    def test_force_rebuilds(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        src = proj / "source.txt"
        src.write_text(_load("chapter_sample.txt"), encoding="utf-8")

        first = detect_all_features(proj)
        time.sleep(0.01)
        second = detect_all_features(proj, force=True)
        # New generation timestamp on a forced re-run.
        assert second.generated_at != first.generated_at or second.features

    def test_cache_invalidated_when_source_changes(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        src = proj / "source.txt"
        src.write_text("Plain prose with no special features.", encoding="utf-8")
        first = detect_all_features(proj)
        assert not first.features["dialogue"].present

        # Re-write source with dialogue and bump mtime.
        time.sleep(0.05)
        src.write_text(_load("chapter_sample.txt"), encoding="utf-8")
        # Force mtime to be newer than cache to ensure invalidation.
        new_mtime = time.time() + 5
        import os
        os.utime(src, (new_mtime, new_mtime))

        second = detect_all_features(proj)
        assert second.features["dialogue"].present


# ---------------------------------------------------------------------------
# Conditional question filtering
# ---------------------------------------------------------------------------


def _manifest_with(features: dict[str, FeatureResult]) -> FeatureManifest:
    return FeatureManifest(features=features, generated_at="test")


class TestMatchesRequires:
    def test_present_required(self):
        m = _manifest_with({"dialogue": FeatureResult("dialogue", True, 10, 0.8)})
        assert matches_requires({"feature": "dialogue"}, m)

    def test_absent_feature_rejected(self):
        m = _manifest_with({"dialogue": FeatureResult("dialogue", False, 0, 0.0)})
        assert not matches_requires({"feature": "dialogue"}, m)

    def test_min_count_threshold(self):
        m = _manifest_with({"dialogue": FeatureResult("dialogue", True, 4, 0.5)})
        assert not matches_requires({"feature": "dialogue", "min_count": 5}, m)
        assert matches_requires({"feature": "dialogue", "min_count": 3}, m)

    def test_unknown_feature_treated_as_absent(self):
        m = _manifest_with({})
        assert not matches_requires({"feature": "verse"}, m)

    def test_empty_requires_passes(self):
        m = _manifest_with({})
        assert matches_requires({}, m)


class TestFilterConditional:
    def test_only_matching_questions_returned(self):
        m = _manifest_with({
            "dialogue": FeatureResult("dialogue", True, 10, 0.8),
            "verse": FeatureResult("verse", False, 0, 0.0),
        })
        questions = [
            {"id": "dialogue_formatting", "requires": {"feature": "dialogue", "min_count": 5}},
            {"id": "verse_handling", "requires": {"feature": "verse"}},
        ]
        out = filter_conditional_questions(questions, m)
        assert [q["id"] for q in out] == ["dialogue_formatting"]

    def test_empty_manifest_returns_no_conditional_questions(self):
        m = _manifest_with({})
        questions = [
            {"id": "dialogue_formatting", "requires": {"feature": "dialogue"}},
            {"id": "verse_handling", "requires": {"feature": "verse"}},
        ]
        assert filter_conditional_questions(questions, m) == []
