"""Per-project temp/config state for the translate-harness CLI.

Harness mode used to scatter intermediate files across a repo-global ``.tmp/``
directory and hand-pass them between inline-Python snippets in SKILL.md. That
collided across books (one ``.tmp/`` shared by every project) and put untested
orchestration in markdown. This module gives every harness command one place to:

  * resolve a project directory from an id or a path, and
  * resolve per-project ``projects/<slug>/.harness/`` working paths, and
  * load/save a small persisted ``config.json`` (target language, locale,
    provider, model, title, author) so commands stop hardcoding ``"Spanish"`` /
    ``"mx"`` / a default model the way the old heredocs did.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# src/harness/state.py -> parents[0]=harness, [1]=src, [2]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Defaults applied when config.json is absent or a key is missing. These replace
# the values the old SKILL.md heredocs hardcoded inline.
DEFAULTS: dict[str, str] = {
    "target_language": "Spanish",
    "locale": "mx",
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "language_code": "es",
    "title": "",
    "author": "",
}

# Config keys a command may override (CLI flag -> config key); used by setup.
CONFIG_KEYS = tuple(DEFAULTS.keys())


def resolve_project_dir(project: str, *, must_exist: bool = True) -> Path:
    """Accept either a project id (a folder under ``projects/``) or a path.

    A bare id (a single path component that is not an existing file) resolves to
    ``projects/<id>``; anything absolute or with a separator is treated as a direct
    path. ``must_exist=False`` (used by ``setup``) allows a not-yet-created target.
    """
    p = Path(project)
    if p.is_absolute() or len(p.parts) > 1 or p.exists():
        if must_exist and not p.exists():
            raise FileNotFoundError(f"project path not found: {project!r}")
        return p
    candidate = REPO_ROOT / "projects" / project
    if candidate.exists():
        return candidate
    # bare id not at the flat root: search grouping subfolders for a project dir
    # of that name (a project dir has chunks/ or source.txt).
    projects_root = REPO_ROOT / "projects"
    if projects_root.exists():
        _found = None
        for entry in projects_root.rglob(project):
            if entry.is_dir() and ((entry / "chunks").exists() or (entry / "source.txt").exists()):
                if _found is None:
                    _found = entry
                else:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        "Duplicate project id %r found at %s and %s; using %s",
                        project, _found, entry, _found,
                    )
                    break
        if _found is not None:
            return _found
    if not must_exist:
        return candidate
    raise FileNotFoundError(
        f"project not found: {project!r} (looked for a path and projects/{project})"
    )


def harness_dir(project_dir: Path) -> Path:
    """The per-project working directory, ``projects/<slug>/.harness/``."""
    return project_dir / ".harness"


def ensure_harness_dir(project_dir: Path, *, clean: bool = False) -> Path:
    """Create (optionally wiping first) the per-project ``.harness/`` directory."""
    d = harness_dir(project_dir)
    if clean and d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(project_dir: Path) -> Path:
    return harness_dir(project_dir) / "config.json"


def load_config(project_dir: Path) -> dict:
    """Load ``.harness/config.json`` merged over DEFAULTS (missing file is fine)."""
    cfg = dict(DEFAULTS)
    path = config_path(project_dir)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def save_config(project_dir: Path, cfg: dict) -> None:
    """Write ``.harness/config.json`` (merged over DEFAULTS so it is complete)."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v is not None})
    ensure_harness_dir(project_dir)
    config_path(project_dir).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
