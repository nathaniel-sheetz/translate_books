"""Unattended book-maintenance actions.

An *action* is one kind of pending work a book can have that a machine can
finish on its own: today the reader's annotations, tomorrow the judge passes and
the stale-EPUB rebuild. Each one answers three questions in the same shape —
"what is outstanding here?" (``detect``), "do it" (``run``), and "which of the
results are safe to write back without a human?" (``auto_apply``) — so a driver
can walk every book and every action without knowing what either one is.

The package deliberately owns no pipeline of its own. ``annotations`` is a thin
adapter over :mod:`src.annotations.review`; a second action will be a second
adapter, not a refactor.

Import :mod:`src.actions.registry` for the dataclasses and the action list, and
:mod:`src.actions.scope` for which books are in scope.
"""

from __future__ import annotations

from src.actions.registry import (  # noqa: F401 - re-exported package surface
    ACTIONS,
    Action,
    ActionState,
    ApplyResult,
    Budget,
    Policy,
    RunResult,
    action_names,
    get_action,
)
