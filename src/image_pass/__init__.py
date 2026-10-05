"""
image-pass: redo a book's images headlessly, on a ChatGPT subscription.

Book images get redone often — lettering on maps and diagrams translated, scans
cleaned, a cover made, an illustration replaced. This package is the
deterministic half of that: it inventories the images a book references, renders
one Codex job per image, harvests the candidates, shows them beside the
original, and swaps the approved one in place behind a backup.

Five modules, each one stage:

- :mod:`inventory` — every image the book references, plus the cover.
- :mod:`jobs`      — ``prepare`` (validate + render prompts) and ``generate``
  (run Codex per candidate, harvest the file).
- :mod:`report`    — the plain-HTML review page: original beside each candidate.
- :mod:`apply`     — ``apply``, ``revert`` and ``verify``: the only code that
  writes into ``images/``.
- :mod:`ledger`    — the append-only record of what was replaced with what.

Two invariants the rest of the repo relies on:

**Filenames never change.** The approved candidate is converted to the
original's name and format, so no ``[IMAGE:images/<file>:<alt>]`` token in
``source.txt``, the chapters, the chunks or the alignments is ever touched. The
reader and the EPUB builder pick the new pixels up as-is.

**The original is never lost.** ``projects/`` is gitignored, so the first
replacement of any file copies it to ``images_original/<file>`` and nothing ever
overwrites that copy. ``revert`` restores from there, byte for byte.

Nothing here spawns a process. The Codex call lives in
:func:`src.harness.headless.run_image_job`, the one module allowed to launch a
CLI, which is what keeps the subscription-only guarantee structural.
"""

from __future__ import annotations

from pathlib import Path

MODE_TRANSLATE = "translate"
MODE_RESTORE = "restore"
MODE_COVER = "cover"
MODE_REPLACE = "replace"
MODES = (MODE_TRANSLATE, MODE_RESTORE, MODE_COVER, MODE_REPLACE)

# The names ``src/epub_builder.py:_resolve_cover`` auto-detects, in its order.
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def images_dir(project_dir: Path) -> Path:
    """Where the reader and the EPUB builder read images from."""
    return Path(project_dir) / "images"


def originals_dir(project_dir: Path) -> Path:
    """Write-once backups of every file this package has replaced."""
    return Path(project_dir) / "images_original"


def work_dir(project_dir: Path) -> Path:
    """Working dir: ``<project>/.harness/images/`` (shared .harness root)."""
    return Path(project_dir) / ".harness" / "images"


def jobs_dir(project_dir: Path) -> Path:
    return work_dir(project_dir) / "jobs"


def image_key(ref: str) -> str:
    """Canonical id of an image: its POSIX path relative to ``images/``.

    Tokens carry the ``images/`` prefix (``[IMAGE:images/001.jpg:…]``) and
    people type either form, so both resolve to ``001.jpg``.
    """
    key = str(ref or "").strip().replace("\\", "/").lstrip("/")
    while key.startswith("./"):
        key = key[2:]
    if key.lower().startswith("images/"):
        key = key[len("images/"):]
    return key


def is_safe_key(key: str) -> bool:
    """True when ``key`` names a file strictly inside ``images/``."""
    if not key or key.startswith("/") or ":" in key:
        return False
    parts = key.split("/")
    return all(part not in ("", ".", "..") for part in parts)


__all__ = [
    "COVER_NAMES",
    "IMAGE_SUFFIXES",
    "MODES",
    "MODE_COVER",
    "MODE_REPLACE",
    "MODE_RESTORE",
    "MODE_TRANSLATE",
    "image_key",
    "images_dir",
    "is_safe_key",
    "jobs_dir",
    "originals_dir",
    "work_dir",
]
