"""
Shared judge ``context`` builder.

Every caller that runs a judge — ``scripts/run_judges.py`` (both the API ``run``
and the subagent ``prepare``) and the dashboard's Review tab — needs the same
per-project inputs loaded the same way, or the two paths render different
prompts for the same book. The address-map precheck in particular must not be
duplicated: without it the forms-of-address judge silently grades against
nothing, and its error strings are the only place a user is told which
``harness.py address-map`` command fixes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def build_judge_context(
    project_dir: Path,
    judge_names: list[str],
    model: Optional[str],
    provider: Optional[str],
) -> tuple[dict, Optional[str]]:
    """Build the judge ``context`` shared by every backend.

    Loads the per-project inputs judges read from disk so the API, subagent and
    dashboard paths render byte-identical prompts:
      * ``style_json_path`` — for judges that use the style guide.
      * ``address_map`` — the ``content`` prose of ``address_map.json`` for the
        forms-of-address judge.

    Returns ``(context, error)``. ``error`` is a human-readable string when the
    ``address`` judge is requested but no usable ``address_map.json`` exists
    (the caller emits it and refuses to run); otherwise ``None``.
    """
    project_dir = Path(project_dir)
    context: dict = {"judge_model": model, "judge_provider": provider}

    style_path = project_dir / "style.json"
    if style_path.exists():
        context["style_json_path"] = style_path

    map_path = project_dir / "address_map.json"
    address_map_loaded = False
    if map_path.exists():
        try:
            from src.utils.file_io import load_address_map

            amap = load_address_map(map_path)
            # v1 the judge reads the prose ``content``; fall back to global_rules
            # if a committed map left content empty.
            prose = (amap.content or "").strip() or (amap.global_rules or "").strip()
            if prose:
                context["address_map"] = prose
                address_map_loaded = True
            elif "address" in judge_names:
                return context, (
                    f"address_map.json at {map_path} has empty content and "
                    "global_rules — the address judge has nothing to check against. "
                    "Re-draft with non-empty `content`, then:\n"
                    f"  python scripts/harness.py address-map commit --project {project_dir.name}"
                )
        except Exception as exc:  # noqa: BLE001 - surface as a clean caller-side error
            return context, (
                f"address_map.json at {map_path} failed to load: {exc}. "
                f"Re-run: python scripts/harness.py address-map commit --project {project_dir.name}"
            )

    if "address" in judge_names and not address_map_loaded:
        return context, (
            "The 'address' judge needs a per-book address map, but "
            f"{map_path} does not exist. Build it first:\n"
            f"  python scripts/harness.py address-map prepare --project {project_dir.name}\n"
            f"  python scripts/harness.py address-map commit  --project {project_dir.name}"
        )

    return context, None


__all__ = ["build_judge_context"]
