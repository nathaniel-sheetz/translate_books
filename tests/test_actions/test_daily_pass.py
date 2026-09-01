"""The nightly driver: scope, budgets, preflight isolation and failure isolation.

The driver never spawns a CLI here — the action is stubbed at the registry, so
what is under test is the orchestration: which books it picks, which ceilings
stop it, that a logged-out CLI costs only the books pinned to it, and that one
book's exception ends that book rather than the night.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src.actions.registry import Action, ActionState, ApplyResult, RunResult
from src.actions.scope import book_profile as real_book_profile
from src.harness import locks
from tests.test_actions.conftest import make_book

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
daily_pass = importlib.import_module("daily_pass")


class FakeAction:
    """Records what the driver asked of it and answers on script."""

    def __init__(self, *, pending=3, blockers=(), raises=None, targets=None):
        self.pending = pending
        self.blockers = list(blockers)
        self.raises = raises
        self.targets = pending if targets is None else targets
        self.ran: list[str] = []
        self.applied: list[str] = []

    def as_action(self):
        return Action(
            name="annotations",
            detect=self.detect,
            run=self.run,
            auto_apply=self.auto_apply,
        )

    def detect(self, project_dir):
        return ActionState(
            action="annotations", pending=self.pending,
            by_type={"word_choice": self.pending}, blockers=list(self.blockers),
        )

    def run(self, project_dir, budget):
        if self.raises:
            raise self.raises
        self.ran.append(Path(project_dir).name)
        planned = self.targets
        if budget.max_targets is not None:
            planned = min(planned, budget.max_targets)
        return RunResult(
            action="annotations", status="ok", targets=planned, committed=planned,
        )

    def auto_apply(self, project_dir, policy):
        self.applied.append(Path(project_dir).name)
        return ApplyResult(action="annotations", status="ok", applied=["k1"], held=["k2"])


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """Point the driver at a scratch projects root, digest dir and log."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr("src.harness.state.projects_root", lambda: projects)
    monkeypatch.setattr(daily_pass, "DIGEST_DIR", tmp_path / "reports" / "nightly")
    monkeypatch.setattr(daily_pass, "NIGHTLY_LOG", tmp_path / "logs" / "nightly.jsonl")
    monkeypatch.setattr(
        locks, "repo_lock_path", lambda: tmp_path / "logs" / ".nightly.lock"
    )
    # Nothing in these tests should ever probe a real CLI login.
    monkeypatch.setattr(daily_pass, "_preflight", lambda pairs: {})
    return projects


def install(monkeypatch, fake):
    monkeypatch.setattr(
        "src.actions.registry.get_action", lambda name: fake.as_action()
    )
    monkeypatch.setattr(daily_pass.registry, "get_action", lambda name: fake.as_action())


def run(argv, run_id="nightly_20260101_000000"):
    return daily_pass._execute(daily_pass._parse_args(argv), run_id)


# ---------------------------------------------------------------------------
# Scope and budgets
# ---------------------------------------------------------------------------


def test_runs_every_in_scope_book(driver, monkeypatch):
    for name in ("book-a", "book-b"):
        make_book(driver, name)
    make_book(driver, "snapshot", group=".backburner")
    fake = FakeAction()
    install(monkeypatch, fake)

    payload = run([])

    assert sorted(fake.ran) == ["book-a", "book-b"]
    assert payload["totals"]["books"] == 2
    assert payload["stopped_because"] == "finished"


def test_skips_a_book_with_nothing_pending(driver, monkeypatch):
    make_book(driver, "empty")
    install(monkeypatch, FakeAction(pending=0))

    payload = run([])

    assert payload["totals"]["books"] == 0


def test_a_blocked_book_is_recorded_not_run(driver, monkeypatch):
    make_book(driver, "broken")
    fake = FakeAction(blockers=["no .harness/config.json"])
    install(monkeypatch, fake)

    payload = run([])

    assert fake.ran == []
    assert payload["skipped_books"][0]["reason"] == "blocked"
    assert "config.json" in payload["skipped_books"][0]["detail"]


def test_max_books_stops_cleanly_and_says_so(driver, monkeypatch):
    for name in ("a", "b", "c"):
        make_book(driver, name)
    fake = FakeAction()
    install(monkeypatch, fake)

    payload = run(["--max-books", "2"])

    assert len(fake.ran) == 2
    assert payload["stopped_because"] == "max_books"
    assert payload["totals"]["books_left"] == 1


def test_the_target_ceiling_is_shared_across_books(driver, monkeypatch):
    """One night's budget, not one per book."""
    for name in ("a", "b", "c"):
        make_book(driver, name)
    fake = FakeAction(pending=4, targets=4)
    install(monkeypatch, fake)

    payload = run(["--max-targets", "6"])

    assert payload["totals"]["targets"] == 6
    assert payload["stopped_because"] == "max_targets"


def test_project_narrows_the_pass(driver, monkeypatch):
    make_book(driver, "wanted")
    make_book(driver, "other")
    fake = FakeAction()
    install(monkeypatch, fake)

    run(["--project", "wanted"])

    assert fake.ran == ["wanted"]


def test_no_apply_reviews_without_writing(driver, monkeypatch):
    make_book(driver, "a")
    fake = FakeAction()
    install(monkeypatch, fake)

    payload = run(["--no-apply"])

    assert fake.ran == ["a"]
    assert fake.applied == []
    assert payload["policy"] is None


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_no_digest_and_no_log(driver, monkeypatch):
    make_book(driver, "a")
    install(monkeypatch, FakeAction())

    payload = run(["--dry-run"])

    assert payload["dry_run"] is True
    assert "digest" not in payload
    assert not daily_pass.DIGEST_DIR.exists()
    assert not daily_pass.NIGHTLY_LOG.exists()


def test_dry_run_takes_no_lock(driver, monkeypatch):
    """`--dry-run` must answer even about a book a wave is working on."""
    book = make_book(driver, "a")
    fake = FakeAction()
    install(monkeypatch, fake)

    with locks.project_lock(book, kind="annotations", run_id="other"):
        payload = run(["--dry-run"])

    assert fake.ran == ["a"]
    assert payload["totals"]["books"] == 1


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def test_a_locked_book_is_skipped_not_raced(driver, monkeypatch):
    book = make_book(driver, "held")
    make_book(driver, "free")
    fake = FakeAction()
    install(monkeypatch, fake)
    monkeypatch.setattr(daily_pass, "_LOCK_WAIT_S", 0.0)

    with locks.project_lock(book, kind="review-judges", run_id="dashboard"):
        payload = run([])

    assert fake.ran == ["free"]
    locked = [b for b in payload["books"] if b["project_id"] == "held"][0]
    assert locked["status"] == "locked"


def test_the_lock_is_released_after_each_book(driver, monkeypatch):
    book = make_book(driver, "a")
    install(monkeypatch, FakeAction())

    run([])

    assert locks.holder_of(book) is None


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_one_books_exception_does_not_end_the_night(driver, monkeypatch):
    make_book(driver, "a")
    make_book(driver, "b")

    class Exploding(FakeAction):
        def run(self, project_dir, budget):
            if Path(project_dir).name == "a":
                raise RuntimeError("disk on fire")
            return super().run(project_dir, budget)

    fake = Exploding()
    install(monkeypatch, fake)

    payload = run([])

    assert fake.ran == ["b"]
    assert payload["totals"]["errors"] == 1
    failed = [b for b in payload["books"] if b["project_id"] == "a"][0]
    assert "disk on fire" in failed["error"]


def test_a_refused_cli_costs_only_its_own_books(driver, monkeypatch):
    """A logged-out `claude` must not take the Cursor books down with it."""
    claude_book = make_book(driver, "on-claude")
    (claude_book / ".harness" / "config.json").write_text(
        json.dumps({"headless_cli": "claude"}), encoding="utf-8"
    )
    cursor_book = make_book(driver, "on-cursor")
    (cursor_book / ".harness" / "config.json").write_text(
        json.dumps({"headless_cli": "cursor"}), encoding="utf-8"
    )
    fake = FakeAction()
    install(monkeypatch, fake)
    monkeypatch.setattr(
        daily_pass, "_preflight",
        lambda pairs: {
            pair: "Invalid API key · Please run /login"
            for pair in pairs if pair[0] == "claude"
        },
    )

    payload = run([])

    assert fake.ran == ["on-cursor"]
    refused = [s for s in payload["skipped_books"] if s["project_id"] == "on-claude"][0]
    assert refused["reason"] == "preflight:claude"
    assert "/login" in refused["detail"]


def test_one_books_refused_model_leaves_the_rest_of_its_family_alone(driver, monkeypatch):
    """Cursor's third gate validates the model id, so a refusal can be one book's.

    Keyed by CLI, one book pinning a model that gate rejects took every other
    book on that family out of the night, with the wrong reason attached.
    """
    for name in ("bad-model", "good-model"):
        book = make_book(driver, name)
        (book / ".harness" / "config.json").write_text(
            json.dumps({"headless_cli": "cursor"}), encoding="utf-8"
        )
    fake = FakeAction()
    install(monkeypatch, fake)

    def _profile_model(project_dir, **kwargs):
        prof = real_book_profile(project_dir, **kwargs)
        model = "not-a-model" if Path(project_dir).name == "bad-model" else "ok-model"
        return replace(prof, worker_model=model)

    monkeypatch.setattr(daily_pass.ascope, "book_profile", _profile_model)
    monkeypatch.setattr(
        daily_pass, "_preflight",
        lambda pairs: {p: "unrecognised model" for p in pairs if p[1] == "not-a-model"},
    )

    payload = run([])

    assert fake.ran == ["good-model"]
    refused = [s for s in payload["skipped_books"] if s["project_id"] == "bad-model"][0]
    assert refused["detail"] == "unrecognised model"


def test_a_refused_cli_prepares_nothing(driver, monkeypatch):
    """Re-running after `claude` + /login must be a clean start, not a resume."""
    book = make_book(driver, "on-claude")
    (book / ".harness" / "config.json").write_text(
        json.dumps({"headless_cli": "claude"}), encoding="utf-8"
    )
    fake = FakeAction()
    install(monkeypatch, fake)
    monkeypatch.setattr(
        daily_pass, "_preflight",
        lambda pairs: {pair: "logged out" for pair in pairs if pair[0] == "claude"},
    )

    run([])

    assert fake.ran == []
    assert not (book / ".harness" / "annotations").exists()


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_a_real_run_writes_a_digest_and_a_log_row(driver, monkeypatch):
    make_book(driver, "a")
    install(monkeypatch, FakeAction())

    payload = run([])

    digest = next(daily_pass.DIGEST_DIR.glob("annotations_*.md"))
    text = digest.read_text(encoding="utf-8")
    assert "# Nightly annotation pass" in text
    assert "### a" in text
    assert "/review-inbox" in text            # held resolutions need a destination

    row = json.loads(daily_pass.NIGHTLY_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert row["run_id"] == payload["run_id"]
    assert row["totals"]["applied"] == 1


def test_two_runs_in_one_day_each_keep_their_digest(driver, monkeypatch):
    """The digest is the whole notification surface, so it is named per run.

    Dated `annotations_<YYYYmmdd>.md`, a hand-run after fixing a logged-out CLI
    silently replaced the 06:30 pass's record of the other fifteen books.
    """
    make_book(driver, "a")
    install(monkeypatch, FakeAction())

    run([], run_id="nightly_20260101_063000")
    run([], run_id="nightly_20260101_181500")

    digests = sorted(pathlib.Path(daily_pass.DIGEST_DIR).glob("annotations_*.md"))
    assert len(digests) == 2


def test_a_dry_run_survives_one_book_raising(driver, monkeypatch):
    """`--dry-run` is what you reach for when a book is already broken.

    The dry-run branch sat above the try that the module's docstring promises
    ("one book's exception ends that book, not the night"), so one corrupt
    results.json aborted the whole plan and took the other books with it.
    """
    make_book(driver, "broken")
    make_book(driver, "fine")
    fake = FakeAction()
    real_run = fake.run

    def _run(project_dir, budget):
        if Path(project_dir).name == "broken":
            raise RuntimeError("corrupt results.json")
        return real_run(project_dir, budget)

    fake.run = _run
    install(monkeypatch, fake)

    payload = run(["--dry-run"])

    assert fake.ran == ["fine"]
    broken = [b for b in payload["books"] if b["project_id"] == "broken"][0]
    assert broken["status"] == "error"
    assert "corrupt results.json" in broken["error"]


def test_held_resolutions_are_counted_for_the_inbox(driver, monkeypatch):
    make_book(driver, "a")
    make_book(driver, "b")
    install(monkeypatch, FakeAction())

    payload = run([])

    assert payload["totals"]["applied"] == 2
    assert payload["totals"]["held"] == 2


def test_the_repo_lock_stops_two_passes_overlapping(driver, monkeypatch, capsys):
    make_book(driver, "a")
    install(monkeypatch, FakeAction())

    with locks.repo_lock(kind="nightly", run_id="already-running"):
        code = daily_pass.main([])

    assert code == 2
    assert "already running" in capsys.readouterr().err
