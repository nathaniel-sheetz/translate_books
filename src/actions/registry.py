"""The action dataclasses and the registry of registered actions.

One entry today (``annotations``). The shape is the point: every wave type in
this repo already follows ``prepare → fanout → commit → apply``, so a second
action is a second adapter module and one line in :data:`ACTIONS`, not a
redesign of the driver.

The four types below are the whole contract between an action and its driver:

``ActionState``   what ``detect`` found — counts and blockers, no spend.
``Budget``        what ``run`` is allowed to consume.
``RunResult``     what ``run`` did.
``Policy``/``ApplyResult``  which results may be written back unattended, and
                  what happened when they were.

Everything is JSON-safe via ``to_payload()`` so a scanner, a driver log and a
web response can all relay the same object without re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Confidence ladder the review verdicts use (``review.parse_verdict`` coerces
# anything else to ``medium``). Ordered weakest-first so a floor is a >= test.
CONFIDENCE_ORDER = ("low", "medium", "high")


def confidence_rank(value: str | None) -> int:
    """Position of a confidence label in :data:`CONFIDENCE_ORDER`.

    An unrecognised label ranks as ``low`` — the safe direction, since the rank
    is only ever compared against a floor that decides whether to write.
    """
    try:
        return CONFIDENCE_ORDER.index(str(value or "").strip().lower())
    except ValueError:
        return 0


@dataclass(frozen=True)
class ActionState:
    """What one action found outstanding in one book. Costs nothing to produce.

    ``blockers`` is the field that earns this type: a book with 12 pending notes
    and a pinned CLI that is not installed is *not* 12 units of available work,
    and a scanner that reported only the count would send the driver at it every
    night to fail the same way.

    ``attention`` is the other half, and is deliberately *not* a blocker: an
    orphaned annotation (its anchor sentence no longer exists, so no run will
    ever reach it) needs a human to re-anchor it, but it must not stop the
    eleven notes beside it from being reviewed tonight.
    """

    action: str
    pending: int
    by_type: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def runnable(self) -> bool:
        """True when there is work here and nothing fatal is stopping it."""
        return self.pending > 0 and not self.blockers

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "pending": self.pending,
            "by_type": dict(self.by_type),
            "skipped": dict(self.skipped),
            "blockers": list(self.blockers),
            "attention": list(self.attention),
            "runnable": self.runnable,
            **({"detail": dict(self.detail)} if self.detail else {}),
        }


@dataclass(frozen=True)
class Budget:
    """What one ``run`` may spend.

    ``deadline`` is a :func:`time.monotonic` stamp rather than a duration so a
    driver can hand the *same* deadline to every book of the night and each one
    can tell how much of it is left.

    The two CLI fields are not interchangeable. ``cli`` **forces** a family past
    a book's own pin and exists for debugging only — the scheduled path leaves it
    ``None``. ``default_cli`` is ``automation.default_cli``: the fallback for
    books that never pinned one, which is the lever that moves the un-pinned
    majority without editing a single book's config.
    """

    concurrency: int = 5
    max_targets: Optional[int] = None
    deadline: Optional[float] = None
    cli: Optional[str] = None
    default_cli: Optional[str] = None
    dry_run: bool = False


@dataclass(frozen=True)
class RunResult:
    """What one ``run`` actually did.

    ``status`` is ``"ok"`` (everything landed), ``"partial"`` (something failed
    or was left over — the usual outcome of a budget ceiling) or ``"error"``
    (nothing useful happened; ``errors`` says why).
    """

    action: str
    status: str
    targets: int = 0
    wrote: int = 0
    failed: int = 0
    committed: int = 0
    errors: list[str] = field(default_factory=list)
    report_path: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "targets": self.targets,
            "wrote": self.wrote,
            "failed": self.failed,
            "committed": self.committed,
            "errors": list(self.errors),
            "report_path": self.report_path,
            **({"detail": dict(self.detail)} if self.detail else {}),
        }


@dataclass(frozen=True)
class Policy:
    """Which reviewed results may be written back with nobody watching.

    Two independent gates, because they fail differently. ``types`` excludes
    whole categories whose writes are *not* recoverable — a ``footnote`` write is
    ``mode: "replace"`` and its text is published into the EPUB by
    :mod:`src.endnotes`, so it is a human's call forever. ``confidence_floor``
    excludes individual results the reviewer itself was unsure of.
    """

    types: tuple[str, ...] = ("word_choice", "inconsistency", "flag")
    confidence_floor: str = "high"
    dry_run: bool = False

    def accepts(self, item: dict[str, Any]) -> bool:
        """True when one ``review.apply`` plan entry clears both gates.

        ``mode`` is checked as well as ``type`` on purpose: the type list is
        configurable, and the one thing that must never be automatic is a
        *replacing* write. Belt and braces beats a typo in ``app_config.json``
        publishing a model's gloss into a book.
        """
        if item.get("mode") != "append":
            return False
        if item.get("type") not in self.types:
            return False
        return confidence_rank(item.get("confidence")) >= confidence_rank(
            self.confidence_floor
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "types": list(self.types),
            "confidence_floor": self.confidence_floor,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ApplyResult:
    """What ``auto_apply`` wrote, and what it deliberately left behind.

    ``held`` is the interesting number: the plan entries the policy refused.
    They are not failures — they are the web inbox's queue.
    """

    action: str
    status: str
    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "applied": list(self.applied),
            "already_applied": list(self.already_applied),
            "held": list(self.held),
            "stale": list(self.stale),
            "errors": list(self.errors),
            **({"detail": dict(self.detail)} if self.detail else {}),
        }


@dataclass(frozen=True)
class Action:
    """One kind of unattended work, as three callables.

    ``auto_apply`` is ``None`` for an action whose results are never safe to
    write without a human — the driver then runs it and stops at the report.
    """

    name: str
    detect: Callable[[Path], ActionState]
    run: Callable[[Path, Budget], RunResult]
    auto_apply: Optional[Callable[[Path, Policy], ApplyResult]] = None
    description: str = ""


# Imported last: ``annotations`` imports the dataclasses above, so this line must
# run after they exist. Package import order guarantees it — ``src/actions/
# __init__.py`` imports this module, so ``src.actions`` is already initialising
# by the time anything can reach ``src.actions.annotations``.
from src.actions.annotations import annotations_action  # noqa: E402

ACTIONS: tuple[Action, ...] = (annotations_action,)


def action_names() -> tuple[str, ...]:
    """Registered action names, in registry order."""
    return tuple(a.name for a in ACTIONS)


def get_action(name: str) -> Action:
    """Look one up by name.

    Raises:
        ValueError: If the name is not registered.
    """
    for action in ACTIONS:
        if action.name == name:
            return action
    raise ValueError(
        f"Unknown action: {name!r}. Available actions: {', '.join(action_names())}"
    )
