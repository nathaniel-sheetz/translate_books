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
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# src/harness/state.py -> parents[0]=harness, [1]=src, [2]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Defaults applied when config.json is absent or a key is missing. These replace
# the values the old SKILL.md heredocs hardcoded inline.
DEFAULTS: dict[str, str] = {
    "target_language": "Spanish",
    "locale": "mx",
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "language_code": "es",
    "title": "",
    "author": "",
}

# Config keys a command may override (CLI flag -> config key); used by setup.
CONFIG_KEYS = tuple(DEFAULTS.keys())


# ── machine-readable result sentinel (streaming wrapped scripts) ──────────────
#
# The streaming harness commands (chunk/cost/translate/epub) wrap a subprocess
# whose human-facing progress goes to stdout. To ALSO give the agent a fresh,
# structured last_output.json (friction-log #18 — those commands used to leave the
# previous command's result in place), the wrapped script prints exactly one line
# ``HARNESS_RESULT: {...json...}``; ``flow._run_script`` tees the child's stdout,
# strips that one line, and returns the parsed dict as the command's result.
HARNESS_RESULT_PREFIX = "HARNESS_RESULT:"


def emit_harness_result(data: dict) -> None:
    """Print the structured-result sentinel a streaming harness wrapper exposes.

    One line, machine-only. ``flow._run_script`` captures it (and keeps it out of
    the human stream) so the streaming command can mirror a fresh structured result
    to ``last_output.json`` instead of leaving a stale one behind (friction-log #18).
    """
    print(f"{HARNESS_RESULT_PREFIX} {json.dumps(data, ensure_ascii=False)}", flush=True)


def _iter_nested_match(root: Path, project_id: str, _depth: int = 0):
    """Yield project dirs whose leaf name equals project_id, sorted alphabetically.

    Uses an explicit walk so it never follows symlinks, never expands glob
    metacharacters, and never descends into a project directory — consistent
    with _iter_project_dirs in web_ui/app.py.
    """
    if _depth > 20:
        return
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        is_proj = (entry / "chunks").exists() or (entry / "source.txt").exists()
        if entry.name == project_id and is_proj:
            yield entry
        elif not is_proj:
            yield from _iter_nested_match(entry, project_id, _depth + 1)


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
        for entry in _iter_nested_match(projects_root, project):
            if _found is None:
                _found = entry
            else:
                _log.warning(
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


def slugify(text: str) -> str:
    """Turn a book title into a filesystem-safe project slug.

    Mirrors the web UI's create-project slug (``web_ui/app.py``) so both entry
    points name projects identically: lowercase, runs of non-alphanumerics
    collapse to a single ``-``, trimmed; empty/symbol-only input -> ``"project"``.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"


def available_project_dir(slug: str) -> Path:
    """Return a not-yet-existing ``projects/<slug>`` dir, suffixing on collision.

    ``projects/<slug>`` if free, else the first free ``projects/<slug>-N`` with
    ``N`` starting at 2 (so a second *Understood Betsy* becomes
    ``understood-betsy-2``). Matches the web UI's collision loop. Used by
    ``setup`` when the slug is auto-derived from the title; an explicit
    ``--project`` is honored verbatim instead and may reuse an existing dir.
    """
    projects_root = REPO_ROOT / "projects"
    candidate = slug
    suffix = 2
    while (projects_root / candidate).exists():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return projects_root / candidate


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
    """Write ``.harness/config.json`` (merged over DEFAULTS so it is complete).

    Keys not in DEFAULTS (e.g. ``run_id``, the persisted spawn knobs) are kept
    as-is, so callers can stash extra state in the config without registering it
    as a CLI-overridable key.
    """
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v is not None})
    ensure_harness_dir(project_dir)
    config_path(project_dir).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── run id (one per pass through the harness) ───────────────────────────────
#
# A "run" is one trip through the pipeline, bounded by ``setup`` (which wipes
# ``.harness/`` for a clean run). The id lives in config.json so every later
# command can stamp the same run, and it is deliberately NOT in DEFAULTS /
# CONFIG_KEYS so it is never exposed as a CLI override.

_RUN_ID_TS_FMT = "%Y%m%d_%H%M%S_%f"  # microseconds break ties on fast re-runs


def new_run_id(project_dir: Path) -> str:
    """Mint a fresh run id, ``<slug>_<YYYYMMDD_HHMMSS_ffffff>`` (not persisted here)."""
    return f"{project_dir.name}_{datetime.now():{_RUN_ID_TS_FMT}}"


def ensure_run_id(project_dir: Path) -> str:
    """Return the project's current run id, minting + persisting one if absent.

    ``setup`` mints a fresh id each run; this is the read path every other
    command uses (and the back-fill for projects created before run-logging
    existed). Best-effort persistence: if the config can't be written, the
    minted id is still returned so the event can be stamped.
    """
    cfg = load_config(project_dir)
    rid = cfg.get("run_id")
    if rid:
        return rid
    rid = new_run_id(project_dir)
    cfg["run_id"] = rid
    try:
        save_config(project_dir, cfg)
    except OSError:
        _log.warning("Could not persist run_id for %s", project_dir, exc_info=True)
    return rid
