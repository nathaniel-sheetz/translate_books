"""Structural guards over the `.claude/skills/` tree.

    .claude/skills/<name>/SKILL.md ─► frontmatter parses, name == <name>
                │                 └─► `references/x.md` mentions resolve on disk
                └────────────────────► CLAUDE.md carries an `invoke <name>` bullet

These assert *structure*, not prose. `tests/test_harness_pipeline.py` records the
deliberate stance that SKILL.md's orchestration prose is verified by manual dogfood
rather than pytest; registration, frontmatter and reference links are mechanical, so
they are cheap to hold here. The registration check exists because the recurring
mistake is adding a skill directory and forgetting to route it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Matches `references/x.md`, optionally fully qualified as
# `.claude/skills/<other-skill>/references/x.md` (judge-review points at
# translate-harness's address-map reference that way). A glob such as
# `references/*.md` deliberately does not match — there is no file to resolve.
_REFERENCE_RE = re.compile(
    r"(?:\.claude/skills/(?P<skill>[A-Za-z0-9_-]+)/)?"
    r"references/(?P<name>[A-Za-z0-9_.-]+\.md)"
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _skill_dirs() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        path
        for path in SKILLS_DIR.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def _frontmatter(skill_dir: Path) -> dict:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match, f"{skill_dir.name}/SKILL.md has no leading YAML frontmatter block"
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict), (
        f"{skill_dir.name}/SKILL.md frontmatter is not a YAML mapping"
    )
    return loaded


_SKILL_DIRS = _skill_dirs()
_SKILL_IDS = [path.name for path in _SKILL_DIRS]


# ── the tree itself ─────────────────────────────────────────────────────────


def test_skills_dir_is_populated():
    """Guard the parametrized tests below from passing vacuously on an empty list."""
    assert _SKILL_DIRS, f"no skill directories with a SKILL.md under {SKILLS_DIR}"


# ── registration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_SKILL_IDS)
def test_skill_is_routed_in_claude_md(skill_dir: Path):
    """Every skill directory has an `→ invoke <name>` bullet in CLAUDE.md.

    An unrouted skill is invisible: the agent never learns the request shape that
    should reach it.
    """
    routing = CLAUDE_MD.read_text(encoding="utf-8")
    assert re.search(rf"invoke {re.escape(skill_dir.name)}\b", routing), (
        f"skill '{skill_dir.name}' has no 'invoke {skill_dir.name}' routing bullet "
        f"in CLAUDE.md"
    )


# ── frontmatter ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_SKILL_IDS)
def test_frontmatter_name_matches_directory(skill_dir: Path):
    """`name:` is the invocation key — a mismatch with the directory breaks lookup."""
    name = _frontmatter(skill_dir).get("name")
    assert name == skill_dir.name, (
        f"{skill_dir.name}/SKILL.md declares name '{name}'; expected "
        f"'{skill_dir.name}' to match its directory"
    )


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_SKILL_IDS)
def test_frontmatter_has_description(skill_dir: Path):
    """The description is the only thing the router sees when deciding to invoke."""
    description = _frontmatter(skill_dir).get("description")
    assert isinstance(description, str) and description.strip(), (
        f"{skill_dir.name}/SKILL.md has an empty or missing description"
    )


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_SKILL_IDS)
def test_allowed_tools_is_a_list_of_names(skill_dir: Path):
    """`allowed-tools` is optional, but when present it restricts the skill's reach."""
    allowed = _frontmatter(skill_dir).get("allowed-tools")
    if allowed is None:
        return
    assert isinstance(allowed, list) and allowed, (
        f"{skill_dir.name}/SKILL.md allowed-tools is present but not a non-empty list"
    )
    for tool in allowed:
        assert isinstance(tool, str) and tool.strip(), (
            f"{skill_dir.name}/SKILL.md allowed-tools contains a non-name entry: {tool!r}"
        )


# ── reference links ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_SKILL_IDS)
def test_reference_links_resolve(skill_dir: Path):
    """Every `references/x.md` a SKILL.md tells the agent to Read exists on disk.

    Bare mentions resolve inside the skill's own directory; fully qualified ones
    resolve from the repo root.
    """
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    missing = []
    for match in _REFERENCE_RE.finditer(text):
        other = match.group("skill")
        base = SKILLS_DIR / other if other else skill_dir
        if not (base / "references" / match.group("name")).is_file():
            missing.append(match.group(0))
    assert not missing, (
        f"{skill_dir.name}/SKILL.md points at reference files that do not exist: "
        f"{sorted(set(missing))}"
    )
