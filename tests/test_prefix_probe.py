"""The probe's Codex arms against the argv a wave runs.

Nothing here spawns a job: the arms are data, and only that data is checked.
"""

from __future__ import annotations

from scripts import prefix_probe
from src.harness import headless


def test_the_every_tool_off_arms_carry_the_trim_a_wave_ships():
    """An arm that disables fewer features measures a prefix no wave has."""
    assert prefix_probe.ARMS["X3"].flags == headless._CODEX_TRIM_FLAGS
    for name in ("X4", "X6"):
        flags = prefix_probe.ARMS[name].flags
        assert flags == prefix_probe.CODEX_NO_CONFIG + headless._CODEX_TRIM_FLAGS


def test_the_baseline_arms_run_with_no_trim_at_all():
    assert prefix_probe.ARMS["X0"].flags == ()
    assert prefix_probe.ARMS["X5"].flags == ()
