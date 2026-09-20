"""Tests for the model pin and for ``status`` — what a wave would do, unrun.

Two things had to exist before this pass could be put behind a button, and both
are here:

* **The model is pinned somewhere every surface reaches.** ``TRIAGE_CONFIDENCE_FLOOR``
  is one number for the whole corpus and it was swept against verdicts from one
  model. Before the ladder, a run with no ``--worker-model`` fell through to
  whatever the CLI defaults to, so a button would have judged findings on a model
  the floor was never tuned for and no screen would have said so.
* **A caller can ask what a wave would do without starting one.** ``prepare`` is
  the only other thing that knows, and it answers by clearing the drafts. A
  dashboard asking for consent, and a skill deciding whether there is anything
  worth running, both need the numbers before anything is destroyed.

The rung labels are load-bearing, not decoration: a consent block that cannot
distinguish "a flag said so" from "the house default for this CLI" cannot tell
an operator whether the floor applies to the run they are about to approve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.triage import pass_ as tp

ES = (
    "Fru Astrida cantaba de los Sigfridos antiguos.\n\n"
    "El niño escuchaba en silencio junto al fuego.\n"
)
CHUNK = "chapter_01_chunk_000"


def _ni(term: str, *, index: int = 0, eval_name: str = "dictionary") -> dict:
    start = ES.index(term)
    return {
        "eval_name": eval_name, "eval_version": "1.0.0", "issue_index": index,
        "severity": "warning",
        "message": f"'{term}': Unknown word (found 1 time(s))",
        "suggestion": None,
        "location": {
            "raw": f"Character position {start}", "side": "target",
            "paragraph_index": 0, "char_start": start, "char_end": start + len(term),
            "snippet_before": "", "match": term, "snippet_after": "",
        },
        "rule_id": None, "category": None, "term": term,
    }


@pytest.fixture
def book(tmp_path: Path) -> Path:
    """One chapter, two live findings — one per checker."""
    project = tmp_path / "bk"
    (project / "chunks").mkdir(parents=True)
    (project / "chunks" / f"{CHUNK}.json").write_text(
        json.dumps({"id": CHUNK, "chunk_id": CHUNK, "translated_text": ES,
                    "source_text": "Fru Astrida sang of the old Sigfrids."}),
        encoding="utf-8",
    )
    (project / "alignments").mkdir(parents=True)
    rows = [
        {"es_idx": i, "en_idx": i, "es": p.strip(), "en": "", "chunk_id": CHUNK}
        for i, p in enumerate(p for p in ES.split("\n\n") if p.strip())
    ]
    (project / "alignments" / "chapter_01.json").write_text(
        json.dumps({"alignments": rows}), encoding="utf-8",
    )
    (project / "evaluations").mkdir(parents=True)
    (project / "evaluations" / f"{CHUNK}.json").write_text(
        json.dumps({"chunk_id": CHUNK, "normalized_issues": [
            _ni("Sigfridos", index=0),
            _ni("escuchaba", index=1, eval_name="grammar"),
        ]}),
        encoding="utf-8",
    )
    return project


def _tree(root: Path) -> dict[str, float]:
    """Every file under ``root``, by path and mtime."""
    return {
        str(p.relative_to(root)): p.stat().st_mtime
        for p in root.rglob("*") if p.is_file()
    }


# --- the model ladder --------------------------------------------------------

def test_a_flag_outranks_the_book_and_the_house():
    cfg = {tp.MODEL_CONFIG_KEY: "from-config"}
    assert tp._resolve_triage_model(cfg, "cursor", "from-flag") == ("from-flag", "cli")


def test_the_book_outranks_the_house():
    cfg = {tp.MODEL_CONFIG_KEY: "from-config"}
    assert tp._resolve_triage_model(cfg, "cursor", None) == ("from-config", "config")


def test_the_house_default_is_the_calibrated_model():
    """With nothing pinned, a wave runs the model the floor was swept on.

    This is the rung that makes the pass safe to put behind a button: every
    other surface passed the model by hand, and a button has no hand.
    """
    model, source = tp._resolve_triage_model({}, "cursor", None)
    assert source == "repo-default"
    assert model == tp.DEFAULT_TRIAGE_MODEL["cursor"]


def test_a_cli_with_no_calibrated_model_pins_nothing():
    """Claude has never been through ``replay_triage.py --exam``.

    Naming a model here would assert a calibration that does not exist, so the
    rung is absent and the CLI's own default answers — which ``status`` then
    reports as a mismatch rather than hiding.
    """
    assert tp.DEFAULT_TRIAGE_MODEL["claude"] is None
    assert tp._resolve_triage_model({}, "claude", None) == (None, "unpinned")


def test_nothing_pinning_it_is_not_spelled_the_same_as_a_flag():
    """A consent block has to tell those two apart.

    Both used to come back as ``cli``, so a popup rendered "sonnet · cli" for a
    run where nothing had chosen the model — which reads as a pin that is not
    there, on the one screen whose job is to say whether the floor applies.
    """
    assert tp._resolve_triage_model({}, "claude", "typed-by-hand")[1] == "cli"
    assert tp._resolve_triage_model({}, "claude", None)[1] == "unpinned"


@pytest.mark.parametrize("value", ["", "   ", None, 17])
def test_a_blank_or_bogus_config_value_is_not_a_pin(value):
    model, source = tp._resolve_triage_model({tp.MODEL_CONFIG_KEY: value}, "cursor", None)
    assert (model, source) == (tp.DEFAULT_TRIAGE_MODEL["cursor"], "repo-default")


def test_prepare_pins_the_house_model_and_records_the_rung(book: Path):
    """The manifest carries the rung, so ``fanout`` and ``commit`` inherit it."""
    out = tp.prepare(book, cli="cursor")
    assert out["status"] == "ok"
    assert out["model_source"] == "repo-default"
    assert out["effective"]["worker_model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]
    assert out["effective"]["worker_model_source"] == "repo-default"

    manifest, error = tp.load_manifest(book)
    assert error is None
    assert manifest["model_source"] == "repo-default"
    assert manifest["model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]


def test_a_flag_still_wins_at_prepare(book: Path):
    out = tp.prepare(book, cli="cursor", worker_model="something-else")
    assert out["model_source"] == "cli"
    assert out["effective"]["worker_model"] == "something-else"


def test_a_cursor_bracket_still_outranks_the_effort_config(book: Path):
    """The ladder must not cost a pinned model's own effort bracket.

    ``resolve_profile`` is called twice to learn the CLI before reading the
    ladder, and only the second call sees the model — so the bracket has to be
    read there or a level typed into the model id would be silently replaced by
    ``headless_effort_triage``.
    """
    out = tp.prepare(
        book, cli="cursor",
        worker_model="grok-4.5[effort=high,fast=false]",
        cfg={"headless_effort_triage": "low"},
    )
    assert out["effective"]["effort"] == "high"
    assert out["effective"]["effort_source"] == "model-bracket"


def test_the_house_model_carries_its_effort_in_its_own_id(book: Path):
    """Nothing appends a bracket to the calibrated model, and that is correct.

    Cursor's current id scheme names the effort in the id itself
    (``cursor-grok-4.6-medium``), so there is no bracket to read and nothing for
    the effort ladder to set. ``resolve_profile`` says so plainly rather than
    inventing a level — which matters here because appending one would produce
    ``cursor-grok-4.6-medium[effort=medium]``, an id this CLI was never asked
    about, in place of one it lists.
    """
    assert "[" not in tp.DEFAULT_TRIAGE_MODEL["cursor"]
    out = tp.prepare(book, cli="cursor")
    assert out["effective"]["worker_model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]
    assert out["effective"]["effort"] is None
    assert out["effective"]["effort_source"] == "cursor-default"
    assert out["effective"]["effort_channel"] == "none"


# --- the CLI ladder ----------------------------------------------------------

def test_a_cli_flag_outranks_the_book_and_the_house():
    cfg = {tp.CLI_CONFIG_KEY: "claude"}
    assert tp._resolve_triage_cli(cfg, "cursor") == ("cursor", "cli")


def test_the_book_outranks_the_house_cli():
    assert tp._resolve_triage_cli({tp.CLI_CONFIG_KEY: "claude"}, None) == ("claude", "config")


def test_the_house_default_is_the_calibrated_cli():
    """The rung the model ladder is keyed by, and could not supply itself.

    Pinning a model per CLI pins nothing while the CLI is still whatever the book
    and the host happen to say: every ``claude`` answer lands on a row that is
    ``None`` by design.
    """
    assert tp._resolve_triage_cli({}, None) == (tp.DEFAULT_TRIAGE_CLI, "repo-default")
    assert tp.DEFAULT_TRIAGE_CLI == "cursor"


def test_auto_unpins_the_pass_back_to_the_book():
    """A book saying "do not pin this pass", handed back to resolve_profile."""
    assert tp._resolve_triage_cli({tp.CLI_CONFIG_KEY: "auto"}, None) == (None, "auto")


@pytest.mark.parametrize("value", ["", "   ", None, 17, "sonnet"])
def test_a_blank_or_bogus_cli_config_value_is_not_a_pin(value):
    assert tp._resolve_triage_cli({tp.CLI_CONFIG_KEY: value}, None) == (
        tp.DEFAULT_TRIAGE_CLI, "repo-default"
    )


def test_a_book_on_the_other_backend_still_triages_on_the_calibrated_pair(book: Path):
    """The case the dashboard button is for.

    Most books here are pinned ``headless_cli: claude`` or left on ``auto``, and
    the Flask process detection reads is a plain shell. Following that key would
    put the wave on a family with no calibrated model — so it is not followed:
    how a book's prose is written is a different decision from which model
    filters what the checkers said about it.
    """
    out = tp.prepare(book, cfg={"headless_cli": "claude"})

    assert out["effective"]["cli"] == tp.DEFAULT_TRIAGE_CLI
    assert out["effective"]["cli_source"] == "repo-default"
    assert out["effective"]["worker_model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]
    assert out["model_source"] == "repo-default"

    manifest, error = tp.load_manifest(book)
    assert error is None
    # fanout re-resolves from here, with cli_source "manifest": the pin survives
    # into the wave rather than being re-derived from the book's config.
    assert manifest["cli"] == tp.DEFAULT_TRIAGE_CLI


def test_a_cli_flag_still_wins_at_prepare(book: Path):
    out = tp.prepare(book, cli="claude")
    assert out["effective"]["cli"] == "claude"
    assert out["effective"]["cli_source"] == "cli"


def test_unpinning_the_cli_costs_the_calibrated_model(book: Path):
    """What ``triage_headless_cli`` buys, and what it costs, in one place.

    The escape hatch works — the book's own key answers again — and ``status``
    says plainly that nothing on that family was ever calibrated, rather than
    printing a model and going quiet.
    """
    cfg = {tp.CLI_CONFIG_KEY: "auto", "headless_cli": "claude"}
    out = tp.status(book, check_cli=False, cfg=cfg)

    assert out["effective"]["cli"] == "claude"
    assert out["effective"]["cli_source"] == "config"
    assert out["model_source"] == "unpinned"
    assert out["calibrated_model"] is None


def test_the_cli_pin_is_not_swapped_for_a_missing_binary(book: Path, monkeypatch):
    """A pin is a decision, and ``resolve_profile`` only second-guesses guesses.

    Swapping here would be the worst of both: the wave would run on the family
    whose row is ``None``, judged by whatever the launcher defaults to, and the
    only screen that could have said so would be showing the other CLI's name.
    The launcher's own "not on PATH" message is the better failure.
    """
    import src.harness.profile as profile

    assert profile._is_guessed_cli("repo-default") is False
    monkeypatch.setattr(profile, "cli_binary_present", lambda name: name == "claude")
    out = tp.prepare(book)

    assert out["effective"]["cli"] == tp.DEFAULT_TRIAGE_CLI
    assert not [w for w in out["effective"]["warnings"] if "falling back" in w]


def test_status_names_the_way_off_the_pin_when_the_cli_cannot_start(book: Path, monkeypatch):
    """The pin is why this machine was asked for a CLI it may not have."""
    import src.harness.headless as headless

    monkeypatch.setattr(
        headless, "preflight_error", lambda cli, **kw: "cursor-agent is not on PATH"
    )
    out = tp.status(book)

    assert out["preflight_error"] == "cursor-agent is not on PATH"
    assert tp.CLI_CONFIG_KEY in out["instructions"]


# --- status ------------------------------------------------------------------

def test_status_counts_the_work_without_doing_any_of_it(book: Path):
    before = _tree(book)
    out = tp.status(book, check_cli=False)

    assert out["status"] == "ok"
    assert out["triageable"] == 2
    assert out["jobs"] == 1
    assert out["by_eval"] == {"dictionary": 1, "grammar": 1}
    assert _tree(book) == before, "status must not write anything"


def test_status_batches_the_way_prepare_would(book: Path):
    assert tp.status(book, items_per_job=1, check_cli=False)["jobs"] == 2
    assert tp.prepare(book, cli="cursor", items_per_job=1)["jobs"] == 2


def test_nothing_to_triage_is_an_answer_not_a_failure(tmp_path: Path):
    """The difference that lets a caller chain this after the checkers.

    ``prepare`` reports a clean scope as an error and exits 1, which is right at
    a prompt and useless to a dashboard: it cannot tell "this book is clean"
    from "the CLI is broken" without matching on a message.
    """
    empty = tmp_path / "clean"
    (empty / "alignments").mkdir(parents=True)
    out = tp.status(empty, check_cli=False)

    assert out["status"] == "ok"
    assert out["triageable"] == 0
    assert out["jobs"] == 0
    assert "Nothing to triage" in out["instructions"]


def test_prepare_stamps_a_stable_code_for_a_clean_scope(tmp_path: Path):
    empty = tmp_path / "clean"
    (empty / "alignments").mkdir(parents=True)
    out = tp.prepare(empty, cli="cursor")

    assert out["status"] == "error"
    assert out["reason"] == "nothing_to_triage"


def test_status_reports_an_unpinned_run_as_unpinned(book: Path):
    out = tp.status(book, cli="claude", check_cli=False)
    assert out["model_source"] == "unpinned"
    assert out["calibrated_model"] is None
    # resolve_profile still says which of its own defaults answered.
    assert out["effective"]["worker_model_source"] == "default:claude"


def test_status_reports_the_model_and_what_it_was_calibrated_against(book: Path):
    out = tp.status(book, cli="cursor", check_cli=False)
    assert out["model_source"] == "repo-default"
    assert out["calibrated_model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]
    assert out["effective"]["worker_model"] == out["calibrated_model"]


def test_status_shows_when_the_run_would_not_be_calibrated(book: Path):
    """An override is allowed; going quiet about it is not.

    The two fields are separate so a caller can compare them. A screen that
    printed only the model could not tell an operator that the floor about to be
    applied was swept on something else.
    """
    out = tp.status(book, cli="cursor", worker_model="some-other-model", check_cli=False)
    assert out["model_source"] == "cli"
    assert out["effective"]["worker_model"] == "some-other-model"
    assert out["calibrated_model"] == tp.DEFAULT_TRIAGE_MODEL["cursor"]
    assert out["effective"]["worker_model"] != out["calibrated_model"]


def test_status_surfaces_a_preflight_failure(book: Path, monkeypatch):
    """Asked before anything is prepared, so the tick can be disabled up front."""
    import src.harness.headless as headless

    monkeypatch.setattr(
        headless, "preflight_error", lambda cli, **kw: "cursor-agent: not logged in"
    )
    out = tp.status(book, cli="cursor")

    assert out["status"] == "ok"
    assert out["preflight_error"] == "cursor-agent: not logged in"
    assert "nothing can run" in out["instructions"]


def test_status_is_quiet_when_the_cli_is_fine(book: Path, monkeypatch):
    import src.harness.headless as headless

    monkeypatch.setattr(headless, "preflight_error", lambda cli, **kw: None)
    out = tp.status(book, cli="cursor")

    assert out["preflight_error"] is None
    assert "prepare" in out["instructions"]


def test_status_scopes_to_the_chapters_it_was_given(book: Path):
    assert tp.status(book, chapters=["chapter_01"], check_cli=False)["triageable"] == 2
    absent = tp.status(book, chapters=["chapter_99"], check_cli=False)
    assert absent["triageable"] == 0
    assert absent["chapters"] == ["chapter_99"]


def test_status_counts_drafts_a_prepare_would_destroy(book: Path):
    """The warning a caller needs before re-preparing over a wave in flight."""
    assert tp.status(book, check_cli=False)["pending_drafts"] == 0
    tp.prepare(book, cli="cursor")
    draft = tp._draft_path(book, tp.DEFAULT_TRIAGE_MODEL["cursor"], "job-001")
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("[]", encoding="utf-8")

    assert tp.status(book, check_cli=False)["pending_drafts"] == 1


def test_status_rejects_a_batch_size_it_could_not_batch_with(book: Path):
    out = tp.status(book, items_per_job=0, check_cli=False)
    assert out["status"] == "error"
    assert "items_per_job" in out["error"]


def test_the_schema_documents_every_key_status_returns(book: Path):
    out = tp.status(book, check_cli=False)
    documented = set(out["_schema"])
    returned = set(out) - {"_schema"}
    assert returned <= documented, returned - documented
