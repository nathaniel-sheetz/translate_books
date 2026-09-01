"""Which books an unattended pass may touch, and what backend each resolves to.

Two things are worth pinning here and nothing else is. First, the exclusions:
``projects/`` contains backup snapshots (``.backburner``) whose annotations are
copies of a live book's, so a walker that finds them spends real machine time
producing notes nobody will read. Second, the CLI ladder: ``automation.default_cli``
must move the un-pinned books and must never override a book's own pin.
"""

from __future__ import annotations

import json

import pytest

from src.actions import scope as ascope
from src.harness.host import HOST_OVERRIDE_ENV
from tests.test_actions.conftest import make_book


# ---------------------------------------------------------------------------
# in_scope
# ---------------------------------------------------------------------------


def test_finds_flat_and_grouped_books(tmp_path):
    make_book(tmp_path, "gaudenzia")
    make_book(tmp_path, "short-stories", group=".macdonald")

    result = ascope.in_scope(tmp_path, exclude_groups=[])

    assert [e.project_id for e in result.projects] == ["short-stories", "gaudenzia"]
    assert result.projects[0].group == ".macdonald"
    assert result.projects[1].group is None


def test_excludes_denied_groups(tmp_path):
    make_book(tmp_path, "live-book")
    make_book(tmp_path, "the-little-duke.bak-ch1-restore", group=".backburner")
    make_book(tmp_path, "campeon", group=".published")

    result = ascope.in_scope(tmp_path, exclude_groups=[".backburner", ".published"])

    assert [e.project_id for e in result.projects] == ["live-book"]
    reasons = {s.project_id: s.reason for s in result.skipped}
    assert reasons == {
        "the-little-duke.bak-ch1-restore": ascope.SKIP_EXCLUDED_GROUP,
        "campeon": ascope.SKIP_EXCLUDED_GROUP,
    }


def test_excludes_a_group_at_any_depth(tmp_path):
    """A book nested below a denied folder is still behind that folder."""
    make_book(tmp_path, "deep", group=".backburner/older")

    result = ascope.in_scope(tmp_path, exclude_groups=[".backburner"])

    assert result.projects == []
    assert result.skipped[0].reason == ascope.SKIP_EXCLUDED_GROUP


def test_excludes_archived_books(tmp_path):
    make_book(tmp_path, "live-book")
    make_book(tmp_path, "done-with", archived=True)
    make_book(tmp_path, "explicitly-not", archived=False)

    result = ascope.in_scope(tmp_path, exclude_groups=[])

    assert sorted(e.project_id for e in result.projects) == ["explicitly-not", "live-book"]
    assert [(s.project_id, s.reason) for s in result.skipped] == [
        ("done-with", ascope.SKIP_ARCHIVED)
    ]


def test_unreadable_project_json_is_not_archived(tmp_path):
    """A half-written project.json must not silently drop a book from the pass."""
    book = make_book(tmp_path, "torn")
    (book / "project.json").write_text("{not json", encoding="utf-8")

    result = ascope.in_scope(tmp_path, exclude_groups=[])

    assert [e.project_id for e in result.projects] == ["torn"]


def test_duplicate_leaf_names_keep_the_first(tmp_path):
    """One leaf name is one addressable id — the second cannot be run as itself."""
    make_book(tmp_path, "twin")
    make_book(tmp_path, "twin", group="group-b")

    result = ascope.in_scope(tmp_path, exclude_groups=[])

    assert len(result.projects) == 1
    assert [(s.project_id, s.reason) for s in result.skipped] == [
        ("twin", ascope.SKIP_DUPLICATE_ID)
    ]


def test_a_grouping_folder_is_not_itself_a_book(tmp_path):
    make_book(tmp_path, "inner", group="outer")

    result = ascope.in_scope(tmp_path, exclude_groups=[])

    assert [e.project_id for e in result.projects] == ["inner"]


def test_missing_projects_root_is_empty_not_an_error(tmp_path):
    assert ascope.in_scope(tmp_path / "nope", exclude_groups=[]).projects == []


# ---------------------------------------------------------------------------
# automation_config
# ---------------------------------------------------------------------------


def test_defaults_are_complete():
    """Every key the driver reads must have a default, or a fresh clone breaks."""
    settings = ascope.automation_config()
    for key in ascope.AUTOMATION_DEFAULTS:
        assert key in settings


def test_app_config_overrides_defaults(monkeypatch):
    monkeypatch.setattr(
        "src.actions.scope.load_app_config",
        lambda: {"automation": {"default_cli": "cursor", "concurrency": 9}},
    )
    settings = ascope.automation_config()
    assert settings["default_cli"] == "cursor"
    assert settings["concurrency"] == 9
    assert settings["confidence_floor"] == "high"      # untouched default


def test_none_overrides_are_ignored():
    """A driver can pass its whole argparse namespace without unset-flag guards."""
    settings = ascope.automation_config({"default_cli": None, "concurrency": 2})
    assert settings["default_cli"] == ascope.AUTOMATION_DEFAULTS["default_cli"]
    assert settings["concurrency"] == 2


@pytest.mark.parametrize("key", ["exclude_groups", "auto_apply_types"])
def test_a_string_where_a_list_belongs_is_refused(monkeypatch, key):
    """Both keys are iterated, so a bare string fails silently — twice over.

    ``exclude_groups`` as ``".backburner"`` becomes a set of its own characters:
    the group it names stops being excluded and the pass reviews backup
    snapshots. ``auto_apply_types`` as a string stops matching any type at all.
    One widens the blast radius, one disables the feature; neither says a word.
    """
    monkeypatch.setattr(
        "src.actions.scope.load_app_config",
        lambda: {"automation": {key: ".backburner"}},
    )
    settings = ascope.automation_config()
    assert settings[key] == ascope.AUTOMATION_DEFAULTS[key]


def test_a_real_list_still_overrides(monkeypatch):
    monkeypatch.setattr(
        "src.actions.scope.load_app_config",
        lambda: {"automation": {"exclude_groups": [".attic"]}},
    )
    assert ascope.automation_config()["exclude_groups"] == [".attic"]


# ---------------------------------------------------------------------------
# resolve_book_cli — the ladder
# ---------------------------------------------------------------------------


def test_a_books_pin_always_wins():
    cli, source = ascope.resolve_book_cli(
        {"headless_cli": "cursor"}, default_cli="claude"
    )
    assert (cli, source) == ("cursor", "config")


def test_default_cli_moves_an_unpinned_book():
    cli, source = ascope.resolve_book_cli({"headless_cli": "auto"}, default_cli="cursor")
    assert (cli, source) == ("cursor", "automation.default_cli")


def test_default_cli_outranks_host_detection(monkeypatch):
    """Under a scheduled task there is no host; a manual re-run must match it.

    Otherwise which terminal happened to launch the re-run would decide whether
    a book was reviewed by Claude or by Cursor.
    """
    monkeypatch.setenv(HOST_OVERRIDE_ENV, "claude-code")
    cli, source = ascope.resolve_book_cli({}, default_cli="cursor")
    assert (cli, source) == ("cursor", "automation.default_cli")


def test_host_detection_still_answers_when_no_default_is_set(monkeypatch):
    monkeypatch.setenv(HOST_OVERRIDE_ENV, "cursor")
    cli, source = ascope.resolve_book_cli({}, default_cli=None)
    assert (cli, source) == ("cursor", "host:cursor")


def test_an_explicit_override_beats_everything():
    cli, source = ascope.resolve_book_cli(
        {"headless_cli": "cursor"}, override="claude", default_cli="cursor"
    )
    assert (cli, source) == ("claude", "cli")


@pytest.mark.parametrize("bogus", ["", "gpt", None])
def test_an_unusable_default_cli_falls_through(bogus):
    cli, source = ascope.resolve_book_cli({"headless_cli": "auto"}, default_cli=bogus)
    assert source not in ("automation.default_cli", "config")
    assert cli in ("claude", "cursor")


# ---------------------------------------------------------------------------
# book_profile
# ---------------------------------------------------------------------------


def test_profile_carries_the_resolved_family_into_the_worker_model(tmp_path):
    """The model must come from the family the wave will run on, not a guess."""
    book = make_book(tmp_path, "pinned")
    (book / ".harness" / "config.json").write_text(
        json.dumps({"headless_cli": "claude"}), encoding="utf-8"
    )
    prof = ascope.book_profile(book, default_cli="cursor", check_binary=False)
    assert prof.cli == "claude"
    assert prof.cli_source == "config"
    assert prof.worker_model == "sonnet"
