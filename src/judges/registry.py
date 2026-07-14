"""
Judge registry + suite resolution.

Mirrors ``src/evaluators/__init__.py``'s registry, but for LLM judges. Suites
are named lists of judges; built-in suites are layered under any defined in
``app_config.json``'s ``judge_suites`` section.
"""

from __future__ import annotations

from src.app_config import get_judge_suites
from src.judges.address_judge import AddressComplianceJudge
from src.judges.base import Judge
from src.judges.dialogue_judge import DialogueComplianceJudge

# Registry mapping judge names to classes.
_JUDGE_REGISTRY: dict[str, type[Judge]] = {
    "dialogue": DialogueComplianceJudge,
    "address": AddressComplianceJudge,
}

# Built-in suites (overridable / extendable via app_config.json).
# ``address`` is deliberately NOT in ``default``: it needs a per-book address_map
# prerequisite and is metered, so it is opt-in via its own suite / --judge.
_BUILTIN_SUITES: dict[str, list[str]] = {
    "default": ["dialogue"],
    "address": ["address"],
}


def available_judges() -> list[str]:
    """Return the sorted list of registered judge names."""
    return sorted(_JUDGE_REGISTRY)


def get_judge(name: str) -> Judge:
    """Instantiate a judge by name.

    Raises:
        ValueError: If the name is unknown.
    """
    cls = _JUDGE_REGISTRY.get(name)
    if cls is None:
        avail = ", ".join(available_judges())
        raise ValueError(f"Unknown judge: {name!r}. Available judges: {avail}")
    return cls()


def all_suites() -> dict[str, list[str]]:
    """Return built-in suites merged with config-defined suites (config wins)."""
    merged = dict(_BUILTIN_SUITES)
    merged.update(get_judge_suites())
    return merged


def resolve_suite(name: str) -> list[str]:
    """Resolve a suite name to its list of judge names.

    Raises:
        ValueError: If the suite is unknown or names an unregistered judge.
    """
    suites = all_suites()
    if name not in suites:
        avail = ", ".join(sorted(suites))
        raise ValueError(f"Unknown suite: {name!r}. Available suites: {avail}")

    members = suites[name]
    unknown = [m for m in members if m not in _JUDGE_REGISTRY]
    if unknown:
        raise ValueError(
            f"Suite {name!r} references unregistered judges: {unknown}. "
            f"Available judges: {', '.join(available_judges())}"
        )
    return list(members)
