"""What a model id says about itself, read off the string alone.

A new model release should not need an edit here or anywhere else, so nothing in
this module names a model. It only knows two *shapes* a ``cursor-agent`` id can
take for the same knob:

- the bracket form, ``grok-4.5[effort=high,fast=false]``, and
- the flat form, ``grok-4.7-medium`` / ``grok-4.7-medium-fast``, where the effort
  is part of the id and the CLI rejects a bracket outright.

Which form a family uses is Cursor's decision and has already moved once
(``grok-4.6[effort=medium,fast=false]`` became ``cursor-grok-4.6-medium``), so
the harness reads the shape off the id it was handed instead of assuming one.
The 2026-09-28 friction log is what assuming cost: ``grok-4.7-medium`` plus a
manifest effort of ``medium`` was composed into ``grok-4.7-medium[effort=medium]``
and rejected, twice.

**Pure string helpers only.** This is imported by :mod:`src.harness.headless`
and :mod:`src.harness.usage`, both of which run in the spawning process, so it
must never reach :mod:`src.harness.host` or :mod:`src.harness.profile` — see
``tests/test_spawn_boundary.py``.
"""

from __future__ import annotations

import re

from src.harness.state import EFFORT_LEVELS

# Longest first so ``xhigh`` is tried before ``high``; anchored on the leading
# hyphen either way, so ``-xhigh`` can never be read as ``-high``.
_EFFORT_SUFFIX_RE = re.compile(
    r"^(?P<stem>.+?)-(?P<effort>"
    + "|".join(sorted(EFFORT_LEVELS, key=len, reverse=True))
    + r")(?P<fast>-fast)?$",
    re.IGNORECASE,
)

# Cursor prefixes its own re-listing of a model (``cursor-grok-4.6-medium`` is the
# ``grok-4.6`` the bracket form used to name). Same model, so same family.
_CURSOR_PREFIX = "cursor-"


def split_effort_suffix(base: str | None) -> tuple[str, str | None, bool]:
    """Split a bracket-less id into ``(stem, effort, fast)``.

    ``"grok-4.7-medium-fast"`` -> ``("grok-4.7", "medium", True)``. An id with no
    effort suffix comes back whole: ``("grok-4.5", None, False)``.
    """
    text = (base or "").strip()
    match = _EFFORT_SUFFIX_RE.match(text)
    if not match:
        return text, None, False
    return match.group("stem"), match.group("effort").lower(), bool(match.group("fast"))


def join_effort_suffix(stem: str, effort: str, fast: bool = False) -> str:
    """Inverse of :func:`split_effort_suffix` for an id that carries an effort."""
    return f"{stem}-{effort}{'-fast' if fast else ''}"


def model_family(model: str | None) -> str:
    """The model an id names, with every per-run knob stripped.

    ``grok-4.7-medium``, ``grok-4.7-high-fast`` and ``grok-4.7[effort=low]`` are
    one family: the fixed per-process prefix and the output verbosity that usage
    estimates are calibrated on belong to the model, not to the effort it was
    run at. Lower-cased, so it is a grouping key and not something to pass back
    to a CLI.
    """
    base = (model or "").partition("[")[0].strip().lower()
    stem = split_effort_suffix(base)[0]
    if stem.startswith(_CURSOR_PREFIX) and len(stem) > len(_CURSOR_PREFIX):
        stem = stem[len(_CURSOR_PREFIX):]
    return stem
