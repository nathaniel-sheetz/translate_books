"""Structural pins on the documentation set: links, anchors, script names.

The docs/harness-first-revamp rewrite deleted ``docs/BATCH_PIPELINE.md`` and
rewired roughly twenty cross-references. Nothing in the suite noticed — a
deleted doc leaves dangling links in every file that pointed at it, and a
renamed script silently orphans its section in ``CLI_REFERENCE.md``.

These are pure filesystem assertions: no fixtures, no imports of app code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ``](target)`` — the inline-link form. Reference-style links are not used in
# this repo's docs; if that changes, extend the pattern rather than the skips.
_LINK_RE = re.compile(r"\]\(([^)]+)\)")

# ATX headings only (``## Title``). The docs use no setext headings.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _tracked_markdown() -> list[Path]:
    """Markdown files git actually tracks, excluding vendored/archived trees.

    Walking the filesystem instead would pull in ``backups/`` and
    ``.claude/worktrees/``, which legitimately hold stale copies of docs that
    still reference deleted files.
    """
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    paths = [REPO_ROOT / line for line in out if line.strip()]
    return [p for p in paths if p.is_file()]


def _github_anchor(heading_text: str) -> str:
    """Slugify a heading the way GitHub does for in-page anchors.

    Lowercase, strip anything that is not alphanumeric/space/hyphen, then
    spaces to hyphens. ``## Resuming, redoing, repairing`` becomes
    ``resuming-redoing-repairing``.
    """
    text = heading_text.strip().lower()
    # GitHub slugifies the *rendered* text, so inline code and emphasis markers
    # vanish while their contents survive. Underscores are NOT markers here:
    # `eval_runs` renders as the literal text ``eval_runs`` and keeps its
    # underscore in the anchor, so stripping ``_`` would break every anchor
    # pointing at a snake_case heading.
    text = re.sub(r"[`*]", "", text)
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _anchors_in(path: Path) -> set[str]:
    body = path.read_text(encoding="utf-8")
    return {_github_anchor(m.group(2)) for m in _HEADING_RE.finditer(body)}


def _links_in(path: Path) -> list[str]:
    body = path.read_text(encoding="utf-8")
    links = []
    for match in _LINK_RE.finditer(body):
        target = match.group(1).strip()
        # Skip external URLs, mailto:, and bare in-page anchors handled below.
        if target.startswith(("http://", "https://", "mailto:", "<")):
            continue
        links.append(target)
    return links


MARKDOWN_FILES = _tracked_markdown()


def test_markdown_files_were_discovered():
    """Guard the guard: a broken git call would make every test below vacuous."""
    assert len(MARKDOWN_FILES) > 10


@pytest.mark.parametrize("doc", MARKDOWN_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_internal_links_resolve(doc):
    """Every relative ``](path)`` target exists on disk."""
    broken = []
    for target in _links_in(doc):
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue  # bare in-page anchor; covered by the anchor test
        resolved = (doc.parent / path_part).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, f"{doc.relative_to(REPO_ROOT)} links to missing files: {broken}"


@pytest.mark.parametrize("doc", MARKDOWN_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_link_anchors_resolve(doc):
    """Every ``#anchor`` matches a heading in the file it points at.

    Covers both in-page (``#section``) and cross-file
    (``OTHER.md#section``) anchors, which is how the harness docs link into
    each other's subsections.
    """
    broken = []
    for target in _links_in(doc):
        if "#" not in target:
            continue
        path_part, anchor = target.split("#", 1)
        if not anchor:
            continue
        if path_part:
            resolved = (doc.parent / path_part).resolve()
            if not resolved.exists() or resolved.suffix != ".md":
                continue  # missing file is the other test's failure to report
        else:
            resolved = doc
        if anchor not in _anchors_in(resolved):
            broken.append(target)
    assert not broken, f"{doc.relative_to(REPO_ROOT)} links to missing anchors: {broken}"


def test_every_script_named_in_cli_reference_exists():
    """``CLI_REFERENCE.md`` documents ``scripts/*``; a rename must not orphan a section."""
    body = (REPO_ROOT / "docs" / "CLI_REFERENCE.md").read_text(encoding="utf-8")
    named = set(re.findall(r"scripts/([A-Za-z_0-9]+\.(?:py|ps1))", body))
    assert named, "no scripts parsed out of CLI_REFERENCE.md — the pattern went stale"
    missing = sorted(n for n in named if not (REPO_ROOT / "scripts" / n).exists())
    assert not missing, f"CLI_REFERENCE.md documents scripts that no longer exist: {missing}"
