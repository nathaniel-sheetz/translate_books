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
from src.harness.address_rename import rename_map, rename_rules
from src.harness.address_sample import (
    dialogue_precheck,
    score_chapter_text,
    select_address_sample_chapters,
)
from src.harness_guard import HarnessValidationError, validate_address_map_file
from src.models import AddressMap, AddressPair, AddressRule, Glossary, GlossaryTerm
from src.utils.file_io import load_address_map, save_address_map, save_glossary


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


# ── glossary reconcile: the rename transform ────────────────────────────────
#
# The map is drafted at 0B with ENGLISH cast names because the glossary does not
# exist yet; `rename` carries the approved forms in afterwards. The hazard it
# exists to remove is ordering: a naive sequential substitution turns
# "Bambi's mother" into "la madre de Bambi's mother". The single-pass alternation
# in src/harness/address_rename.py makes that unrepresentable.

def _cast_glossary(*pairs, types=None) -> Glossary:
    return Glossary(terms=[
        GlossaryTerm(english=en, spanish=es, type=(types or {}).get(en, "character"))
        for en, es in pairs
    ])


def _rename(content: str, glossary: Glossary, **kw):
    """Rename a content-only map in memory; returns (map, hits, flags)."""
    m = AddressMap(content=content, **kw)
    hits, flags = rename_map(m, rename_rules(glossary))
    return m, hits, flags


def test_longer_english_name_wins_over_the_shorter_one_it_contains():
    """The ordering hazard, directly: a sequential rename yields 'Great-la tía Harriet'."""
    g = _cast_glossary(("Aunt Harriet", "la tía Harriet"),
                       ("Great-aunt Harriet", "la tía abuela Harriet"))
    m, _, _ = _rename("Betsy obeys Great-aunt Harriet but teases Aunt Harriet.", g)
    assert m.content == "Betsy obeys la tía abuela Harriet but teases la tía Harriet."


def test_a_replacement_is_never_re_matched():
    """Single-pass: 'la madre de Bambi' must not then match the 'Bambi' rule."""
    g = _cast_glossary(("Bambi's mother", "la madre de Bambi"), ("Bambi", "el joven Bambi"))
    m, _, _ = _rename("Bambi's mother speaks to Bambi.", g)
    assert m.content == "La madre de Bambi speaks to el joven Bambi."


def test_a_target_containing_its_own_english_is_not_re_substituted():
    """The self-nesting hazard: 'Detective Smuff' matches inside 'el detective Smuff'.

    Real case on 3 of the 12 books on disk (the Hardy Boys glossaries), plus
    'Signora Patti' → 'la Signora Patti'. Without the target-span check a second
    rename produced 'el el detective Smuff' — and the skill's docs tell the agent
    to re-run the verb to confirm the reconcile landed.
    """
    g = _cast_glossary(("Detective Smuff", "el detective Smuff"))
    once, hits, _ = _rename("Frank distrusts Detective Smuff.", g)
    assert once.content == "Frank distrusts el detective Smuff."
    assert len(hits) == 1

    twice, hits_again, flags_again = _rename(once.content, g)
    assert twice.content == once.content
    assert hits_again == [] and flags_again == []


def test_self_nested_rule_still_fires_on_a_genuinely_english_occurrence():
    """Only the already-reconciled span is spared, not every occurrence of the name."""
    g = _cast_glossary(("Detective Smuff", "el detective Smuff"))
    m, hits, _ = _rename("El detective Smuff nods; Detective Smuff scowls.", g)
    assert m.content == "El detective Smuff nods; el detective Smuff scowls."
    assert len(hits) == 1


def test_a_target_containing_ANOTHER_rules_english_is_not_re_substituted():
    """The cross-nesting hazard, which self-nesting alone did not cover.

    A glossary that keeps a name in one term and translates it in another
    ('Aunt Harriet' → 'la tía Harriet' beside 'Harriet' → 'Enriqueta') leaves the
    shorter rule matching the longer one's finished output. The second rename then
    produced 'la tía Enriqueta' — and the stale-cast warning, which had the same
    blind spot, went empty. The skill's docs tell the agent to re-run the verb to
    confirm the reconcile landed, so 'confirm' created the drift it was disproving.
    """
    g = _cast_glossary(("Aunt Harriet", "la tía Harriet"), ("Harriet", "Enriqueta"))
    once, _, _ = _rename("Betsy obeys Aunt Harriet.", g)
    assert once.content == "Betsy obeys la tía Harriet."

    twice, hits_again, _ = _rename(once.content, g)
    assert twice.content == once.content
    assert hits_again == []


def test_a_stripped_variant_does_not_re_fire_inside_an_approved_form():
    """Same hazard reached through a derived rule rather than a drafted one.

    'the Redhead' → 'el Pelirrojo' also yields the bare 'Redhead' → 'Pelirrojo',
    which matches inside the *other* term's approved 'la esposa de Redhead'.
    """
    g = _cast_glossary(("Redhead's wife", "la esposa de Redhead"),
                       ("the Redhead", "el Pelirrojo"))
    once, _, _ = _rename("Fenton questions Redhead's wife.", g)
    assert once.content == "Fenton questions la esposa de Redhead."
    twice, _, _ = _rename(once.content, g)
    assert twice.content == once.content


def test_a_cross_nested_suppression_is_flagged_but_a_self_nested_one_is_silent():
    """'shadowed' says the glossary contradicts itself; self-nesting says nothing."""
    conflict = _cast_glossary(("Aunt Harriet", "la tía Harriet"), ("Harriet", "Enriqueta"))
    _, _, flags = _rename("Betsy obeys la tía Harriet.", conflict)
    assert [f["kind"] for f in flags] == ["shadowed"]
    assert flags[0]["english"] == "Harriet" and flags[0]["context"]

    clean = _cast_glossary(("Detective Smuff", "el detective Smuff"))
    _, _, no_flags = _rename("Frank distrusts el detective Smuff.", clean)
    assert no_flags == []


def test_sentence_initial_capital_is_carried_into_the_replacement():
    """Approved forms are stored as they read mid-prose ('el tecolote')."""
    g = _cast_glossary(("the screech-owl", "el tecolote"))
    m, _, _ = _rename("The screech-owl uses usted. Adults fear the screech-owl.\n"
                      "- The screech-owl is old.", g)
    assert m.content == ("El tecolote uses usted. Adults fear el tecolote.\n"
                         "- El tecolote is old.")


def test_semicolons_and_colons_do_not_start_a_sentence():
    """Over-capitalizing mid-clause is a visible error; under-capitalizing is not."""
    g = _cast_glossary(("Miss Polly", "la señorita Polly"))
    m, _, _ = _rename("A departure; Miss Polly is cold: Miss Polly stays cold.", g)
    assert "; la señorita Polly is cold" in m.content
    assert ": la señorita Polly stays cold" in m.content


def test_article_stripped_variant_matches_the_bare_form():
    g = _cast_glossary(("the screech-owl", "el tecolote"))
    m, _, _ = _rename("A screech-owl addressing the screech-owl.", g)
    assert m.content == "A tecolote addressing el tecolote."


def test_word_boundaries_prevent_matching_inside_a_longer_word():
    """'Thor' must not fire inside 'authority', nor 'Eric' inside 'Alberico'."""
    g = _cast_glossary(("Thor", "Tor"), ("Eric", "Érico"))
    m, hits, _ = _rename("Ricardo asserts authority over Alberico.", g)
    assert m.content == "Ricardo asserts authority over Alberico."
    assert hits == []


def test_every_surface_is_renamed_including_rule_notes():
    """Nine fields, not the four the old inline haystack read."""
    g = _cast_glossary(("Aunt Ena", "la tía Ena"))
    m = AddressMap(
        content="Aunt Ena is formal.",
        global_rules="Aunt Ena outranks the others.",
        style_guide_summary="Use usted with Aunt Ena.",
        pairs=[AddressPair(a="Aunt Ena", b="Bambi", relationship="Aunt Ena is the aunt",
                           directions={"a_to_b": [
                               AddressRule(form="usted", when="when Aunt Ena is present",
                                           after_event="Aunt Ena arrives",
                                           notes="Aunt Ena is older"),
                               AddressRule(form="tú", when="default"),
                           ]})],
    )
    rename_map(m, rename_rules(g))
    rule = m.pairs[0].directions["a_to_b"][0]
    assert m.content == "La tía Ena is formal."
    assert m.global_rules == "La tía Ena outranks the others."
    assert m.style_guide_summary == "Use usted with la tía Ena."
    assert m.pairs[0].relationship == "La tía Ena is the aunt"
    assert (rule.when, rule.after_event, rule.notes) == (
        "when la tía Ena is present", "La tía Ena arrives", "La tía Ena is older")


def test_pair_identity_fields_keep_the_canonical_lowercase_form():
    """pairs[].a/b hold the approved name verbatim, not a sentence-cased variant."""
    g = _cast_glossary(("Aunt Ena", "la tía Ena"))
    m = AddressMap(content="x", pairs=[AddressPair(
        a="Aunt Ena", b="Bambi",
        directions={"a_to_b": [AddressRule(form="tú", when="default")]})])
    rename_map(m, rename_rules(g))
    assert m.pairs[0].a == "la tía Ena"


def test_untranslated_and_non_character_terms_yield_no_rule():
    g = _cast_glossary(("Pollyanna", "Pollyanna"), ("Boston", "Bostón"),
                       types={"Boston": "place"})
    assert rename_rules(g) == []


def test_flags_mark_possessives_and_compounds():
    g = _cast_glossary(("Aunt Ena", "la tía Ena"), ("boys", "los muchachos"))
    m, _, flags = _rename("Aunt Ena's fawn joined a 1920s boys' adventure.", g)
    assert {f["kind"] for f in flags} == {"possessive", "compound"}
    assert all(f["context"] for f in flags)
    # The possessive IS substituted (the name was genuinely stale); the compound is not.
    assert m.content == "La tía Ena's fawn joined a 1920s boys' adventure."


def test_a_compound_edged_match_is_flagged_and_left_in_english():
    """Substituting anyway produced garbage in a draft a human is asked to approve.

    Live on 3 of the 12 books: `a 1920s boys' adventure` (the-house-on-the-cliff)
    and the quoted vocatives in the-secret-of-the-old-mill and stormy-misty-s-foal.
    A reviewer skimming `flags` would have committed 'Great-la tía Harriet'.
    """
    g = _cast_glossary(("Aunt Harriet", "la tía Harriet"), ("Uncle Dock", "el tío Dock"))
    m, hits, flags = _rename(
        "Betsy obeys Great-aunt Harriet. Lester said 'I'm sorry, Uncle Dock'.", g)
    assert m.content == "Betsy obeys Great-aunt Harriet. Lester said 'I'm sorry, Uncle Dock'."
    assert [f["kind"] for f in flags] == ["compound", "compound"]
    # No substitution happened, so `renamed` must not claim one.
    assert hits == []


# ── glossary reconcile: the beat (rename → commit) ──────────────────────────

def _commit_map(project: Path, **kw) -> None:
    save_address_map(AddressMap.model_validate(kw), project / "address_map.json")


def _commit_glossary(project: Path, glossary: Glossary) -> None:
    save_glossary(glossary, project / "glossary.json")


def test_rename_writes_a_draft_and_leaves_the_committed_map_alone(beat_project: Path):
    """`address-map commit` stays the only write path into address_map.json."""
    _commit_map(beat_project, content="Aunt Polly is stern.",
                pairs=[_asymmetric_pair()], global_rules="tú in family.")
    _commit_glossary(beat_project, _cast_glossary(("Aunt Polly", "la tía Polly")))

    result = flow.address_map_rename(str(beat_project))

    assert load_address_map(beat_project / "address_map.json").content == "Aunt Polly is stern."
    draft = json.loads(Path(result["draft_path"]).read_text(encoding="utf-8"))
    assert draft["content"] == "La tía Polly is stern."
    assert result["renamed"] == [
        {"english": "Aunt Polly", "target": "la tía Polly", "count": 1, "surfaces": ["content"]}
    ]
    assert result["remaining_warnings"] == []


def test_renamed_draft_commits_clean(beat_project: Path):
    """The end of the reconcile: commit validates it and the cast warning is gone."""
    _commit_map(beat_project, content="Aunt Polly uses tú with Old Tom.",
                style_guide_summary="Servants use usted to Aunt Polly.",
                pairs=[_asymmetric_pair()], global_rules="tú in family.")
    _commit_glossary(beat_project, _cast_glossary(("Aunt Polly", "la tía Polly"),
                                                  ("Old Tom", "el viejo Tom")))
    flow.address_map_rename(str(beat_project))

    result = flow.address_map_commit(str(beat_project))
    assert result["warnings"] == []
    committed = load_address_map(Path(result["address_map_path"]))
    assert committed.content == "La tía Polly uses tú with el viejo Tom."
    assert committed.style_guide_summary == "Servants use usted to la tía Polly."


def test_commit_reports_a_stale_cast_without_re_running_glossary_commit(beat_project: Path):
    """The symmetric check: whichever artifact commits second sees the drift."""
    _commit_glossary(beat_project, _cast_glossary(("Aunt Polly", "la tía Polly")))
    draft = state.ensure_harness_dir(beat_project) / "address_map_draft.json"
    draft.write_text(json.dumps({
        "content": "Aunt Polly is stern.", "pairs": [_asymmetric_pair()],
        "global_rules": "tú in family.", "style_guide_summary": "Use usted.",
    }), encoding="utf-8")

    result = flow.address_map_commit(str(beat_project))
    assert any("address-map rename" in w for w in result["warnings"])
    assert any("Aunt Polly" in w for w in result["warnings"])


def test_rename_is_idempotent(beat_project: Path):
    """A reconciled map renames to itself — safe to re-run, a no-op the second time."""
    _commit_map(beat_project, content="Aunt Polly is stern.",
                pairs=[_asymmetric_pair()], global_rules="tú in family.")
    _commit_glossary(beat_project, _cast_glossary(("Aunt Polly", "la tía Polly")))
    flow.address_map_rename(str(beat_project))
    flow.address_map_commit(str(beat_project))

    again = flow.address_map_rename(str(beat_project))
    assert again["renamed"] == []
    assert again["remaining_warnings"] == []


def test_rename_is_idempotent_for_a_self_nested_cast_name(beat_project: Path):
    """End to end on the term class that used to yield 'el el detective Smuff'."""
    pair = {"a": "Frank", "b": "Detective Smuff", "relationship": "boy ↔ officer",
            "directions": {"a_to_b": [{"form": "usted", "when": "default"}]}}
    _commit_map(beat_project, content="Frank uses usted with Detective Smuff.",
                pairs=[pair], global_rules="Detective Smuff is never tuteado.")
    _commit_glossary(beat_project, _cast_glossary(("Detective Smuff", "el detective Smuff")))

    flow.address_map_rename(str(beat_project))
    flow.address_map_commit(str(beat_project))
    committed = load_address_map(beat_project / "address_map.json")
    assert committed.pairs[0].b == "el detective Smuff"
    assert committed.content == "Frank uses usted with el detective Smuff."

    again = flow.address_map_rename(str(beat_project))
    assert again["renamed"] == []
    assert again["remaining_warnings"] == []
    assert "el el" not in json.dumps(
        json.loads(Path(again["draft_path"]).read_text(encoding="utf-8")))


def test_rename_is_idempotent_for_a_cross_nested_cast_name(beat_project: Path):
    """End to end on the term class that used to yield 'la tía Enriqueta'.

    The map is reconciled after one rename; the second must be a no-op, and must
    not quietly re-translate the name inside the form it just approved.
    """
    _commit_map(beat_project, content="Betsy obeys Aunt Harriet.", pairs=[_asymmetric_pair()],
                global_rules="Aunt Harriet outranks Betsy.")
    _commit_glossary(beat_project, _cast_glossary(("Aunt Harriet", "la tía Harriet"),
                                                  ("Harriet", "Enriqueta")))
    flow.address_map_rename(str(beat_project))
    flow.address_map_commit(str(beat_project))
    committed = load_address_map(beat_project / "address_map.json")
    assert committed.content == "Betsy obeys la tía Harriet."

    again = flow.address_map_rename(str(beat_project))
    assert again["renamed"] == []
    assert "Enriqueta" not in Path(again["draft_path"]).read_text(encoding="utf-8")


def test_a_compound_only_term_is_reported_as_unchanged(beat_project: Path):
    """`renamed`/`unchanged` describe what was substituted, not merely what was found.

    The site is flagged for a human and named in a second `remaining_warnings`
    line that says re-running the rename will not clear it.
    """
    _commit_map(beat_project, content="A 1920s boys' adventure.", pairs=[_asymmetric_pair()])
    _commit_glossary(beat_project, _cast_glossary(("boys", "los muchachos")))

    result = flow.address_map_rename(str(beat_project))
    assert result["renamed"] == []
    assert result["unchanged"] == ["boys"]
    assert [f["kind"] for f in result["flags"]] == ["compound"]
    assert len(result["remaining_warnings"]) == 1
    assert "will not clear these" in result["remaining_warnings"][0]
    assert "address-map rename` to apply" not in result["remaining_warnings"][0]


def test_skip_refuses_to_discard_a_built_map(beat_project: Path):
    """'Skip' is offered on every unfinished resume — one keystroke from a built map."""
    _commit_map(beat_project, content="Ricardo uses tú.", pairs=[_asymmetric_pair()])
    flow.address_map_commit(str(beat_project), draft=str(beat_project / "address_map.json"))
    assert state.load_config(beat_project)["address_map_decision"] == "built"

    with pytest.raises(HarnessValidationError, match="already has a committed address map"):
        flow.address_map_skip(str(beat_project))
    assert state.load_config(beat_project)["address_map_decision"] == "built"


def test_precheck_leaves_a_built_decision_alone(beat_project: Path, monkeypatch):
    """A prior 'built' wins over the score — including when dialogue is present."""
    _commit_map(beat_project, content="Ricardo uses tú.", pairs=[_asymmetric_pair()])
    flow.address_map_commit(str(beat_project), draft=str(beat_project / "address_map.json"))

    monkeypatch.setattr(
        "src.harness.address_sample.dialogue_precheck",
        lambda project_dir, **kw: {
            "chapters_scored": 3, "qualifying_chapters": 2, "total_turns": 20,
            "max_density": 4.0, "min_turns": 6, "dialogue_present": True, "top_chapters": [],
        },
    )
    result = flow.address_map_precheck(str(beat_project))

    assert result["address_map_decision"] == "built"
    assert state.load_config(beat_project)["address_map_decision"] == "built"
    assert "built" in result["recommendation"]
    assert "Offer" not in result["recommendation"]


def test_precheck_leaves_a_skipped_decision_alone(beat_project: Path, monkeypatch):
    """An explicit decline is the user's answer, not something to re-derive.

    `precheck` is re-runnable at any time, and both decisions stop the router
    identically — so overwriting 'skipped' with 'no_dialogue' changed nothing
    except the record of who decided, silently. The dialogue-present path used
    to say "Offer" anyway; that must not re-open a declined beat either.
    """
    flow.address_map_skip(str(beat_project))

    monkeypatch.setattr(
        "src.harness.address_sample.dialogue_precheck",
        lambda project_dir, **kw: {
            "chapters_scored": 3, "qualifying_chapters": 2, "total_turns": 20,
            "max_density": 4.0, "min_turns": 6, "dialogue_present": True, "top_chapters": [],
        },
    )
    result = flow.address_map_precheck(str(beat_project))

    assert result["address_map_decision"] == "skipped"
    assert state.load_config(beat_project)["address_map_decision"] == "skipped"
    assert "declined" in result["recommendation"]
    assert "Offer" not in result["recommendation"]


def test_precheck_leaves_built_alone_when_dialogue_absent(beat_project: Path, monkeypatch):
    """Dialogue-light score must not retire a map the user already approved."""
    _commit_map(beat_project, content="Ricardo uses tú.", pairs=[_asymmetric_pair()])
    flow.address_map_commit(str(beat_project), draft=str(beat_project / "address_map.json"))

    monkeypatch.setattr(
        "src.harness.address_sample.dialogue_precheck",
        lambda project_dir, **kw: {
            "chapters_scored": 3, "qualifying_chapters": 0, "total_turns": 1,
            "max_density": 0.1, "min_turns": 6, "dialogue_present": False, "top_chapters": [],
        },
    )
    result = flow.address_map_precheck(str(beat_project))

    assert result["address_map_decision"] == "built"
    assert state.load_config(beat_project)["address_map_decision"] == "built"
    assert "Offer" not in result["recommendation"]

def test_precheck_and_skip_schemas_document_every_key(beat_project: Path):
    """Friction-log #19: the whole address-map family self-documents, not just three verbs."""
    precheck = flow.address_map_precheck(str(beat_project))
    assert not sorted(set(precheck) - set(flow.OUTPUT_SCHEMAS["address-map precheck"]))

    skip = flow.address_map_skip(str(beat_project))
    assert not sorted(set(skip) - set(flow.OUTPUT_SCHEMAS["address-map skip"]))


def test_rename_needs_both_artifacts(beat_project: Path):
    with pytest.raises(FileNotFoundError, match="address map"):
        flow.address_map_rename(str(beat_project))

    _commit_map(beat_project, content="x", pairs=[_asymmetric_pair()])
    with pytest.raises(FileNotFoundError, match="glossary"):
        flow.address_map_rename(str(beat_project))


def test_rename_schema_documents_every_key(beat_project: Path):
    """Friction-log #19: no key ships undocumented."""
    _commit_map(beat_project, content="Aunt Polly is stern.", pairs=[_asymmetric_pair()])
    _commit_glossary(beat_project, _cast_glossary(("Aunt Polly", "la tía Polly")))

    result = flow.address_map_rename(str(beat_project))
    schema = flow.OUTPUT_SCHEMAS["address-map rename"]
    assert not sorted(set(result) - set(schema))
