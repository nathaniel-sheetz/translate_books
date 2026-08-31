"""Which books an unattended pass is allowed to touch, and what CLI each runs on.

``projects/`` is not a flat list of live books. The canonical walker
(:func:`src.harness.state.iter_project_dirs`) descends through grouping folders,
which is right for the dashboard — you want ``.macdonald/photogen-nycteris`` in
the project list — and wrong for a scheduled job, because the same walk also
finds ``.backburner/the-little-duke.bak-ch1-restore``: a *backup snapshot* whose
annotations are a copy of another book's, and reviewing them spends real machine
time to produce notes nobody will ever read.

No book on disk sets ``archived: true``, so the archive flag alone excludes
nothing. Hence two filters, in this order:

1. **Group denylist** — a configurable list of grouping folders (default
   ``.backburner`` and ``.published``) whose books are never in scope.
2. **``project.json``'s ``archived``** — the reader's own per-book choice, for
   when a book inside a live group is finished with.

The second half of this module answers "and what will each in-scope book run
as?". The rule is: **respect each book's pin; never force one.** The only new
lever is :data:`AUTOMATION_DEFAULTS`'s ``default_cli``, which slots in *between*
a book's own ``headless_cli`` pin and host detection — because under a scheduled
task there is no driving host to detect, so an un-pinned book would otherwise
fall silently to the implicit ``"claude"`` fallback with nothing to change it but
editing sixteen ``config.json`` files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.app_config import load_app_config
from src.harness import state as hstate

# The ``automation`` block's defaults. Anything the operator does not set in
# ``app_config.json`` comes from here, so a fresh clone behaves the same as this
# machine and every key has exactly one documented default.
AUTOMATION_DEFAULTS: dict[str, Any] = {
    # Grouping folders an unattended pass never descends into.
    "exclude_groups": [".backburner", ".published"],
    # Fallback launcher family for books that never pinned one. Outranks host
    # detection (there is no host under a scheduled task), never a book's pin.
    "default_cli": "claude",
    # Annotation types whose reviewed text may be written back unattended.
    # ``footnote`` is absent and must stay absent: its write is a *replace* and
    # its text is published into the EPUB.
    "auto_apply_types": ["word_choice", "inconsistency", "flag"],
    # Minimum reviewer confidence for an unattended write.
    "confidence_floor": "high",
    # Ceilings for one pass.
    "max_targets_per_run": 400,
    "deadline_minutes": 120,
    "concurrency": 5,
}

# Why a book was left out. Stable strings — the scanner prints them and the
# tests assert on them.
SKIP_EXCLUDED_GROUP = "excluded_group"
SKIP_ARCHIVED = "archived"
SKIP_DUPLICATE_ID = "duplicate_id"


@dataclass(frozen=True)
class ScopeEntry:
    """One in-scope book."""

    project_id: str
    project_dir: Path
    group: Optional[str] = None       # grouping folder under projects/, if any

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_dir": str(self.project_dir),
            "group": self.group,
        }


@dataclass(frozen=True)
class SkippedProject:
    """One book the scope rules excluded, and why."""

    project_id: str
    project_dir: Path
    reason: str
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        out = {
            "project_id": self.project_id,
            "project_dir": str(self.project_dir),
            "reason": self.reason,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class ScopeResult:
    """The whole answer: what is in, what is out, and under which settings."""

    projects: list[ScopeEntry] = field(default_factory=list)
    skipped: list[SkippedProject] = field(default_factory=list)
    exclude_groups: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.projects)

    def __len__(self) -> int:
        return len(self.projects)


def automation_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """:data:`AUTOMATION_DEFAULTS` merged under ``app_config.json``'s ``automation``.

    ``overrides`` (a driver's command-line flags) wins over both. Keys with a
    ``None`` value are ignored rather than blanking the default, so a caller can
    pass its whole argparse namespace without special-casing every unset flag.
    """
    merged = dict(AUTOMATION_DEFAULTS)
    block = load_app_config().get("automation")
    if isinstance(block, dict):
        merged.update({k: v for k, v in block.items() if v is not None})
    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})
    return merged


def _archived(project_dir: Path) -> bool:
    """``project.json``'s ``archived`` flag; False when absent or unreadable."""
    try:
        doc = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(doc, dict) and doc.get("archived"))


def _group_of(project_dir: Path, root: Path) -> Optional[str]:
    """The grouping folder a book sits in, relative to ``projects/``.

    ``projects/gaudenzia`` → ``None``; ``projects/.macdonald/short-stories`` →
    ``".macdonald"``; a deeper nesting returns the whole relative parent path.
    """
    try:
        rel = project_dir.relative_to(root)
    except ValueError:
        return None
    parent = rel.parent
    return None if str(parent) in (".", "") else parent.as_posix()


def in_scope(
    root: Optional[Path] = None,
    *,
    exclude_groups: Optional[list[str]] = None,
) -> ScopeResult:
    """Walk ``projects/`` and split it into in-scope books and skips.

    Preserves :func:`src.harness.state.iter_project_dirs`'s semantics exactly —
    symlinks skipped, depth-capped, sorted — and adds the two exclusions plus the
    duplicate-leaf-name rule the dashboard already applies: two books whose
    folders share a name are one addressable id, so the first wins and the second
    is reported rather than silently reviewed under the other's name.
    """
    root = root if root is not None else hstate.projects_root()
    if exclude_groups is None:
        exclude_groups = automation_config()["exclude_groups"]
    denied = {str(g).strip() for g in (exclude_groups or []) if str(g).strip()}

    projects: list[ScopeEntry] = []
    skipped: list[SkippedProject] = []
    seen: dict[str, Path] = {}

    for project_dir in hstate.iter_project_dirs(root):
        project_id = project_dir.name
        group = _group_of(project_dir, root)

        # Match on any path component, so a nested `.backburner/x/y` is excluded
        # by the same entry that excludes `.backburner/y`.
        parts = set((group or "").split("/")) if group else set()
        hit = denied & parts
        if hit:
            skipped.append(
                SkippedProject(project_id, project_dir, SKIP_EXCLUDED_GROUP, sorted(hit)[0])
            )
            continue

        if _archived(project_dir):
            skipped.append(SkippedProject(project_id, project_dir, SKIP_ARCHIVED))
            continue

        first = seen.get(project_id)
        if first is not None:
            skipped.append(
                SkippedProject(
                    project_id, project_dir, SKIP_DUPLICATE_ID, f"also at {first}"
                )
            )
            continue

        seen[project_id] = project_dir
        projects.append(ScopeEntry(project_id, project_dir, group))

    return ScopeResult(projects, skipped, sorted(denied))


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def resolve_book_cli(
    cfg: dict[str, Any],
    *,
    override: Optional[str] = None,
    default_cli: Optional[str] = None,
) -> tuple[str, str]:
    """``(cli, provenance)`` for one book under an unattended pass.

    :func:`src.harness.profile.resolve_cli`'s ladder with one rung inserted:

    1. ``override`` — the driver's ``--cli`` debugging flag → ``"cli"``
    2. the book's ``headless_cli`` pin → ``"config"``
    3. ``automation.default_cli`` → ``"automation.default_cli"``  ← new
    4. host detection → ``"host:*"``
    5. ``claude`` → ``"fallback"``

    Rung 3 sits above host detection rather than below it because the host under
    a scheduled task is nobody: without it, whether a book runs on Claude or
    Cursor would depend on which terminal happened to launch a manual re-run.
    """
    from src.harness.profile import resolve_cli

    cli, source = resolve_cli(cfg, override=override)
    if source in ("cli", "config"):
        return cli, source

    candidate = str(default_cli or "").strip().lower()
    if candidate in hstate.HEADLESS_CLIS:
        return candidate, "automation.default_cli"
    return cli, source


def book_profile(
    project_dir: Path,
    *,
    command: str = "annotations",
    override: Optional[str] = None,
    default_cli: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
    check_binary: bool = True,
):
    """The full :class:`~src.harness.profile.HeadlessProfile` for one book.

    Resolves the CLI through :func:`resolve_book_cli` first and hands the answer
    down, so the worker model, effort and token baseline are all derived from the
    *same* family the wave will actually run on. Passing a resolved CLI back into
    ``resolve_profile`` as an override is safe here precisely because
    :func:`resolve_book_cli` already honoured the book's pin — a pinned book
    reports ``config`` and gets its own value handed back unchanged.
    """
    from src.harness.profile import resolve_profile

    if cfg is None:
        cfg = hstate.load_config(project_dir)
    cli, source = resolve_book_cli(cfg, override=override, default_cli=default_cli)
    return resolve_profile(
        project_dir,
        command=command,
        cli=cli,
        cli_source=source,
        cfg=cfg,
        check_binary=check_binary,
    )
