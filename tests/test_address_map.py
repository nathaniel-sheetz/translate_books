"""Tests for the address-map artifact: schema, validator, chapter sampler, and
the harness prepare/commit beat.

    FIXTURE chapters/ ─► address-map prepare ─► (agent draft) ─► address-map commit
                              │ samples dialogue-heavy          │ (canned map JSON)
                              ▼ chapters (whole-book spread)     ▼
                         prompt_path rendered                address_map.json validates
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.harness import flow, state
from src.harness.address_sample import (
    dialogue_precheck,
    score_chapter_text,
    select_address_sample_chapters,
)
from src.harness_guard import HarnessValidationError, validate_address_map_file
from src.models import AddressMap, AddressPair, AddressRule
from src.utils.file_io import load_address_map, save_address_map


# ── model + validator ───────────────────────────────────────────────────────

def _asymmetric_pair() -> dict:
    """Ricardo↔Astrida: tú in private, but usted from Astrida in public."""
    return {
        "a": "Ricardo", "b": "Astrida", "relationship": "foster child ↔ guardian",
        "directions": {
            "a_to_b": [{"form": "tú", "when": "default", "since": "chapter_01"}],
            "b_to_a": [
                {"form": "usted", "when": "public", "notes": "deference before others"},
                {"form": "tú", "when": "default"},
            ],
        },
    }


def test_model_accepts_asymmetric_public_private():
    m = AddressMap.model_validate({"content": "x", "pairs": [_asymmetric_pair()]})
    pair = m.pairs[0]
    assert [r.form for r in pair.directions["a_to_b"]] == ["tú"]
    assert [(r.form, r.when) for r in pair.directions["b_to_a"]] == [
        ("usted", "public"), ("tú", "default"),
    ]


def test_form_alias_normalized():
    assert AddressRule.model_validate({"form": "tu"}).form == "tú"
    assert AddressRule.model_validate({"form": "USTED", "when": ""}).when == "default"


def test_unknown_form_rejected():
    with pytest.raises(Exception):
        AddressRule.model_validate({"form": "vos"})


def test_direction_without_default_rejected():
    with pytest.raises(Exception):
        AddressPair.model_validate(
            {"a": "X", "b": "Y", "directions": {"b_to_a": [{"form": "usted", "when": "public"}]}}
        )


def test_direction_default_must_be_last():
    with pytest.raises(Exception):
        AddressPair.model_validate({
            "a": "X", "b": "Y",
            "directions": {
                "a_to_b": [
                    {"form": "tú", "when": "default"},
                    {"form": "usted", "when": "public"},
                ]
            },
        })


def test_direction_duplicate_default_rejected():
    with pytest.raises(Exception):
        AddressPair.model_validate({
            "a": "X", "b": "Y",
            "directions": {
                "a_to_b": [
                    {"form": "tú", "when": "default"},
                    {"form": "usted", "when": "default"},
                ]
            },
        })


def test_unknown_direction_rejected():
    with pytest.raises(Exception):
        AddressPair.model_validate(
            {"a": "X", "b": "Y", "directions": {"a_to_c": [{"form": "tú", "when": "default"}]}}
        )


def test_validate_address_map_file_roundtrip(tmp_path: Path):
    m = AddressMap.model_validate({"content": "prose", "pairs": [_asymmetric_pair()]})
    out = tmp_path / "address_map.json"
    save_address_map(m, out)
    loaded = validate_address_map_file(out)  # returns the AddressMap
    assert loaded.pairs[0].a == "Ricardo"
    assert load_address_map(out).content == "prose"


def test_validate_address_map_file_rejects_bad_draft(tmp_path: Path):
    bad = tmp_path / "address_map.json"
    bad.write_text(
        json.dumps({"pairs": [{"a": "X", "b": "Y",
                               "directions": {"a_to_b": [{"form": "usted", "when": "public"}]}}]}),
        encoding="utf-8",
    )
    with pytest.raises(HarnessValidationError):
        validate_address_map_file(bad)


# ── chapter sampler ─────────────────────────────────────────────────────────

def _dialogue_chapter(name: str, exchanges: int = 8) -> str:
    line = f'"{name}, will you come with me?" she asked. "Yes, I will," you answered {name}. '
    return (line * exchanges).strip()


def _narration_chapter(name: str) -> str:
    return (f"{name} walked through the quiet village past the old well and the great oak. " * 20).strip()


def test_score_chapter_counts_dialogue_signals():
    s = score_chapter_text("chapter_01", _dialogue_chapter("Betsy", exchanges=5))
    assert s.turns >= 8          # two quoted segments per exchange
    assert s.attributions >= 5   # asked / answered
    assert s.second_person >= 5  # you / will
    assert s.density > 0


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """A project with 9 chapters of varying dialogue density, spread across the book."""
    proj = tmp_path / "samplebook"
    (proj / "chapters").mkdir(parents=True)
    # Dense dialogue in ch01 (beginning), ch05 (middle), ch09 (end); the rest lighter.
    densities = {1: 12, 3: 6, 5: 14, 7: 5, 9: 13}
    for n in range(1, 10):
        cid = f"chapter_{n:02d}"
        if n in densities:
            body = _dialogue_chapter(f"Char{n}", exchanges=densities[n])
        else:
            body = _narration_chapter(f"Char{n}")
        (proj / "chapters" / f"{cid}.txt").write_text(body, encoding="utf-8")
    state.save_config(proj, {})
    return proj


def test_sampler_returns_whole_book_spread(sample_project: Path):
    picks = select_address_sample_chapters(sample_project, max_chapters=3)
    ids = [s.chapter_id for s in picks]
    assert len(ids) == 3
    # One from each third of the book (beginning / middle / end), not just the front.
    assert any(i <= "chapter_03" for i in ids)
    assert any("chapter_04" <= i <= "chapter_06" for i in ids)
    assert any(i >= "chapter_07" for i in ids)


def test_sampler_respects_max_chapters_of_one(sample_project: Path):
    picks = select_address_sample_chapters(sample_project, max_chapters=1)
    assert len(picks) == 1


def test_sampler_rejects_non_positive_max_chapters(sample_project: Path):
    with pytest.raises(ValueError, match="max_chapters"):
        select_address_sample_chapters(sample_project, max_chapters=0)


def test_sampler_skips_dialogue_light_chapters(tmp_path: Path):
    proj = tmp_path / "lightbook"
    (proj / "chapters").mkdir(parents=True)
    (proj / "chapters" / "chapter_01.txt").write_text(_dialogue_chapter("A", 6), encoding="utf-8")
    (proj / "chapters" / "chapter_02.txt").write_text(_narration_chapter("B"), encoding="utf-8")
    picks = select_address_sample_chapters(proj, max_chapters=6)
    # The pure-narration chapter has no quoted turns → excluded by the turn gate.
    assert [s.chapter_id for s in picks] == ["chapter_01"]


# ── harness beat (prepare → commit) ─────────────────────────────────────────

@pytest.fixture
def beat_project(tmp_path: Path) -> Path:
    proj = tmp_path / "beatbook"
    (proj / "chapters").mkdir(parents=True)
    for n in (1, 2, 3):
        (proj / "chapters" / f"chapter_{n:02d}.txt").write_text(
            _dialogue_chapter(f"Char{n}", exchanges=8), encoding="utf-8"
        )
    state.save_config(proj, {"target_language": "Spanish", "locale": "mx"})
    return proj


def test_address_map_prepare_renders_prompt(beat_project: Path):
    result = flow.address_map_prepare(str(beat_project), max_chapters=3)
    prompt_path = Path(result["prompt_path"])
    assert prompt_path.exists()
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "{{" not in prompt  # fully rendered, no leftover placeholders
    assert result["sample_chapters"], "expected at least one sampled chapter"
    assert result["draft_path"].endswith("address_map_draft.json")


def test_address_map_commit_writes_and_validates(beat_project: Path):
    flow.address_map_prepare(str(beat_project), max_chapters=3)
    draft = state.harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(
        json.dumps({
            "content": "Ricardo↔Astrida use tú in private; Astrida uses usted in public.",
            "pairs": [_asymmetric_pair()],
            "global_rules": "tú in family; usted to strangers.",
        }),
        encoding="utf-8",
    )
    result = flow.address_map_commit(str(beat_project))
    out = Path(result["address_map_path"])
    assert out.exists()
    assert result["pair_count"] == 1
    assert result["has_content"] is True
    # The written file validates against the model.
    validate_address_map_file(out)


def test_address_map_commit_rejects_bad_draft(beat_project: Path):
    draft = state.ensure_harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(
        json.dumps({"pairs": [{"a": "X", "b": "Y",
                               "directions": {"a_to_b": [{"form": "usted", "when": "public"}]}}]}),
        encoding="utf-8",
    )
    with pytest.raises(HarnessValidationError):
        flow.address_map_commit(str(beat_project))


def test_address_map_commit_rejects_empty_content(beat_project: Path):
    draft = state.ensure_harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(
        json.dumps({
            "content": "   ",
            "pairs": [_asymmetric_pair()],
            "global_rules": "tú in family.",
        }),
        encoding="utf-8",
    )
    with pytest.raises(HarnessValidationError, match="empty `content`"):
        flow.address_map_commit(str(beat_project))


def test_address_map_prepare_errors_without_chapters(tmp_path: Path):
    proj = tmp_path / "emptybook"
    (proj / "chapters").mkdir(parents=True)
    state.save_config(proj, {})
    with pytest.raises(FileNotFoundError):
        flow.address_map_prepare(str(proj))


# ── dialogue gate (precheck) ────────────────────────────────────────────────
#
# The sampler deliberately falls back to the densest chapters when nothing clears
# the turn threshold, so a dialogue-*light* book that opts in still gets a usable
# sample. `dialogue_precheck` is the honest yes/no the beat needs *before* it is
# offered: a book with no interpersonal dialogue has nothing for a map to say.

def test_dialogue_precheck_detects_dialogue(sample_project: Path):
    r = dialogue_precheck(sample_project)
    assert r["dialogue_present"] is True
    assert r["qualifying_chapters"] == 5   # densities fixture: ch 1,3,5,7,9
    assert r["chapters_scored"] == 9
    assert r["total_turns"] > 0
    assert len(r["top_chapters"]) == 3     # capped, densest first


def test_dialogue_precheck_false_for_pure_narration(tmp_path: Path):
    proj = tmp_path / "narrationbook"
    (proj / "chapters").mkdir(parents=True)
    for n in (1, 2, 3):
        (proj / "chapters" / f"chapter_{n:02d}.txt").write_text(
            _narration_chapter(f"Char{n}"), encoding="utf-8"
        )
    r = dialogue_precheck(proj)
    assert r["dialogue_present"] is False
    assert r["qualifying_chapters"] == 0
    assert r["chapters_scored"] == 3


def test_precheck_records_no_dialogue_decision(tmp_path: Path):
    """A dialogue-free book must stop the router from re-offering the beat."""
    proj = tmp_path / "narrationbeat"
    (proj / "chapters").mkdir(parents=True)
    (proj / "chapters" / "chapter_01.txt").write_text(_narration_chapter("A"), encoding="utf-8")
    state.save_config(proj, {"target_language": "Spanish", "locale": "mx"})

    r = flow.address_map_precheck(str(proj))
    assert r["dialogue_present"] is False
    assert r["address_map_decision"] == "no_dialogue"
    assert "SKIP" in r["recommendation"]
    assert state.load_config(proj).get("address_map_decision") == "no_dialogue"


def test_precheck_leaves_decision_unset_when_dialogue_present(beat_project: Path):
    """With dialogue, the decision stays the user's — precheck must not pre-empt it."""
    r = flow.address_map_precheck(str(beat_project))
    assert r["dialogue_present"] is True
    assert r["address_map_decision"] is None
    assert state.load_config(beat_project).get("address_map_decision") is None


def test_precheck_errors_without_chapters(tmp_path: Path):
    proj = tmp_path / "emptyprecheck"
    (proj / "chapters").mkdir(parents=True)
    state.save_config(proj, {})
    with pytest.raises(FileNotFoundError):
        flow.address_map_precheck(str(proj))


def test_address_map_skip_records_decision(beat_project: Path):
    r = flow.address_map_skip(str(beat_project))
    assert r["address_map_decision"] == "skipped"
    assert state.load_config(beat_project).get("address_map_decision") == "skipped"


# ── style_guide_summary + register threading ────────────────────────────────

def test_style_guide_summary_is_optional_and_roundtrips(tmp_path: Path):
    """Maps written before the field must stay valid; the field must survive a save."""
    legacy = AddressMap.model_validate({"content": "x", "pairs": [_asymmetric_pair()]})
    assert legacy.style_guide_summary is None

    legacy.style_guide_summary = "Children use usted to adults; adults use tú to children."
    out = tmp_path / "address_map.json"
    save_address_map(legacy, out)
    assert load_address_map(out).style_guide_summary.startswith("Children use usted")


def test_commit_warns_when_summary_missing(beat_project: Path):
    """Without the summary the beat informs nothing downstream — say so, don't block."""
    draft = state.ensure_harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(
        json.dumps({"content": "Ricardo↔Astrida use tú.", "pairs": [_asymmetric_pair()],
                    "global_rules": "tú in family."}),
        encoding="utf-8",
    )
    result = flow.address_map_commit(str(beat_project))
    assert result["style_guide_summary"] == ""
    assert any("style_guide_summary" in w for w in result["warnings"])
    # Advisory only: the map still committed, and the decision is recorded.
    assert Path(result["address_map_path"]).exists()
    assert result["address_map_decision"] == "built"


def test_commit_is_clean_with_summary(beat_project: Path):
    draft = state.ensure_harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(
        json.dumps({"content": "Ricardo↔Astrida use tú.", "pairs": [_asymmetric_pair()],
                    "global_rules": "tú in family.",
                    "style_guide_summary": "Use tú among family; usted to strangers."}),
        encoding="utf-8",
    )
    result = flow.address_map_commit(str(beat_project))
    assert result["warnings"] == []
    assert result["style_guide_summary"].startswith("Use tú")


def test_prepare_threads_forms_of_address_answer(beat_project: Path):
    """The one question pulled forward must reach the drafting prompt."""
    hdir = state.ensure_harness_dir(beat_project)
    (hdir / "style_questions.json").write_text(json.dumps([{
        "id": "forms_of_address",
        "question": "How should forms of address be handled?",
        "options": [
            {"label": "Tú dominates (informal)", "style_guide_effect": "USTED_IS_RARE_MARKER"},
            {"label": "Usted dominates (formal)", "style_guide_effect": "USTED_DOMINATES_MARKER"},
        ],
    }]), encoding="utf-8")
    (hdir / "style_answers.json").write_text(
        json.dumps({"forms_of_address": "usted_dominates_formal"}), encoding="utf-8"
    )
    result = flow.address_map_prepare(str(beat_project), max_chapters=3)
    assert result["forms_of_address_loaded"] is True
    prompt = Path(result["prompt_path"]).read_text(encoding="utf-8")
    assert "USTED_DOMINATES_MARKER" in prompt
    assert "{{" not in prompt


def test_prepare_without_answer_still_renders(beat_project: Path):
    result = flow.address_map_prepare(str(beat_project), max_chapters=3)
    assert result["forms_of_address_loaded"] is False
    prompt = Path(result["prompt_path"]).read_text(encoding="utf-8")
    assert "not answered" in prompt
    assert "{{" not in prompt


def test_prepare_without_glossary_demands_english_names(beat_project: Path):
    """Step 0B runs before the glossary; a guessed Spanish name would strand the judge."""
    result = flow.address_map_prepare(str(beat_project), max_chapters=3)
    assert result["characters_loaded"] == 0
    prompt = Path(result["prompt_path"]).read_text(encoding="utf-8")
    assert "ENGLISH source names" in prompt
